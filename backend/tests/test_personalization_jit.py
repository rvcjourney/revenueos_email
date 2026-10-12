"""Just-in-time generation runner: claim -> model -> finalize, against an
in-memory store that mirrors the SQL semantics (leases, charged attempts,
guarded finalize, rollback). No database, no network, no email."""

from __future__ import annotations

import uuid

import pytest

from app.modules.campaigns.message_rendering import (
    compute_content_digest,
    render_step_content,
)
from app.modules.personalization.budget import AllowAllLimiter
from app.modules.personalization.fake_model import FakeModel
from app.modules.personalization.jit import GenerationRunner, RunnerConfig
from app.modules.personalization.ports import (
    GenerationOutput,
    MalformedOutput,
    ModelPermanentError,
    ModelRateLimited,
    ModelTimeout,
    PreviousEmail,
    ResearchOutcome,
)
from app.modules.personalization.service import PersonalizationService
from tests.support.personalization_fakes import (
    FROZEN,
    NOW,
    WS,
    Clock,
    FakeResearch,
    FakeStore,
)

CAMPAIGN = uuid.uuid4()


class DenyLimiter:
    def allow(self) -> bool:
        return False


def _build(
    *,
    script=None,
    max_attempts: int = 3,
    research: ResearchOutcome | None = None,
    rpm=None,
    cap: int = 100,
    max_transient: int = 3,
    frozen=None,
):
    clock = Clock()
    store = FakeStore(clock, max_attempts=max_attempts)
    if frozen is not None:
        store.enrollment["frozen_variables"] = frozen
    model = FakeModel(script)
    service = PersonalizationService(
        model=model, research=FakeResearch(research), min_facts=2
    )
    runner = GenerationRunner(
        db=store,
        service=service,
        rpm=rpm or AllowAllLimiter(),
        config=RunnerConfig(
            lease_seconds=300,
            chunk_size=10,
            max_transient_errors=max_transient,
            daily_generation_cap=cap,
        ),
        clock=clock,
    )
    return runner, store, model, clock


def _run(runner: GenerationRunner, owner: str = "worker-1"):
    return runner.run_chunk(workspace_id=WS, campaign_id=CAMPAIGN, lease_owner=owner)


def _bad_number(model: FakeModel):
    def make(request):
        good = model.default_output(request)
        return GenerationOutput(
            subject=good.subject,
            paragraphs=(
                *good.paragraphs[:-1],
                "We grew revenue 847% last year.",
                good.paragraphs[-1],
            ),
            facts_used=good.facts_used,
            angle=good.angle,
        )

    return make


class TestHappyPath:
    def test_generates_validates_and_schedules_an_immutable_snapshot(self) -> None:
        runner, store, model, _ = _build()
        summary = _run(runner)

        assert summary.generated == 1 and summary.failed == 0
        msg = store.message
        assert msg["status"] == "SCHEDULED"
        assert msg["renderer_version"] == 2
        # The same digest formula the send gate recomputes from the stored body.
        assert msg["digest"] == compute_content_digest(msg["subject"], msg["body"], 2)
        assert "Sarah" in msg["body"] and "<p>" in msg["body"]
        assert store.generation["state"] == "SUCCEEDED"
        assert store.generation["lease_owner"] is None
        assert store.generation["fallback_used"] is False
        assert [a["outcome"] for a in store.attempts] == ["ACCEPTED"]
        assert store.usage["GENERATION"] == 1
        assert store.tokens == {"in": 100, "out": 80}
        assert model.calls == 1

    def test_records_the_facts_and_angle_used_for_follow_up_context(self) -> None:
        runner, store, _, _ = _build()
        _run(runner)
        facts = store.generation["facts"]
        assert facts and facts[0]["id"] == "F1"
        assert store.generation["angle"] == "fake personalization"
        assert store.generation["prompt_version"]

    def test_due_time_moves_into_the_window_when_generation_finishes_late(
        self,
    ) -> None:
        runner, store, _, clock = _build()
        store.message["due_at"] = NOW  # already due: generate now
        clock.now = NOW.replace(hour=18)  # after the 09:00-17:00 window
        store.generation["next_attempt_at"] = NOW
        _run(runner)
        due = store.message["due_at"]
        assert due.hour == 9 and due.day == 3  # next window start

    def test_a_second_worker_cannot_claim_a_leased_generation(self) -> None:
        runner, store, model, _ = _build()
        # First worker claims but has not finished (simulated by claiming only).
        with store.transaction() as repo:
            repo.claim_due(lease_owner="w1", ttl_seconds=300, limit=10)
        summary = _run(runner, owner="w2")
        assert summary.claimed == 0 and model.calls == 0


