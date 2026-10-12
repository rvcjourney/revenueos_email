from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from app.modules.campaigns.follow_up_timing import intended_follow_up_due
from app.modules.personalization.version import CAMPAIGN_TYPE_HYPER

logger = logging.getLogger(__name__)


class RetimeRepository(Protocol):
    """What RetimeService needs from CampaignWorkerRepository."""

    def get_reconcile_context(
        self, *, workspace_id: UUID, campaign_id: UUID, generation: int
    ) -> Mapping[str, Any] | None: ...

    def list_sequence_steps(
        self, *, workspace_id: UUID, sequence_id: UUID
    ) -> Sequence[Mapping[str, Any]]: ...

    def fetch_unretimed_planned(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        limit: int,
        after_id: UUID | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def apply_retime(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        generation_id: UUID,
        due_at: datetime,
        next_attempt_at: datetime,
    ) -> bool: ...


@dataclass
class RetimeSummary:
    # False when the campaign is not a personalized one RUNNING at this generation
    # (paused again, resumed again, a standard campaign): nothing was touched.
    active: bool = True
    fetched: int = 0
    retimed: int = 0
    unchanged: int = 0
    last_id: UUID | None = None


class RetimeService:
    """After a resume, move the personalized follow-ups that are planned but not yet
    written to the time their wait implies NOW (ADR-0020 amendment 2).

    A planned personalized follow-up is a PLANNED message plus a generation job whose
    `next_attempt_at` (due time minus the lead time) was fixed when it was planned, and
    the writer only looks at a job once that time arrives. So a wait that was edited
    while the campaign was paused would have no effect on a follow-up that is days
    away unless someone moves it. Only follow-ups whose wait was edited after they
    were planned are selected (the database says so); the rest keep their time,
    including an overdue follow-up's spread-out slot.

    Level-triggered and idempotent: a processed job stops matching (its `updated_at`
    is bumped even when its time did not change), so a replayed or duplicated task
    converges without any stored state.
    """

    def __init__(self, repo: RetimeRepository, *, lead_seconds: int) -> None:
        self.repo = repo
        self.lead = timedelta(seconds=lead_seconds)

    def retime_batch(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        generation: int,
        limit: int,
        after_id: UUID | None = None,
    ) -> RetimeSummary:
        summary = RetimeSummary()
        ctx = self.repo.get_reconcile_context(
            workspace_id=workspace_id, campaign_id=campaign_id, generation=generation
        )
        if ctx is None or ctx.get("campaign_type") != CAMPAIGN_TYPE_HYPER:
            summary.active = False
            return summary

        rows = self.repo.fetch_unretimed_planned(
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            limit=limit,
            after_id=after_id,
        )
        summary.fetched = len(rows)
        if not rows:
            return summary
        summary.last_id = UUID(str(rows[-1]["generation_id"]))
        steps = list(
            self.repo.list_sequence_steps(
                workspace_id=workspace_id,
                sequence_id=UUID(str(ctx["activated_sequence_id"])),
            )
        )
        for row in rows:
            planned_due: datetime = row["due_at"]
            due = (
                intended_follow_up_due(
                    steps=steps,
                    step_id=str(row["step_id"]),
                    anchor_at=row["anchor_at"],
                    planned_due=planned_due,
                    # The database only returned rows whose wait was edited after they
                    # were planned.
                    wait_edited=True,
                    timezone=ctx["timezone"],
                    weekday_set=int(ctx["weekday_set"]),
                    window_start_local=ctx["window_start_local"],
                    window_end_local=ctx["window_end_local"],
                )
                or planned_due
            )
            self.repo.apply_retime(
                workspace_id=workspace_id,
                message_id=UUID(str(row["message_id"])),
                generation_id=UUID(str(row["generation_id"])),
                due_at=due,
                next_attempt_at=due - self.lead,
            )
            if due != planned_due:
                summary.retimed += 1
            else:
                summary.unchanged += 1
        return summary
