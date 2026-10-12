from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)


class ReopenRepository(Protocol):
    def lock_campaign_for_reopen(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> Mapping[str, Any] | None: ...

    def fetch_reopen_candidates(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        limit: int,
        after_id: UUID | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    def reopen_enrollment(
        self,
        *,
        workspace_id: UUID,
        enrollment_id: UUID,
        step_id: UUID,
        position: int,
    ) -> bool: ...


@dataclass
class ReopenSummary:
    fetched: int = 0
    reopened: int = 0
    skipped_suppressed: int = 0
    campaign_not_eligible: bool = False
    last_enrollment_id: UUID | None = None


class ReopenService:
    """Opt-in at resume (ADR-0020): leads that FINISHED the sequence before the pause
    get the emails that were appended while it was paused.

    A COMPLETED enrollment is put back to ACTIVE pointing at the last email it was
    sent (a message that is already SENT), which is exactly the state progression
    expects of a lead waiting for a follow-up; progression then plans the appended
    email after the wait, like for any other lead. Only clean leads qualify
    (see `fetch_reopen_candidates`): a lead who replied, unsubscribed, bounced or
    complained, or whose last email did not reach the provider, is never reopened.

    Safe to repeat: the reopen is guarded on `state = COMPLETED`, so a replay finds
    nothing left to do, and a lead that was reopened and then progressed is no longer
    COMPLETED.
    """

    def __init__(
        self,
        repo: ReopenRepository,
        *,
        is_suppressed: Callable[[UUID, UUID], bool],
    ) -> None:
        self.repo = repo
        self._is_suppressed = is_suppressed

    def reopen_batch(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        generation: int,
        limit: int,
        after_id: UUID | None = None,
    ) -> ReopenSummary:
        summary = ReopenSummary()
        campaign = self.repo.lock_campaign_for_reopen(
            workspace_id=workspace_id, campaign_id=campaign_id
        )
        # Bound to the resume that asked for it: a later pause or resume supersedes it,
        # and only a STANDARD campaign has appended emails to send.
        if (
            campaign is None
            or campaign["status"] != "RUNNING"
            or campaign["campaign_type"] != "STANDARD"
            or int(campaign["schedule_generation"]) != generation
        ):
            summary.campaign_not_eligible = True
            return summary

        candidates = self.repo.fetch_reopen_candidates(
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            limit=limit,
            after_id=after_id,
        )
        summary.fetched = len(candidates)
        for row in candidates:
            summary.last_enrollment_id = UUID(str(row["enrollment_id"]))
            if self._is_suppressed(workspace_id, UUID(str(row["address_id"]))):
                summary.skipped_suppressed += 1
                continue
            if self.repo.reopen_enrollment(
                workspace_id=workspace_id,
                enrollment_id=UUID(str(row["enrollment_id"])),
                step_id=UUID(str(row["step_id"])),
                position=int(row["position"]),
            ):
                summary.reopened += 1
        return summary
