from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from app.core.errors import AppError
from app.modules.campaigns.message_rendering import render_step_content, reply_subject
from app.modules.campaigns.pacing import (
    MailboxPacing,
    SpreadPlanner,
    pacing_from_policies,
)
from app.modules.campaigns.scheduling import WindowCalendar, project_into_window
from app.modules.personalization.version import CAMPAIGN_TYPE_HYPER

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NextEmailPlan:
    """The next EMAIL step for an enrollment and the total wait before it."""

    step: Mapping[str, Any]
    wait_minutes: int


def plan_next_email(
    steps: Sequence[Mapping[str, Any]], current_position: int
) -> NextEmailPlan | None:
    """The first EMAIL step after `current_position`, with the WAIT steps
    between them summed. None when no email follows (the sequence is finished).

    Pure: no I/O, so the timing rules can be tested exhaustively. A wait is an
    elapsed duration (CAMPAIGN_ENGINE.md: a day is 24 elapsed hours), so the
    waits are simply added.
    """
    wait_minutes = 0
    for step in sorted(steps, key=lambda s: s["position"]):
        if step["position"] <= current_position:
            continue
        if step["kind"] == "WAIT":
            wait_minutes += int(step["wait_duration_minutes"] or 0)
            continue
        return NextEmailPlan(step=step, wait_minutes=wait_minutes)
    return None


def compute_due_at(
    accepted_at: datetime,
    wait_minutes: int,
    *,
    timezone: str,
    weekday_set: int,
    window_start_local: Any,
    window_end_local: Any,
) -> datetime:
    """Earliest send time for a follow-up: the previous message's provider
    acceptance time plus the wait, moved into the next allowed sending window.

    Anchoring on the persisted acceptance time (not queue or first-attempt
    time) is the documented rule; the send gates re-check limits later.
    """
    return project_into_window(
        accepted_at + timedelta(minutes=wait_minutes),
        timezone=timezone,
        weekday_set=weekday_set,
        window_start_local=window_start_local,
        window_end_local=window_end_local,
    )


