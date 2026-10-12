from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.core.errors import AppError
from app.modules.campaigns.message_rendering import render_step_content
from app.modules.campaigns.pacing import (
    MailboxPacing,
    SpreadPlanner,
    pacing_from_policies,
)
from app.modules.campaigns.progression import compute_due_at
from app.modules.campaigns.scheduling import WindowCalendar, project_into_window
from app.modules.personalization.version import CAMPAIGN_TYPE_HYPER

logger = logging.getLogger(__name__)


def wait_before_step(
    steps: Sequence[Mapping[str, Any]], step_id: str
) -> int | None:
    """Total WAIT minutes between the previous EMAIL step and `step_id`, or None
    when `step_id` is the first email (it has no previous email to wait for)."""
    ordered = sorted(steps, key=lambda s: s["position"])
    index = next((i for i, s in enumerate(ordered) if str(s["id"]) == step_id), None)
    if index is None:
        return None
    wait = 0
    for step in reversed(ordered[:index]):
        if step["kind"] == "WAIT":
            wait += int(step["wait_duration_minutes"] or 0)
            continue
        return wait
    return None


class ReconcileRepository(Protocol):
    """What ReconcileService needs from CampaignWorkerRepository."""

    def get_reconcile_context(
        self, *, workspace_id: UUID, campaign_id: UUID, generation: int
    ) -> Mapping[str, Any] | None: ...

    def list_sequence_steps(
        self, *, workspace_id: UUID, sequence_id: UUID
    ) -> Sequence[Mapping[str, Any]]: ...

    def list_eligible_campaign_mailboxes(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> Sequence[Mapping[str, Any]]: ...

    def list_mailbox_limit_policies(
        self, *, workspace_id: UUID, mailbox_ids: list[UUID]
    ) -> dict[UUID, list[dict[str, int]]]: ...

    def fetch_stale_messages_chunk(
        self, **kwargs: Any
    ) -> Sequence[Mapping[str, Any]]: ...

    def lock_campaign_for_planning(self, **kwargs: Any) -> None: ...

    def list_mailbox_unsent_hourly(
        self, **kwargs: Any
    ) -> dict[UUID, list[tuple[datetime, int]]]: ...

    def reschedule_message(self, **kwargs: Any) -> bool: ...


@dataclass
class ReconcileSummary:
    # False when the campaign is no longer RUNNING at this generation: the task
    # was superseded (paused again, resumed again) and must not touch anything.
    active: bool = True
    fetched: int = 0
    restamped: int = 0
    rerendered: int = 0
    spread: int = 0
    kept_future: int = 0
    skipped: int = 0
    last_due_at: datetime | None = None
    last_id: UUID | None = None
    offsets: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class ReconcileService:
    """Re-validates and re-times the unsent work of a campaign after a resume
    (ADR-0020).

    A resume bumps `campaigns.schedule_generation`; the scheduler only claims
    messages stamped with the campaign's generation, so every message that
    existed before the pause is held back until this pass has looked at it. Per
    message it keeps the guarantees the planner gave originally:

    * never earlier than the previous email's acceptance time plus its wait
      (waits keep elapsing while paused -- they are real elapsed time);
    * work that is still in the future keeps its time, re-projected into the
      campaign's *current* sending windows (a settings edit may have changed them);
    * work that became overdue during the pause is spread over the coming
      windows, one slot per message per mailbox, instead of becoming due at once.

    Level-triggered and idempotent: a message at the current generation is never
    selected again, so a replayed or duplicated task converges.
    """

    def __init__(
        self, repo: ReconcileRepository, *, default_pacing: MailboxPacing
    ) -> None:
        self.repo = repo
        self.default_pacing = default_pacing

    def reconcile_batch(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        generation: int,
        limit: int,
        after_due_at: datetime | None = None,
        after_id: UUID | None = None,
        # ignored; kept so already-queued tasks still run
        offsets: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> ReconcileSummary:
        summary = ReconcileSummary()
        ctx = self.repo.get_reconcile_context(
            workspace_id=workspace_id, campaign_id=campaign_id, generation=generation
        )
        if ctx is None:
            summary.active = False
            return summary

        now = now or datetime.now(UTC)
        steps = list(
            self.repo.list_sequence_steps(
                workspace_id=workspace_id,
                sequence_id=UUID(str(ctx["activated_sequence_id"])),
            )
        )
        window = {
            "timezone": ctx["timezone"],
            "weekday_set": int(ctx["weekday_set"]),
            "window_start_local": ctx["window_start_local"],
            "window_end_local": ctx["window_end_local"],
        }
        mailbox_ids = [
            UUID(str(r["mailbox_id"]))
            for r in self.repo.list_eligible_campaign_mailboxes(
                workspace_id=workspace_id, campaign_id=campaign_id
            )
        ]
        policies = self.repo.list_mailbox_limit_policies(
            workspace_id=workspace_id, mailbox_ids=mailbox_ids
        )
        pacing = {
            mailbox_id: pacing_from_policies(
                policies.get(mailbox_id, []), self.default_pacing
            )
            for mailbox_id in mailbox_ids
        }
        calendar = WindowCalendar(now, **window)
        planner: SpreadPlanner | None = None

        rows = self.repo.fetch_stale_messages_chunk(
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            generation=generation,
            limit=limit,
            after_due_at=after_due_at,
            after_id=after_id,
        )
        summary.fetched = len(rows)
        if rows:
            summary.last_due_at = rows[-1]["due_at"]
            summary.last_id = UUID(str(rows[-1]["id"]))

        for row in rows:
            message_id = UUID(str(row["id"]))
            mailbox_id = UUID(str(row["mailbox_id"]))
            try:
                base = self._lower_bound(row, steps, window)
                if base > now:
                    # Still in the future: keep its slot, re-projected into the
                    # current windows. Never earlier than `base`.
                    due = project_into_window(base, **window)
                    summary.kept_future += 1
                else:
                    if planner is None:
                        # Lock first, then read what is already planned: a progression
                        # sweep placing overdue follow-ups at the same moment must not
                        # read the same free slots (the lock serialises them).
                        self.repo.lock_campaign_for_planning(
                            workspace_id=workspace_id, campaign_id=campaign_id
                        )
                        planner = SpreadPlanner(
                            calendar,
                            mailbox_count=len(mailbox_ids),
                            campaign_daily_limit=ctx["daily_limit"],
                            pacing_by_mailbox=pacing,
                            default=self.default_pacing,
                            hourly_by_mailbox=self.repo.list_mailbox_unsent_hourly(
                                workspace_id=workspace_id,
                                campaign_id=campaign_id,
                                from_at=now,
                            ),
                        )
                    due = planner.place(mailbox_id, message_id)
                    summary.spread += 1
                due = max(due, base)
                if row["status"] == "RETRY_SCHEDULED" and row["next_retry_at"]:
                    due = max(due, row["next_retry_at"])
            except AppError:
                summary.skip("no_sending_window")
                continue

            content = (
                None
                if ctx.get("campaign_type") == CAMPAIGN_TYPE_HYPER
                else self._changed_content(row)
            )
            if self.repo.reschedule_message(
                workspace_id=workspace_id,
                message_id=message_id,
                expected_status=row["status"],
                due_at=due,
                generation=generation,
                content=content,
            ):
                summary.restamped += 1
                if content is not None:
                    summary.rerendered += 1
            else:
                summary.skip("concurrent_change")
        return summary

    @staticmethod
    def _changed_content(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
        """The re-rendered (subject, body, digest) when the step's content changed
        since this message was rendered, else None.

        Only a message that no provider was ever asked to send is eligible: no
        transport identity (rfc_message_id), never claimed, no live attempt. A
        message past that point keeps exactly what it was approved with. The
        digest is computed by the same function the send gate recomputes it with,
        so a re-rendered message always passes the gate's integrity check."""
        if (
            row["rfc_message_id"] is not None
            or row["dispatch_origin"] is not None
            or row["has_live_attempt"]
            or row["email_subject"] is None
            or int(row["renderer_version"]) != 1
        ):
            return None
        rendered = render_step_content(
            subject=row["email_subject"],
            body_html=row["email_body_html"] or "",
            preheader=row["email_preheader"],
            frozen_variables=row["frozen_variables"] or {},
            renderer_version=int(row["renderer_version"]),
        )
        if rendered.content_digest == row["content_digest"]:
            return None
        return rendered.subject, rendered.body_html, rendered.content_digest

    @staticmethod
    def _lower_bound(
        row: Mapping[str, Any],
        steps: Sequence[Mapping[str, Any]],
        window: Mapping[str, Any],
    ) -> datetime:
        """The earliest time this message may be sent. A scheduled follow-up is
        recomputed from the previous email's acceptance time and the *current*
        wait (the user may have edited it while paused); a first email or a
        retry keeps the time the planner/retry policy already chose."""
        due: datetime = row["due_at"]
        if row["status"] != "SCHEDULED" or row["anchor_at"] is None:
            return due
        wait = wait_before_step(steps, str(row["step_id"]))
        if wait is None:
            return due
        return compute_due_at(row["anchor_at"], wait, **window)
