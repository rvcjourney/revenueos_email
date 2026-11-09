"""Same-thread follow-ups for personalized campaigns (ADR-0021, slice 2).

Fake model, in-memory stores and mocked repositories only: no database, no
network, no OpenAI, no email. The SQL is proven in test_same_thread_hyper_real_db.py.
"""

# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.api.deps import WorkspaceContext
from app.modules.campaigns.message_rendering import (
    compute_content_digest,
    render_step_content,
)
from app.modules.campaigns.schemas import SequenceStepCreateIn
from app.modules.campaigns.sequence_service import SequenceService
from app.modules.personalization.config_schema import (
    PersonalizationConfig,
    compute_approval_digest,
)
from app.modules.personalization.fake_model import FakeModel
from app.modules.personalization.ports import (
    DraftedStep,
    GenerationOutput,
    SequenceDraftOutput,
)
from app.modules.personalization.reference_validator import (
    ContextEmail,
    DraftContext,
    validate_draft,
)
from app.modules.personalization.version import PROMPT_VERSION
from tests.support.personalization_fakes import CONFIG, NOW, REF_BODY
from tests.test_personalization_api import CAMPAIGN_ID, CTX, STEPS, USER, _campaign
from tests.test_personalization_api import TestPreviewRunner as _PreviewHarness
from tests.test_personalization_api import _service as _api_service
from tests.test_personalization_jit import _build, _run

PARENT_SUBJECT = "Quick idea for Acme"


def _previous() -> dict:
    return {
        "content_subject": "Earlier subject",
        "content_body_html": "<p>Hi Sarah, we help SaaS teams automate outbound "
        "prospecting for Acme.</p>",
        "accepted_at": NOW.replace(day=1),
        "angle": "Acme's sales team",
        "personalization_facts": [],
    }


def _threaded(store, *, parent: str | None = PARENT_SUBJECT) -> None:
    store.step.update(position=3, email_same_thread=True, email_subject=None)
    store.message["parent_subject"] = parent
    store.previous = _previous()


# ---------------------------------------------------------------------------
# D1: the API accepts the switch on personalized campaigns
# ---------------------------------------------------------------------------


def test_the_api_accepts_the_switch_on_a_personalized_campaign() -> None:
    sequence_id = uuid.uuid4()
    service = SequenceService(MagicMock())
    repo = MagicMock()
    repo.get_campaign.return_value = {
        "id": CAMPAIGN_ID, "status": "DRAFT", "campaign_type": "HYPER_PERSONALIZED", "activation_id": None,
    }
    repo.get_sequence.return_value = {"id": sequence_id, "campaign_id": CAMPAIGN_ID, "personalization_config": None}
    first = {"kind": "EMAIL", "position": 1, "email_same_thread": False}
    repo.list_steps_ordered.return_value = [first, {"kind": "WAIT", "position": 2}, {"kind": "EMAIL", "position": 3, "email_same_thread": True}]
    now = datetime.now(UTC)
    repo.insert_step.side_effect = lambda **kw: {
        **kw, "id": uuid.uuid4(), "version": 1, "created_at": now, "updated_at": now,
        "email_variable_schema": kw["email_variable_schema"],
    }
    service.repo = repo
    out = service.add_step(
        WorkspaceContext(workspace_id=uuid.uuid4(), user_id=USER, role_code="MANAGER"),
        CAMPAIGN_ID,
        SequenceStepCreateIn(kind="EMAIL", position=3, email_body_html=REF_BODY, email_same_thread=True),
    )
    assert out.email_same_thread is True
    assert repo.insert_step.call_args.kwargs["email_same_thread"] is True


# ---------------------------------------------------------------------------
# D3/D6: the subject comes from the parent, on both paths
# ---------------------------------------------------------------------------