class TestValidationRetries:
    def test_rejected_output_is_retried_with_codes_only_then_succeeds(self) -> None:
        model = FakeModel()
        runner, store, model, clock = _build(script=[])
        model._script = [_bad_number(model)]
        summary = _run(runner)
        assert summary.rejected == 1
        assert store.generation["state"] == "PENDING"
        assert store.generation["attempt_count"] == 1
        assert store.generation["last_failure_codes"] == ["unsupported_number"]
        assert store.message["status"] == "PLANNED"  # nothing sendable yet
        assert store.attempts[0]["outcome"] == "REJECTED_VALIDATION"

        clock.advance(minutes=5)
        summary = _run(runner)
        assert summary.generated == 1
        assert store.message["status"] == "SCHEDULED"
        retry_request = model.requests[1]
        assert retry_request.retry_codes == ("unsupported_number",)
        # The rejected text is never fed back, only the code.
        assert "847" not in repr(retry_request)

    def test_attempts_exhausted_fails_the_message_and_never_sends(self) -> None:
        runner, store, model, clock = _build(max_attempts=2)
        model._script = [_bad_number(model), _bad_number(model)]
        _run(runner)
        clock.advance(minutes=5)
        summary = _run(runner)
        assert summary.failed == 1
        assert store.message["status"] == "FAILED"
        assert store.message["terminal_reason"] == (
            "personalization_failed:unsupported_number"
        )
        assert store.generation["state"] == "FAILED"
        assert store.message.get("subject") is None  # no content ever written
        assert model.calls == 2  # billed calls bounded by max_attempts

    def test_malformed_output_counts_as_a_rejected_attempt(self) -> None:
        runner, store, model, _ = _build(script=[MalformedOutput("not json")])
        summary = _run(runner)
        assert summary.rejected == 1 and summary.transient_errors == 0
        assert store.generation["attempt_count"] == 1
        assert store.attempts[0]["outcome"] == "MALFORMED_OUTPUT"

    def test_never_say_phrase_is_rejected(self) -> None:
        model = FakeModel()

        def make(request):
            good = model.default_output(request)
            return GenerationOutput(
                subject=good.subject,
                paragraphs=(
                    *good.paragraphs[:-1],
                    "Results are guaranteed.",
                    good.paragraphs[-1],
                ),
                facts_used=good.facts_used,
                angle=good.angle,
            )

        runner, store, model, _ = _build()
        model._script = [make]
        _run(runner)
        assert store.generation["last_failure_codes"] == ["never_say_violation"]


class TestProviderErrors:
    def test_timeout_is_transient_and_does_not_burn_a_quality_attempt(self) -> None:
        runner, store, _, clock = _build(script=[ModelTimeout()])
        summary = _run(runner)
        assert summary.transient_errors == 1
        assert store.generation["attempt_count"] == 0  # refunded
        assert store.generation["transient_error_count"] == 1
        assert store.generation["state"] == "PENDING"
        assert store.generation["next_attempt_at"] > clock.now
        assert store.attempts[0]["outcome"] == "TIMEOUT"

    def test_rate_limit_honours_retry_after(self) -> None:
        runner, store, _, clock = _build(
            script=[ModelRateLimited(retry_after_seconds=120)]
        )
        _run(runner)
        assert store.generation["next_attempt_at"] >= clock.now.replace(
            minute=clock.now.minute + 2
        )

    def test_persistent_outage_eventually_fails_without_sending(self) -> None:
        runner, store, model, clock = _build(max_transient=2)
        model._script = [ModelTimeout(), ModelTimeout(), ModelTimeout()]
        for _ in range(3):
            _run(runner)
            clock.advance(hours=1)
        assert store.generation["state"] == "FAILED"
        assert store.generation["failure_code"] == "provider_unavailable"
        assert store.message["status"] == "FAILED"

    def test_permanent_error_keeps_the_job_pending_and_burns_no_attempt(
        self,
    ) -> None:
        runner, store, _, clock = _build(
            script=[ModelPermanentError(code="provider_401")]
        )
        summary = _run(runner)
        assert summary.permanent_errors == 1
        assert store.generation["state"] == "PENDING"
        assert store.generation["attempt_count"] == 0
        assert store.generation["last_failure_codes"] == ["provider_401"]
        assert store.generation["next_attempt_at"] > clock.now
        assert store.message["status"] == "PLANNED"

    def test_an_unexpected_bug_does_not_abandon_the_rest_of_the_chunk(self) -> None:
        runner, store, model, _ = _build()

        def boom(request):
            raise RuntimeError("bug")

        model._script = [boom]
        _run(runner)  # must not raise
        assert store.message["status"] == "PLANNED"
        assert store.generation["lease_owner"] == "worker-1"  # recovered by expiry


