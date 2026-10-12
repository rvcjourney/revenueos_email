"""Just-in-time generation runner (ADR-0011).

Per message the unit of work is:

  T1  short transaction: claim (lease + charge one attempt), check the global
      rate limit and the workspace's daily cap, append a RESERVED ledger row,
      load everything needed. COMMIT.
      -- no transaction, no lock -- research fetch, model call, validation.
  T2  short transaction: re-lock the message, recheck eligibility (message still
      PLANNED, campaign RUNNING/READY, enrollment ACTIVE at this step, address
      not suppressed) and write the immutable snapshot (PLANNED -> SCHEDULED),
      or record the rejection / failure. COMMIT.

Everything is safe to repeat: the lease + UNIQUE(message) + `WHERE
status='PLANNED'` make a duplicate finisher a no-op; a crash after the model call
leaves a RESERVED attempt that is closed as ABANDONED at lease expiry and still
counts, so billed calls per message are bounded by `max_attempts`. The send
worker is not involved and cannot generate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.modules.campaigns.follow_up_timing import (
    intended_follow_up_due,
    wait_edited_after,
)
from app.modules.campaigns.scheduling import project_into_window
from app.modules.personalization.budget import RpmLimiter
from app.modules.personalization.config_schema import parse_config
from app.modules.personalization.ports import (
    MalformedOutput,
    ModelError,
    ModelPermanentError,
    ModelRateLimited,
    ModelRefusal,
    ModelTimeout,
    PreviousEmail,
)
from app.modules.personalization.service import (
    AttemptOutcome,
    GenerationInputs,
    PersonalizationService,
    facts_payload,
)
from app.modules.personalization.text_utils import html_to_text
from app.modules.personalization.version import PROMPT_VERSION

logger = logging.getLogger(__name__)

_REJECT_BACKOFF_BASE_SECONDS = 15
_REJECT_BACKOFF_MAX_SECONDS = 600
_TRANSIENT_BACKOFF_BASE_SECONDS = 30
_TRANSIENT_BACKOFF_MAX_SECONDS = 1800
_RPM_DEFER_SECONDS = 30
_CAP_DEFER_SECONDS = 1800
_PAUSED_DEFER_SECONDS = 300
_PERMANENT_DEFER_SECONDS = 1800
_PREVIOUS_BODY_CHARS = 4000


@dataclass(frozen=True)
class RunnerConfig:
    lease_seconds: int = 300
    chunk_size: int = 10
    max_transient_errors: int = 8
    daily_generation_cap: int = 2000
    # How long before its due time a follow-up is written (the planner's lead time).
    lead_seconds: int = 3600

    @classmethod
    def from_settings(cls, settings: Any) -> RunnerConfig:
        return cls(
            lease_seconds=settings.personalization_lease_seconds,
            chunk_size=settings.personalization_chunk_size,
            max_transient_errors=settings.personalization_max_transient_errors,
            daily_generation_cap=settings.personalization_daily_generation_cap,
            lead_seconds=settings.personalization_lead_time_seconds,
        )


@dataclass
class ChunkSummary:
    exhausted: int = 0
    claimed: int = 0
    generated: int = 0
    fallback: int = 0
    rejected: int = 0
    failed: int = 0
    superseded: int = 0
    deferred: int = 0
    transient_errors: int = 0
    permanent_errors: int = 0
    codes: dict[str, int] = field(default_factory=dict)

    def count_code(self, code: str) -> None:
        self.codes[code] = self.codes.get(code, 0) + 1


@dataclass(frozen=True)
class _Item:
    generation_id: UUID
    message_id: UUID
    attempt_no: int
    attempt_count: int
    max_attempts: int
    transient_error_count: int
    retry_codes: tuple[str, ...]
    work: Mapping[str, Any]
    previous: PreviousEmail | None


def _backoff(base: int, cap: int, n: int) -> float:
    return float(min(cap, base * (2 ** max(0, n - 1))))


class GenerationRunner:
    def __init__(
        self,
        *,
        db: Any,
        service: PersonalizationService,
        rpm: RpmLimiter,
        config: RunnerConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._db = db
        self._service = service
        self._rpm = rpm
        self._config = config
        self._clock = clock

    # ------------------------------------------------------------------

    def run_chunk(
        self, *, workspace_id: UUID, campaign_id: UUID, lease_owner: str
    ) -> ChunkSummary:
        summary = ChunkSummary()
        items = self._claim(workspace_id, campaign_id, lease_owner, summary)
        for item in items:
            try:
                self._process(workspace_id, item, summary)
            except Exception:
                # A bug or infrastructure fault in one message must not abandon
                # the rest of the chunk; the lease expires and the attempt is
                # closed as ABANDONED (still counted) on the next claim.
                logger.exception(
                    "Unexpected error generating message",
                    extra={"generation_id": str(item.generation_id)},
                )
        return summary

    # ------------------------------------------------------------------
    # T1
    # ------------------------------------------------------------------

    def _claim(
        self,
        workspace_id: UUID,
        campaign_id: UUID,
        lease_owner: str,
        summary: ChunkSummary,
    ) -> list[_Item]:
        now = self._clock()
        items: list[_Item] = []
        with self._db.transaction() as repo:
            repo.purge_expired(workspace_id=workspace_id, limit=50)
            exhausted = repo.fail_exhausted(
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                limit=self._config.chunk_size,
            )
            summary.exhausted = len(exhausted)
            claimed = repo.claim_due(
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                lease_owner=lease_owner,
                ttl_seconds=self._config.lease_seconds,
                limit=self._config.chunk_size,
            )
            summary.claimed = len(claimed)
            for row in claimed:
                generation_id = UUID(str(row["id"]))
                repo.abandon_open_attempts(
                    workspace_id=workspace_id, generation_id=generation_id
                )
                if not self._rpm.allow():
                    repo.release_claim(
                        workspace_id=workspace_id,
                        generation_id=generation_id,
                        delay_seconds=_RPM_DEFER_SECONDS,
                        refund_attempt=True,
                    )
                    summary.deferred += 1
                    continue
                if not repo.reserve_usage(
                    workspace_id=workspace_id,
                    day=now.date(),
                    kind="GENERATION",
                    cap=self._config.daily_generation_cap,
                ):
                    repo.release_claim(
                        workspace_id=workspace_id,
                        generation_id=generation_id,
                        delay_seconds=_CAP_DEFER_SECONDS,
                        refund_attempt=True,
                        failure_codes=["daily_cap_reached"],
                    )
                    summary.deferred += 1
                    summary.count_code("daily_cap_reached")
                    continue
                work = repo.load_work_item(
                    workspace_id=workspace_id, generation_id=generation_id
                )
                if work is None:
                    repo.fail_generation(
                        workspace_id=workspace_id,
                        generation_id=generation_id,
                        message_id=UUID(str(row["message_id"])),
                        failure_code="work_item_unavailable",
                    )
                    summary.failed += 1
                    continue
                attempt_no = repo.insert_attempt(
                    workspace_id=workspace_id, generation_id=generation_id
                )
                previous = self._load_previous(repo, workspace_id, work, now)
                items.append(
                    _Item(
                        generation_id=generation_id,
                        message_id=UUID(str(row["message_id"])),
                        attempt_no=attempt_no,
                        attempt_count=int(row["attempt_count"]),
                        max_attempts=int(row["max_attempts"]),
                        transient_error_count=int(row["transient_error_count"]),
                        retry_codes=tuple(row["last_failure_codes"] or ()),
                        work=dict(work),
                        previous=previous,
                    )
                )
        return items

    @staticmethod
    def _load_previous(
        repo: Any, workspace_id: UUID, work: Mapping[str, Any], now: datetime
    ) -> PreviousEmail | None:
        position = int(work["step_position"])
        if position <= 1:
            return None
        row = repo.load_previous_email(
            workspace_id=workspace_id,
            enrollment_id=UUID(str(work["enrollment_id"])),
            before_position=position,
        )
        if row is None or not row["content_subject"]:
            return None
        facts = row["personalization_facts"] or []
        accepted_at = row["accepted_at"]
        days = max(0, (now - accepted_at).days) if accepted_at else 0
        return PreviousEmail(
            subject=str(row["content_subject"]),
            body_text=html_to_text(str(row["content_body_html"] or ""))[
                :_PREVIOUS_BODY_CHARS
            ],
            angle=row["angle"],
            fact_ids_used=tuple(
                str(f.get("id", "")) for f in facts if isinstance(f, dict)
            ),
            facts_used=tuple(
                str(f.get("text", "")) for f in facts if isinstance(f, dict)
            ),
            days_since_sent=days,
        )

    # ------------------------------------------------------------------
    # Model call (no transaction)
    # ------------------------------------------------------------------

    def _process(self, workspace_id: UUID, item: _Item, summary: ChunkSummary) -> None:
        work = item.work
        if not self._eligible_now(work):
            self._abort_ineligible(workspace_id, item, work, summary)
            return
        if work["campaign_status"] != "RUNNING" or work["planning_status"] != "READY":
            # Paused between the claim and now: don't spend a model call.
            self._defer_paused(workspace_id, item, summary)
            return
        if self._hold_back_until_lead_time(workspace_id, item, work, summary):
            return

        try:
            config = parse_config(work["personalization_config"])
        except ValidationError:
            config = None
        if config is None:
            self._fail(
                workspace_id, item, "objective_invalid", ["objective_invalid"], summary
            )
            return

        inputs = GenerationInputs(
            workspace_id=workspace_id,
            frozen_variables=work["frozen_variables"] or {},
            objective=config,
            reference_subject=work["email_subject"],
            reference_body_html=work["email_body_html"],
            reference_preheader=work["email_preheader"],
            step_position=int(work["step_position"]),
            previous=item.previous,
            retry_codes=item.retry_codes,
        )
        try:
            outcome = self._service.attempt(inputs)
        except ModelError as exc:
            self._handle_model_error(workspace_id, item, exc, summary)
            return

        if outcome.kind == "REJECTED":
            self._handle_rejected(workspace_id, item, outcome, summary)
        else:
            self._finalize(workspace_id, item, outcome, summary)

    @staticmethod
    def _eligible_now(work: Mapping[str, Any]) -> bool:
        return (
            work["message_status"] == "PLANNED"
            and work["enrollment_state"] == "ACTIVE"
            and str(work["next_step_id"]) == str(work["step_id"])
        )

    # ------------------------------------------------------------------
    # T2 variants
    # ------------------------------------------------------------------

    def _abort_ineligible(
        self,
        workspace_id: UUID,
        item: _Item,
        work: Mapping[str, Any],
        summary: ChunkSummary,
    ) -> None:
        with self._db.transaction() as repo:
            repo.finish_attempt(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                attempt_no=item.attempt_no,
                outcome="ABORTED_INELIGIBLE",
            )
            repo.refund_usage(
                workspace_id=workspace_id, day=self._clock().date(), kind="GENERATION"
            )
            repo.supersede_generation(
                workspace_id=workspace_id, generation_id=item.generation_id
            )
        summary.superseded += 1

    def _intended_due(
        self,
        repo: Any,
        workspace_id: UUID,
        work: Mapping[str, Any],
        planned_due: datetime | None,
    ) -> datetime | None:
        """When this follow-up should go out, given the waits as they stand NOW (the
        rule is shared with the post-resume re-time pass: follow_up_timing.py)."""
        if work.get("anchor_at") is None:
            return planned_due
        steps = repo.list_step_waits(
            workspace_id=workspace_id, sequence_id=work["sequence_id"]
        )
        return intended_follow_up_due(
            steps=steps,
            step_id=str(work["step_id"]),
            anchor_at=work["anchor_at"],
            planned_due=planned_due,
            wait_edited=wait_edited_after(
                steps, int(work["step_position"]), work.get("message_created_at")
            ),
            timezone=work["timezone"],
            weekday_set=int(work["weekday_set"]),
            window_start_local=work["window_start_local"],
            window_end_local=work["window_end_local"],
        )

    def _hold_back_until_lead_time(
        self,
        workspace_id: UUID,
        item: _Item,
        work: Mapping[str, Any],
        summary: ChunkSummary,
    ) -> bool:
        """The wait was made longer after this message was planned, so it is not yet
        time to write it: move the message to its new time and look at it again one
        lead time before that. No model call is spent. True when held back."""
        if work.get("anchor_at") is None:
            return False
        now = self._clock()
        with self._db.transaction() as repo:
            target = self._intended_due(repo, workspace_id, work, work["due_at"])
            lead = timedelta(seconds=self._config.lead_seconds)
            if target is None or target - lead <= now:
                return False
            repo.retime_planned_message(
                workspace_id=workspace_id, message_id=item.message_id, due_at=target
            )
            repo.finish_attempt(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                attempt_no=item.attempt_no,
                outcome="ABORTED_INELIGIBLE",
            )
            repo.refund_usage(
                workspace_id=workspace_id, day=now.date(), kind="GENERATION"
            )
            repo.release_claim(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                delay_seconds=max(1.0, (target - lead - now).total_seconds()),
                refund_attempt=True,
            )
        summary.deferred += 1
        return True

    def _defer_paused(
        self, workspace_id: UUID, item: _Item, summary: ChunkSummary
    ) -> None:
        with self._db.transaction() as repo:
            repo.finish_attempt(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                attempt_no=item.attempt_no,
                outcome="ABORTED_INELIGIBLE",
            )
            repo.refund_usage(
                workspace_id=workspace_id, day=self._clock().date(), kind="GENERATION"
            )
            repo.release_claim(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                delay_seconds=_PAUSED_DEFER_SECONDS,
                refund_attempt=True,
            )
        summary.deferred += 1

    def _handle_model_error(
        self, workspace_id: UUID, item: _Item, exc: ModelError, summary: ChunkSummary
    ) -> None:
        code = exc.code
        summary.count_code(code)
        if isinstance(exc, ModelPermanentError):
            # Auth/config/billing: retrying cannot help and an operator must act.
            # The job stays PENDING with a long delay and no attempt is burned.
            logger.error(
                "Personalization model configuration error",
                extra={"generation_id": str(item.generation_id), "code": code},
            )
            with self._db.transaction() as repo:
                repo.finish_attempt(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    attempt_no=item.attempt_no,
                    outcome="PROVIDER_ERROR",
                    failure_codes=[code],
                )
                repo.release_claim(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    delay_seconds=_PERMANENT_DEFER_SECONDS,
                    refund_attempt=True,
                    failure_codes=[code],
                )
            summary.permanent_errors += 1
            return

        if isinstance(exc, (MalformedOutput, ModelRefusal)):
            # The model answered, badly: a rejected (charged) attempt.
            self._reject(
                workspace_id,
                item,
                [code],
                summary,
                attempt_outcome="MALFORMED_OUTPUT",
            )
            return

        # Timeout / rate limit / 5xx / anything unclassified: transient. Does not
        # consume a quality attempt; bounded by max_transient_errors.
        new_transient = item.transient_error_count + 1
        outcome_name = "TIMEOUT" if isinstance(exc, ModelTimeout) else "PROVIDER_ERROR"
        if new_transient > self._config.max_transient_errors:
            with self._db.transaction() as repo:
                repo.finish_attempt(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    attempt_no=item.attempt_no,
                    outcome=outcome_name,
                    failure_codes=[code],
                )
                repo.fail_generation(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    message_id=item.message_id,
                    failure_code="provider_unavailable",
                    failure_codes=[code],
                )
            summary.failed += 1
            return
        delay = _backoff(
            _TRANSIENT_BACKOFF_BASE_SECONDS,
            _TRANSIENT_BACKOFF_MAX_SECONDS,
            new_transient,
        )
        if isinstance(exc, ModelRateLimited) and exc.retry_after_seconds:
            delay = max(
                delay, min(exc.retry_after_seconds, _TRANSIENT_BACKOFF_MAX_SECONDS)
            )
        with self._db.transaction() as repo:
            repo.finish_attempt(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                attempt_no=item.attempt_no,
                outcome=outcome_name,
                failure_codes=[code],
            )
            repo.release_claim(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                delay_seconds=delay,
                refund_attempt=True,
                transient_error=True,
                failure_codes=[code],
            )
        summary.transient_errors += 1

    def _handle_rejected(
        self,
        workspace_id: UUID,
        item: _Item,
        outcome: AttemptOutcome,
        summary: ChunkSummary,
    ) -> None:
        for code in outcome.failure_codes:
            summary.count_code(code)
        self._reject(
            workspace_id,
            item,
            list(outcome.failure_codes),
            summary,
            attempt_outcome="REJECTED_VALIDATION",
            outcome=outcome,
        )

    def _reject(
        self,
        workspace_id: UUID,
        item: _Item,
        codes: list[str],
        summary: ChunkSummary,
        *,
        attempt_outcome: str,
        outcome: AttemptOutcome | None = None,
    ) -> None:
        summary.rejected += 1
        exhausted = item.attempt_count >= item.max_attempts
        with self._db.transaction() as repo:
            repo.finish_attempt(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                attempt_no=item.attempt_no,
                outcome=attempt_outcome,
                failure_codes=codes,
                model=outcome.model if outcome else None,
                prompt_version=PROMPT_VERSION,
                input_tokens=outcome.input_tokens if outcome else None,
                output_tokens=outcome.output_tokens if outcome else None,
                latency_ms=outcome.latency_ms if outcome else None,
                context_digest=outcome.context_digest if outcome else None,
                output_digest=outcome.output_digest if outcome else None,
            )
            if outcome is not None:
                repo.add_tokens(
                    workspace_id=workspace_id,
                    day=self._clock().date(),
                    kind="GENERATION",
                    input_tokens=outcome.input_tokens,
                    output_tokens=outcome.output_tokens,
                )
            if exhausted:
                repo.fail_generation(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    message_id=item.message_id,
                    failure_code=(codes[0] if codes else "validation_failed"),
                    failure_codes=codes,
                )
            else:
                repo.release_claim(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    delay_seconds=_backoff(
                        _REJECT_BACKOFF_BASE_SECONDS,
                        _REJECT_BACKOFF_MAX_SECONDS,
                        item.attempt_count,
                    ),
                    refund_attempt=False,
                    failure_codes=codes,
                )
        if exhausted:
            summary.failed += 1

    def _fail(
        self,
        workspace_id: UUID,
        item: _Item,
        code: str,
        codes: list[str],
        summary: ChunkSummary,
    ) -> None:
        with self._db.transaction() as repo:
            repo.finish_attempt(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                attempt_no=item.attempt_no,
                outcome="ABORTED_INELIGIBLE",
                failure_codes=codes,
            )
            repo.fail_generation(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                message_id=item.message_id,
                failure_code=code,
                failure_codes=codes,
            )
        summary.failed += 1
        summary.count_code(code)

    def _finalize(
        self,
        workspace_id: UUID,
        item: _Item,
        outcome: AttemptOutcome,
        summary: ChunkSummary,
    ) -> None:
        now = self._clock()
        with self._db.transaction() as repo:
            locked = repo.lock_for_finalize(
                workspace_id=workspace_id, generation_id=item.generation_id
            )
            if (
                locked is None
                or locked["generation_state"] != "PENDING"
                or locked["message_status"] != "PLANNED"
                or locked["enrollment_state"] != "ACTIVE"
                or str(locked["next_step_id"]) != str(locked["step_id"])
                or repo.is_suppressed(
                    workspace_id=workspace_id,
                    address_id=UUID(str(locked["address_id"])),
                )
            ):
                repo.finish_attempt(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    attempt_no=item.attempt_no,
                    outcome="ABORTED_INELIGIBLE",
                )
                repo.refund_usage(
                    workspace_id=workspace_id, day=now.date(), kind="GENERATION"
                )
                repo.supersede_generation(
                    workspace_id=workspace_id, generation_id=item.generation_id
                )
                summary.superseded += 1
                return
            # The reference email was edited (while the campaign was paused, then
            # resumed) after this generation read it: what was written follows the
            # OLD reference. Throw it away and write it again from the new one.
            reference_changed = item.work.get("step_version") is not None and str(
                locked["step_version"]
            ) != str(item.work["step_version"])
            if (
                locked["campaign_status"] != "RUNNING"
                or locked["planning_status"] != "READY"
                or reference_changed
            ):
                # Paused (or otherwise not running), or the reference changed under
                # us: keep the job for later, and do not spend the attempt or the
                # budget unit.
                repo.finish_attempt(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    attempt_no=item.attempt_no,
                    outcome="ABORTED_INELIGIBLE",
                )
                repo.refund_usage(
                    workspace_id=workspace_id, day=now.date(), kind="GENERATION"
                )
                repo.release_claim(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    delay_seconds=1 if reference_changed else _PAUSED_DEFER_SECONDS,
                    refund_attempt=True,
                )
                summary.deferred += 1
                return

            work = item.work
            intended: datetime = (
                self._intended_due(repo, workspace_id, work, locked["due_at"]) or now
            )
            due_at = project_into_window(
                max(intended, now),
                timezone=work["timezone"],
                weekday_set=int(work["weekday_set"]),
                window_start_local=work["window_start_local"],
                window_end_local=work["window_end_local"],
            )
            wrote = repo.finalize_message(
                workspace_id=workspace_id,
                message_id=item.message_id,
                subject=outcome.subject,
                body_html=outcome.body_html,
                content_digest=outcome.content_digest,
                renderer_version=outcome.renderer_version,
                due_at=due_at,
            )
            if not wrote:
                repo.finish_attempt(
                    workspace_id=workspace_id,
                    generation_id=item.generation_id,
                    attempt_no=item.attempt_no,
                    outcome="ABORTED_INELIGIBLE",
                )
                repo.supersede_generation(
                    workspace_id=workspace_id, generation_id=item.generation_id
                )
                summary.superseded += 1
                return
            fallback = outcome.kind == "FALLBACK"
            repo.finish_attempt(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                attempt_no=item.attempt_no,
                outcome="FALLBACK" if fallback else "ACCEPTED",
                model=outcome.model,
                prompt_version=PROMPT_VERSION,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                latency_ms=outcome.latency_ms,
                context_digest=outcome.context_digest,
                output_digest=outcome.output_digest,
            )
            repo.add_tokens(
                workspace_id=workspace_id,
                day=now.date(),
                kind="GENERATION",
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
            )
            repo.complete_generation(
                workspace_id=workspace_id,
                generation_id=item.generation_id,
                state="SUCCEEDED",
                provider=self._service.provider,
                model=outcome.model or self._service.model_name,
                prompt_version=PROMPT_VERSION,
                context_digest=outcome.context_digest,
                fallback_used=fallback,
                facts=facts_payload(outcome.facts_used),
                angle=outcome.angle,
                research_status=outcome.research_status,
            )
        if fallback:
            summary.fallback += 1
        else:
            summary.generated += 1