class TestJitSubject:
    def test_a_generated_follow_up_takes_the_parents_subject(self) -> None:
        runner, store, model, _ = _build()
        _threaded(store)
        summary = _run(runner)
        assert summary.generated == 1
        msg = store.message
        assert msg["subject"] == f"Re: {PARENT_SUBJECT}"
        assert msg["digest"] == compute_content_digest(msg["subject"], msg["body"], msg["renderer_version"])
        # The prompt is unchanged: the model is still asked for a subject.
        assert model.requests[0].reference_subject == ""

    def test_a_fallback_follow_up_takes_the_parents_subject(self) -> None:
        runner, store, model, _ = _build(frozen={"first_name": "Sarah"})
        _threaded(store)
        summary = _run(runner)
        assert summary.fallback == 1 and model.calls == 0
        msg = store.message
        expected = render_step_content(
            subject="", body_html=REF_BODY, frozen_variables={"first_name": "Sarah"},
            renderer_version=1, thread_subject=f"Re: {PARENT_SUBJECT}",
        )
        assert (msg["subject"], msg["body"], msg["digest"]) == (
            expected.subject, expected.body_html, expected.content_digest,
        )

    def test_re_is_not_doubled(self) -> None:
        runner, store, _, _ = _build()
        _threaded(store, parent="RE: Quick idea for Acme")
        _run(runner)
        assert store.message["subject"] == "RE: Quick idea for Acme"

    def test_no_parent_uses_the_previous_sent_email(self) -> None:
        runner, store, _, _ = _build()
        _threaded(store, parent=None)
        _run(runner)
        assert store.message["subject"] == "Re: Earlier subject"
        assert store.audits == []

    def test_no_parent_and_no_previous_keeps_the_models_subject_and_records_why(self) -> None:
        runner, store, _, _ = _build()
        _threaded(store, parent=None)
        store.step["email_subject"] = "Fallback idea for {{company}}"
        store.previous = None
        _run(runner)
        assert store.message["status"] == "SCHEDULED"
        assert not store.message["subject"].startswith("Re:")
        assert store.audits == [{"message_id": store.message_id, "reason": "previous_subject_missing"}]

    def test_subject_problems_never_reject_a_threaded_follow_up(self) -> None:
        def no_subject(request):
            good = FakeModel().default_output(request)
            return GenerationOutput(subject="", paragraphs=good.paragraphs, facts_used=good.facts_used, angle=good.angle)

        runner, store, model, _ = _build(script=[no_subject])
        _threaded(store)
        summary = _run(runner)
        assert summary.generated == 1 and model.calls == 1
        assert store.message["subject"] == f"Re: {PARENT_SUBJECT}"

    def test_an_unthreaded_follow_up_is_still_checked(self) -> None:
        def no_subject(request):
            good = FakeModel().default_output(request)
            return GenerationOutput(subject="", paragraphs=good.paragraphs, facts_used=good.facts_used, angle=good.angle)

        runner, store, _, _ = _build(script=[no_subject])
        store.step["position"] = 3
        store.previous = _previous()
        _run(runner)
        assert "empty_subject" in store.generation["last_failure_codes"]

    def test_a_reference_edited_mid_generation_is_regenerated_still_threaded(self) -> None:
        runner, store, model, _ = _build()
        _threaded(store)
        original = model.generate

        def edit_then_generate(request):
            store.step["version"] = 2
            return original(request)

        model.generate = edit_then_generate  # type: ignore[method-assign]
        first = _run(runner)
        assert first.deferred == 1 and store.message["status"] == "PLANNED"
        model.generate = original  # type: ignore[method-assign]
        runner._clock.advance(seconds=5)
        _run(runner)
        assert store.message["subject"] == f"Re: {PARENT_SUBJECT}"

    def test_prompt_version_is_unchanged(self) -> None:
        assert PROMPT_VERSION == "hp-2"


# ---------------------------------------------------------------------------
# D6: approval digest
# ---------------------------------------------------------------------------