class TestEligibility:
    def test_reply_during_generation_supersedes_and_writes_nothing(self) -> None:
        runner, store, model, _ = _build()

        def reply_arrives(request):
            store.enrollment["state"] = "STOPPED"
            store.enrollment["next_step_id"] = None
            return model.default_output(request)

        model._script = [reply_arrives]
        summary = _run(runner)
        assert summary.superseded == 1 and summary.generated == 0
        assert store.message["status"] == "PLANNED"
        assert store.generation["state"] == "SUPERSEDED"
        assert store.usage["GENERATION"] == 0  # refunded: nothing was produced
        assert store.attempts[0]["outcome"] == "ABORTED_INELIGIBLE"

    def test_unsubscribe_during_generation_supersedes(self) -> None:
        runner, store, _, _ = _build()
        store.suppressed = True
        summary = _run(runner)
        assert summary.superseded == 1
        assert store.message["status"] == "PLANNED"

    def test_already_stopped_before_claim_never_calls_the_model(self) -> None:
        runner, store, model, _ = _build()
        store.message["status"] = "SKIPPED"
        summary = _run(runner)
        assert summary.claimed == 0 and model.calls == 0

    def test_pause_before_claim_is_skipped(self) -> None:
        runner, store, model, _ = _build()
        store.campaign["status"] = "PAUSED"
        summary = _run(runner)
        assert summary.claimed == 0 and model.calls == 0

    def test_pause_during_generation_defers_without_spending(self) -> None:
        runner, store, model, clock = _build()

        def paused(request):
            store.campaign["status"] = "PAUSED"
            return model.default_output(request)

        model._script = [paused]
        summary = _run(runner)
        assert summary.deferred == 1
        assert store.message["status"] == "PLANNED"
        assert store.generation["state"] == "PENDING"
        assert store.generation["attempt_count"] == 0
        assert store.usage["GENERATION"] == 0
        assert store.generation["next_attempt_at"] > clock.now

    def test_reference_edited_during_generation_discards_the_stale_email(self) -> None:
        """Pause -> edit the reference -> resume while a generation is in flight: what
        the model wrote followed the OLD reference, so it is thrown away unspent and
        written again from the new one."""
        runner, store, model, clock = _build()

        def edited(request):
            store.step["version"] = store.step.get("version", 1) + 1
            store.step["email_subject"] = "Better subject"
            return model.default_output(request)

        model._script = [edited]
        summary = _run(runner)
        assert summary.deferred == 1 and summary.generated == 0
        assert store.message["status"] == "PLANNED"  # nothing written
        assert store.generation["state"] == "PENDING"  # will be claimed again
        assert store.generation["attempt_count"] == 0
        assert store.usage["GENERATION"] == 0
        assert store.attempts[0]["outcome"] == "ABORTED_INELIGIBLE"
        assert store.generation["next_attempt_at"] > clock.now

    def test_message_already_scheduled_by_someone_else_is_not_overwritten(
        self,
    ) -> None:
        runner, store, model, _ = _build()

        def raced(request):
            store.message["status"] = "SCHEDULED"  # another finisher won
            store.message["subject"] = "other"
            return model.default_output(request)

        model._script = [raced]
        summary = _run(runner)
        assert summary.generated == 0
        assert store.message["subject"] == "other"
        assert store.generation["state"] == "SUPERSEDED"


class TestBudgets:
    def test_global_rate_limit_defers_without_calling_the_model(self) -> None:
        runner, store, model, clock = _build(rpm=DenyLimiter())
        summary = _run(runner)
        assert summary.deferred == 1 and model.calls == 0
        assert store.generation["attempt_count"] == 0
        assert store.generation["next_attempt_at"] > clock.now

    def test_daily_cap_defers_without_calling_the_model(self) -> None:
        runner, store, model, _ = _build(cap=0)
        summary = _run(runner)
        assert summary.deferred == 1 and model.calls == 0
        assert store.generation["attempt_count"] == 0
        assert summary.codes == {"daily_cap_reached": 1}