class ProgressionRepository(Protocol):
    """What ProgressionService needs from CampaignWorkerRepository."""

    def get_progression_context(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> Mapping[str, Any] | None: ...

    def list_sequence_steps(
        self, *, workspace_id: UUID, sequence_id: UUID
    ) -> Sequence[Mapping[str, Any]]: ...

    def fetch_progressable_enrollments(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        limit: int,
        after_id: UUID | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def insert_followup_message(self, **kwargs: Any) -> UUID | None: ...

    def render_message(self, **kwargs: Any) -> None: ...

    def mark_generation_pending(self, **kwargs: Any) -> bool: ...

    def advance_enrollment(self, **kwargs: Any) -> bool: ...

    def complete_enrollment(self, **kwargs: Any) -> bool: ...

    def fail_enrollment(self, **kwargs: Any) -> bool: ...

    # Only used when overdue follow-ups are spread (ProgressionService with pacing).
    def lock_campaign_for_planning(self, **kwargs: Any) -> None: ...

    def list_eligible_campaign_mailboxes(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> Sequence[Mapping[str, Any]]: ...

    def list_mailbox_limit_policies(
        self, *, workspace_id: UUID, mailbox_ids: list[UUID]
    ) -> dict[UUID, list[dict[str, int]]]: ...

    def list_mailbox_unsent_hourly(
        self, **kwargs: Any
    ) -> dict[UUID, list[tuple[datetime, int]]]: ...


@dataclass
class ProgressionSummary:
    fetched: int = 0
    advanced: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    # Keyset cursor for the next chunk of the same sweep: rows that are skipped
    # stay eligible, so a chunk must be able to continue past them.
    last_enrollment_id: UUID | None = None
    reasons: dict[str, int] = field(default_factory=dict)
    spread: int = 0

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class ProgressionService:
    """Creates the next email for enrollments whose current email was accepted.

    Level-triggered: it looks at persisted state ("current message is SENT but
    the enrollment still points at it"), not at events, so it does not matter
    which path recorded the acceptance (send worker, reconciliation, sync) and a
    missed run only delays progression to the next run. Everything for one
    enrollment -- the new message, its rendering and the pointer move -- happens
    in the caller's single transaction, so a crash replays cleanly, and the
    unique (enrollment, step) message key stops a race from creating two.

    Pause, reply, unsubscribe and suppression are honoured without special
    cases here: a campaign that is not RUNNING is not selected, a STOPPED
    enrollment is not ACTIVE, and the send gates remain the authoritative
    check when a scheduled follow-up comes due.
    """

    def __init__(
        self,
        repo: ProgressionRepository,
        is_suppressed: Callable[[UUID, UUID], bool],
        *,
        personalization_lead_seconds: int = 3600,
        personalization_max_attempts: int = 3,
        default_pacing: MailboxPacing | None = None,
        overdue_grace: timedelta = timedelta(minutes=2),
    ) -> None:
        self.repo = repo
        self.is_suppressed = is_suppressed
        # With pacing, a follow-up whose time already passed (typically because the
        # campaign was paused longer than its wait) is spread over the next sending
        # windows, per mailbox, instead of everything becoming due at once. Without
        # it (None) behaviour is exactly as before. A follow-up that is only a little
        # late -- normal sweep lag -- keeps its time: `overdue_grace`.
        self.default_pacing = default_pacing
        self.overdue_grace = overdue_grace
        # Only used for hyper-personalized campaigns (ADR-0011).
        self.personalization_lead_seconds = personalization_lead_seconds
        self.personalization_max_attempts = personalization_max_attempts

    def advance_batch(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        limit: int,
        after_id: UUID | None = None,
        now: datetime | None = None,
    ) -> ProgressionSummary:
        summary = ProgressionSummary()
        now = now or datetime.now(UTC)
        planner: SpreadPlanner | None = None
        ctx = self.repo.get_progression_context(
            workspace_id=workspace_id, campaign_id=campaign_id
        )
        if ctx is None:
            # Not RUNNING/READY (paused, completed, superseded): nothing to plan.
            return summary

        steps = list(
            self.repo.list_sequence_steps(
                workspace_id=workspace_id,
                sequence_id=UUID(str(ctx["activated_sequence_id"])),
            )
        )
        steps_by_id = {str(s["id"]): s for s in steps}
        rows = self.repo.fetch_progressable_enrollments(
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            limit=limit,
            after_id=after_id,
        )
        summary.fetched = len(rows)
        if rows:
            summary.last_enrollment_id = UUID(str(rows[-1]["enrollment_id"]))

        for row in rows:
            enrollment_id = UUID(str(row["enrollment_id"]))
            current = steps_by_id.get(str(row["next_step_id"]))
            if current is None:
                summary.skip("unknown_current_step")
                continue

            if row["message_status"] == "FAILED":
                # The email could not be sent for good: no next step.
                if self.repo.fail_enrollment(
                    workspace_id=workspace_id,
                    enrollment_id=enrollment_id,
                    expected_step_id=UUID(str(current["id"])),
                ):
                    summary.failed += 1
                else:
                    summary.skip("concurrent_change")
                continue

            accepted_at: datetime = row["accepted_at"]
            plan = plan_next_email(steps, int(current["position"]))
            if plan is None:
                if self.repo.complete_enrollment(
                    workspace_id=workspace_id,
                    enrollment_id=enrollment_id,
                    expected_step_id=UUID(str(current["id"])),
                    accepted_at=accepted_at,
                ):
                    summary.completed += 1
                else:
                    summary.skip("concurrent_change")
                continue

            address_id = UUID(str(row["address_id"]))
            # Courtesy check only (the send gate is authoritative): don't build
            # a message for an address that is already suppressed.
            if self.is_suppressed(workspace_id, address_id):
                summary.skip("suppressed")
                continue
            if row["assigned_mailbox_id"] is None or not row["sender_address"]:
                summary.skip("no_sender")
                continue

            try:
                due_at = compute_due_at(
                    accepted_at,
                    plan.wait_minutes,
                    timezone=ctx["timezone"],
                    weekday_set=ctx["weekday_set"],
                    window_start_local=ctx["window_start_local"],
                    window_end_local=ctx["window_end_local"],
                )
            except AppError:
                logger.warning(
                    "No sending window for follow-up",
                    extra={"campaign_id": str(campaign_id)},
                )
                summary.skip("no_sending_window")
                continue

            if self.default_pacing is not None and due_at < now - self.overdue_grace:
                if planner is None:
                    planner = self._spread_planner(workspace_id, campaign_id, ctx, now)
                # Never earlier than accepted_at + wait: spreading only moves later.
                due_at = max(
                    planner.place(
                        UUID(str(row["assigned_mailbox_id"])),
                        f"{enrollment_id}:{plan.step['id']}",
                    ),
                    due_at,
                )
                summary.spread += 1

            hyper = ctx.get("campaign_type") == CAMPAIGN_TYPE_HYPER
            # ADR-0021: the parent is the email the lead was just sent (the row's
            # SENT message). A reply must come from the same mailbox; if the lead
            # was moved to another one it goes out unthreaded with the same subject.
            # Personalized follow-ups store it too: the writer takes the subject
            # from it.
            threaded = bool(plan.step.get("email_same_thread"))
            reply_to: UUID | None = None
            if threaded and str(row["message_mailbox_id"]) == str(
                row["assigned_mailbox_id"]
            ):
                reply_to = UUID(str(row["message_id"]))
            elif threaded:
                logger.info(
                    "Same-thread follow-up planned unthreaded: mailbox changed",
                    extra={"enrollment_id": str(enrollment_id)},
                )
            rendered = (
                None
                if hyper
                else render_step_content(
                    subject=plan.step["email_subject"] or "",
                    body_html=plan.step["email_body_html"],
                    preheader=plan.step.get("email_preheader"),
                    frozen_variables=row["frozen_variables"] or {},
                    renderer_version=1,
                    thread_subject=(
                        reply_subject(row["message_subject"] or "")
                        if threaded
                        else None
                    ),
                )
            )
            message_id = self.repo.insert_followup_message(
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                sequence_id=UUID(str(ctx["activated_sequence_id"])),
                step_id=UUID(str(plan.step["id"])),
                enrollment_id=enrollment_id,
                mailbox_id=UUID(str(row["assigned_mailbox_id"])),
                address_id=address_id,
                schedule_generation=int(ctx["schedule_generation"]),
                reply_to_message_id=reply_to,
            )
            if message_id is not None and hyper:
                # The follow-up is written just in time by the personalization
                # worker (ADR-0011); it stays PLANNED with its intended time.
                self.repo.mark_generation_pending(
                    workspace_id=workspace_id,
                    campaign_id=campaign_id,
                    sequence_id=UUID(str(ctx["activated_sequence_id"])),
                    step_id=UUID(str(plan.step["id"])),
                    enrollment_id=enrollment_id,
                    message_id=message_id,
                    due_at=due_at,
                    anchor_at=accepted_at,
                    next_attempt_at=due_at
                    - timedelta(seconds=self.personalization_lead_seconds),
                    max_attempts=self.personalization_max_attempts,
                )
            elif message_id is not None:
                assert rendered is not None
                self.repo.render_message(
                    workspace_id=workspace_id,
                    message_id=message_id,
                    content_subject=rendered.subject,
                    content_body_html=rendered.body_html,
                    content_digest=rendered.content_digest,
                    frozen_destination=row["frozen_destination"],
                    frozen_sender_address=row["sender_address"],
                    frozen_sender_name=row["sender_name"],
                    due_at=due_at,
                    anchor_at=accepted_at,
                )
            # A None message_id means a message for this step already exists
            # (an earlier run got that far); still move the pointer.
            if self.repo.advance_enrollment(
                workspace_id=workspace_id,
                enrollment_id=enrollment_id,
                expected_step_id=UUID(str(current["id"])),
                next_step_id=UUID(str(plan.step["id"])),
                next_position=int(plan.step["position"]),
                accepted_at=accepted_at,
            ):
                summary.advanced += 1
            else:
                summary.skip("concurrent_change")
        return summary

    def _spread_planner(
        self,
        workspace_id: UUID,
        campaign_id: UUID,
        ctx: Mapping[str, Any],
        now: datetime,
    ) -> SpreadPlanner:
        """The planner for this batch's overdue follow-ups. Built (and the campaign
        row locked, so a concurrent sweep or reconcile cannot read the same free
        slots) only when a batch actually has overdue work."""
        assert self.default_pacing is not None
        self.repo.lock_campaign_for_planning(
            workspace_id=workspace_id, campaign_id=campaign_id
        )
        mailbox_ids = [
            UUID(str(r["mailbox_id"]))
            for r in self.repo.list_eligible_campaign_mailboxes(
                workspace_id=workspace_id, campaign_id=campaign_id
            )
        ]
        policies = self.repo.list_mailbox_limit_policies(
            workspace_id=workspace_id, mailbox_ids=mailbox_ids
        )
        return SpreadPlanner(
            WindowCalendar(
                now,
                timezone=ctx["timezone"],
                weekday_set=int(ctx["weekday_set"]),
                window_start_local=ctx["window_start_local"],
                window_end_local=ctx["window_end_local"],
            ),
            mailbox_count=len(mailbox_ids),
            campaign_daily_limit=ctx.get("daily_limit"),
            pacing_by_mailbox={
                mailbox_id: pacing_from_policies(
                    policies.get(mailbox_id, []), self.default_pacing
                )
                for mailbox_id in mailbox_ids
            },
            default=self.default_pacing,
            hourly_by_mailbox=self.repo.list_mailbox_unsent_hourly(
                workspace_id=workspace_id, campaign_id=campaign_id, from_at=now
            ),
        )