def _legacy_approval_digest(steps, model: str) -> str:
    email_steps = [
        {"position": s["position"], "subject": s.get("email_subject"), "body": s.get("email_body_html"),
         "preheader": s.get("email_preheader") or None}
        for s in sorted(steps, key=lambda s: s["position"]) if s["kind"] == "EMAIL"
    ]
    payload = json.dumps(
        {"config": dict(CONFIG), "steps": email_steps, "prompt_version": PROMPT_VERSION, "model": model},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class TestApprovalDigest:
    def test_existing_campaigns_keep_their_digest_byte_for_byte(self) -> None:
        legacy = _legacy_approval_digest(STEPS, "m")
        assert compute_approval_digest(config=CONFIG, steps=STEPS, model="m") == legacy
        unthreaded = [{**s, "email_same_thread": False} for s in STEPS]
        assert compute_approval_digest(config=CONFIG, steps=unthreaded, model="m") == legacy

    def test_turning_the_switch_on_makes_the_approval_stale(self) -> None:
        approved = _api_service(campaign=_campaign())
        digest, _, _ = approved.current_digest(CTX, CAMPAIGN_ID)
        rows = [{"config_digest": digest, "approved_at": datetime.now(UTC), "approved_by": USER}]
        approved.repo.list_approvals.return_value = rows
        assert approved.approval_status(CTX, CAMPAIGN_ID).status == "APPROVED"

        threaded_steps = [*STEPS[:2], {**STEPS[2], "email_same_thread": True}]
        edited = _api_service(campaign=_campaign(), steps=threaded_steps)
        edited.repo.list_approvals.return_value = rows
        assert edited.approval_status(CTX, CAMPAIGN_ID).status == "STALE"


# ---------------------------------------------------------------------------
# Previews: "Re: " + the previous step's preview subject
# ---------------------------------------------------------------------------


def test_a_threaded_preview_replies_to_the_previous_preview() -> None:
    harness = _PreviewHarness()
    runner, store, _ = harness._runner()
    store.previews[1].update(email_same_thread=True, email_subject=None)
    summary = harness._run(runner)
    assert summary.ok == 2
    first, second = store.previews
    assert second["subject"] == f"Re: {first['subject']}"


# ---------------------------------------------------------------------------
# Drafting (ADR-0016/0018): distinct subjects only for unthreaded steps
# ---------------------------------------------------------------------------


def _drafted(position: int, subject: str) -> DraftedStep:
    return DraftedStep(position=position, role="r", subject=subject,
                       paragraphs=("Hi {{first_name|there}},", "Short."), preheader="", wait_days_after=3)


def _draft_ctx(**kw) -> DraftContext:
    return DraftContext(
        objective=PersonalizationConfig.model_validate(CONFIG), company=None,
        expected_positions=(1, 2), allowed_variables=frozenset({"first_name"}), **kw,
    )


class TestDrafting:
    def test_a_repeated_subject_is_flagged_between_unthreaded_emails(self) -> None:
        result = validate_draft(SequenceDraftOutput(theme="t", steps=(_drafted(1, "Same"), _drafted(2, "Same"))), _draft_ctx())
        assert "subject_repeated" in result.by_position[2]

    def test_a_threaded_steps_subject_is_ignored(self) -> None:
        result = validate_draft(
            SequenceDraftOutput(theme="t", steps=(_drafted(1, "Same"), _drafted(2, "Same"))),
            _draft_ctx(threaded_positions=frozenset({2})),
        )
        assert "subject_repeated" not in result.by_position[2]
        assert result.steps[1].subject == ""
        empty = validate_draft(
            SequenceDraftOutput(theme="t", steps=(_drafted(1, "One"), _drafted(2, ""))),
            _draft_ctx(threaded_positions=frozenset({2})),
        )
        assert "subject_invalid" not in empty.by_position[2]

    def test_a_threaded_neighbours_subject_is_not_compared(self) -> None:
        result = validate_draft(
            SequenceDraftOutput(theme="t", steps=(_drafted(1, "Same"),)),
            DraftContext(
                objective=PersonalizationConfig.model_validate(CONFIG), company=None,
                expected_positions=(1,), allowed_variables=frozenset({"first_name"}),
                context_emails=(ContextEmail(position=2, subject="Same", body_text="other words here", threaded=True),),
            ),
        )
        assert "subject_repeated" not in result.by_position[1]