class TestThinContextFallback:
    def test_sends_the_rendered_reference_and_never_calls_the_model(self) -> None:
        runner, store, model, _ = _build(frozen={"first_name": "Sarah"})
        summary = _run(runner)
        assert summary.fallback == 1 and summary.generated == 0
        assert model.calls == 0
        msg = store.message
        expected = render_step_content(
            subject=store.step["email_subject"],
            body_html=store.step["email_body_html"],
            frozen_variables={"first_name": "Sarah"},
            renderer_version=1,
        )
        assert msg["status"] == "SCHEDULED"
        assert msg["renderer_version"] == 1
        assert msg["subject"] == expected.subject
        assert msg["body"] == expected.body_html
        assert msg["digest"] == expected.content_digest
        assert store.generation["fallback_used"] is True
        assert store.generation["state"] == "SUCCEEDED"
        assert store.attempts[0]["outcome"] == "FALLBACK"

    def test_website_research_can_lift_a_lead_out_of_thin_context(self) -> None:
        runner, store, model, _ = _build(
            frozen={"first_name": "Sarah", "company": "Acme"},
            research=ResearchOutcome(
                status="OK",
                snippets=("Acme builds outbound tooling for revenue teams.",),
                source_url="https://acme.test/",
            ),
        )
        summary = _run(runner)
        assert summary.generated == 1 and summary.fallback == 0
        assert model.calls == 1


class TestCrashRecovery:
    def test_crash_before_commit_leaves_no_partial_write_and_is_bounded(
        self,
    ) -> None:
        runner, store, model, clock = _build(max_attempts=2)
        store.fail_finalize_once = True
        _run(runner)  # the finalize transaction raised and rolled back
        assert store.message["status"] == "PLANNED"
        assert store.generation["state"] == "PENDING"
        assert store.attempts[0]["outcome"] == "RESERVED"
        assert model.calls == 1

        # Lease expires; the next claim closes the stale attempt as ABANDONED.
        clock.advance(minutes=10)
        _run(runner)
        outcomes = [a["outcome"] for a in store.attempts]
        assert outcomes[0] == "ABANDONED"
        assert store.message["status"] == "SCHEDULED"
        assert model.calls == 2  # exactly one extra billed call

    def test_repeated_crashes_end_in_a_failed_message_not_endless_billing(
        self,
    ) -> None:
        runner, store, model, clock = _build(max_attempts=2)
        for _ in range(2):
            store.fail_finalize_once = True
            _run(runner)
            clock.advance(minutes=10)
        # Attempts are used up while nobody holds the lease -> failed, not retried.
        summary = _run(runner)
        assert summary.exhausted == 1
        assert store.message["status"] == "FAILED"
        assert store.generation["failure_code"] == "attempts_exhausted"
        assert model.calls == 2


class TestFollowUp:
    def _previous(self) -> dict:
        return {
            "content_subject": "Quick idea for Acme",
            "content_body_html": "<p>Hi Sarah, we help SaaS teams automate "
            "outbound prospecting for Acme.</p>",
            "accepted_at": NOW.replace(day=1),
            "angle": "Acme's sales team",
            "personalization_facts": [
                {"id": "F3", "source": "LEAD", "text": "Job title: VP Sales"}
            ],
        }

    def test_follow_up_receives_the_previous_email_and_no_reply_context(self) -> None:
        runner, store, model, _ = _build()
        store.step["position"] = 2
        store.previous = self._previous()
        _run(runner)
        request = model.requests[0]
        assert isinstance(request.previous, PreviousEmail)
        assert request.previous.subject == "Quick idea for Acme"
        assert request.previous.days_since_sent == 1
        assert request.previous.facts_used == ("Job title: VP Sales",)
        assert store.message["status"] == "SCHEDULED"

    def test_a_follow_up_that_repeats_the_previous_email_is_rejected(self) -> None:
        runner, store, model, _ = _build()
        store.step["position"] = 2
        store.previous = self._previous()

        def repeat(request):
            return GenerationOutput(
                subject="Quick idea for Acme",
                paragraphs=(
                    "Hi Sarah, we help SaaS teams automate outbound prospecting "
                    "for Acme. Would you be open to a quick conversation?",
                ),
                facts_used=("F1",),
                angle="same as before",
            )

        model._script = [repeat]
        _run(runner)
        codes = store.generation["last_failure_codes"]
        assert "repeats_previous" in codes and "subject_repeated" in codes

    def test_filler_follow_up_is_rejected(self) -> None:
        runner, store, model, _ = _build()
        store.step["position"] = 2
        store.previous = self._previous()

        def filler(request):
            good = model.default_output(request)
            return GenerationOutput(
                subject="A different subject line",
                paragraphs=("Just following up on this.", *good.paragraphs[1:]),
                facts_used=good.facts_used,
                angle=good.angle,
            )

        model._script = [filler]
        _run(runner)
        assert "filler_followup" in store.generation["last_failure_codes"]


@pytest.mark.parametrize("field", ["email", "phone", "linkedin_url"])
def test_contact_data_never_reaches_the_model(field: str) -> None:
    runner, _, model, _ = _build()
    _run(runner)
    blob = repr(model.requests[0])
    assert FROZEN[field] not in blob
