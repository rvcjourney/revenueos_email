from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.core.metrics import MetricsRegistry
from app.core.permissions import has_permission
from app.modules.campaigns.audience_service import AudienceService
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import AudienceSelectIn, SupplementOut

logger = logging.getLogger(__name__)


class SupplementService:
    """Adds leads to a PAUSED, activated campaign (ADR-0020).

    The activated audience is never touched. New leads are captured as a separate
    SUPPLEMENT audience with the existing capture machinery (minus anyone already
    enrolled, suppressed or archived), reviewed by the user, then enrolled by the
    same planner that enrolled the original audience, under its own planning jobs.
    Resume waits for those jobs, so a half-enrolled supplement never starts sending.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)
        self.audiences = AudienceService(session)

    # ------------------------------------------------------------------ guards

    def _paused_campaign(
        self, context: WorkspaceContext, campaign_id: UUID, *, lock: bool = False
    ) -> RowMapping:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if not has_permission(context.role_code, "campaigns.execute"):
            raise AppError(
                "forbidden",
                "Only a manager can add leads to a paused campaign",
                status_code=403,
            )
        if campaign["status"] != "PAUSED" or campaign["activation_id"] is None:
            raise AppError(
                "state_conflict",
                "Leads can only be added to a campaign while it is paused",
                status_code=409,
            )
        if lock:
            locked = self.repo.get_campaign(
                workspace_id=context.workspace_id,
                campaign_id=campaign_id,
                for_update=True,
            )
            # The state may have moved between the read and the lock (a resume
            # that won the race): re-check against the locked row.
            if (
                locked is None
                or locked["status"] != "PAUSED"
                or locked["activation_id"] is None
            ):
                raise AppError(
                    "state_conflict",
                    "Leads can only be added to a campaign while it is paused",
                    status_code=409,
                )
            return locked
        return campaign

    # ------------------------------------------------------------------ reads

    def list_supplements(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> list[SupplementOut]:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        jobs = self._jobs_by_audience(context, campaign_id)
        return [
            self._to_out(context, audience, jobs.get(str(audience["id"]), {}))
            for audience in self.repo.list_supplement_audiences(
                workspace_id=context.workspace_id, campaign_id=campaign_id
            )
        ]

    # ------------------------------------------------------------------ commands

    def select(
        self, context: WorkspaceContext, campaign_id: UUID, payload: AudienceSelectIn
    ) -> SupplementOut:
        self._paused_campaign(context, campaign_id)
        captured = self.audiences.start_capture(
            context, campaign_id, payload, kind="SUPPLEMENT"
        )
        audience = self.repo.get_audience(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            audience_id=captured.id,
        )
        assert audience is not None
        return self._to_out(
            context, audience, {}, total_candidates=captured.total_candidates
        )

    def enroll(
        self, context: WorkspaceContext, campaign_id: UUID, audience_id: UUID
    ) -> SupplementOut:
        campaign = self._paused_campaign(context, campaign_id, lock=True)
        audience = self.repo.get_audience(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            audience_id=audience_id,
        )
        if audience is None or audience["kind"] != "SUPPLEMENT":
            raise AppError("not_found", "Audience revision not found", status_code=404)

        jobs = self._jobs_by_audience(context, campaign_id).get(str(audience_id), {})
        if "ENROLL" in jobs:
            # A repeat (double click, retried request) reports what already runs.
            return self._to_out(context, audience, jobs)
        if audience["status"] != "READY":
            raise AppError(
                "state_conflict",
                "These leads are still being checked. Try again in a moment.",
                status_code=409,
            )
        counts = self.repo.get_audience_member_counts(
            workspace_id=context.workspace_id, audience_id=audience_id
        )
        accepted = int(counts["accepted"])
        if accepted == 0:
            raise AppError(
                "validation_error",
                "None of the selected leads can be added: they are already in "
                "this campaign, suppressed or archived.",
                status_code=422,
            )

        activation_id = UUID(str(campaign["activation_id"]))
        sequence_id = UUID(str(campaign["activated_sequence_id"]))
        for phase in ("ENROLL", "RENDER"):
            self.repo.insert_planning_job(
                workspace_id=context.workspace_id,
                campaign_id=campaign_id,
                activation_id=activation_id,
                sequence_id=sequence_id,
                phase=phase,
                total_count=accepted,
                audience_id=audience_id,
            )
        self.repo.record_campaign_audit(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            action="campaign.leads_added_while_paused",
            campaign_id=campaign_id,
            after_state={"audience_id": str(audience_id), "accepted": accepted},
        )
        # The worker claims the job by reading it, so commit before dispatching.
        self.session.commit()
        logger.info(
            "CAMPAIGN_LEADS_ADDED workspace=%s campaign=%s audience=%s accepted=%d",
            context.workspace_id,
            campaign_id,
            audience_id,
            accepted,
        )
        MetricsRegistry.get().increment("campaign_supplement_enroll_started_total")
        self._dispatch_enroll(
            context.workspace_id, campaign_id, activation_id, audience_id
        )

        refreshed = self._jobs_by_audience(context, campaign_id).get(
            str(audience_id), {}
        )
        return self._to_out(context, audience, refreshed)

    def retry(
        self, context: WorkspaceContext, campaign_id: UUID, audience_id: UUID
    ) -> SupplementOut:
        """Re-run this supplement's FAILED enroll/render job (cursor kept)."""
        campaign = self._paused_campaign(context, campaign_id)
        audience = self.repo.get_audience(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            audience_id=audience_id,
        )
        if audience is None or audience["kind"] != "SUPPLEMENT":
            raise AppError("not_found", "Audience revision not found", status_code=404)
        jobs = self._jobs_by_audience(context, campaign_id).get(str(audience_id), {})
        if not any(job["state"] == "FAILED" for job in jobs.values()):
            # Nothing failed (already retried, or never started): report what is.
            return self._to_out(context, audience, jobs)
        self.repo.record_campaign_audit(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            action="campaign.supplement_retry",
            campaign_id=campaign_id,
            after_state={"audience_id": str(audience_id)},
        )
        self.session.commit()
        from app.services.task_dispatch import get_task_producer

        get_task_producer().send_task(
            "campaigns.retry_planning_job",
            kwargs={
                "workspace_id": str(context.workspace_id),
                "campaign_id": str(campaign_id),
                "activation_id": str(campaign["activation_id"]),
                "audience_id": str(audience_id),
            },
            queue="campaigns",
        )
        MetricsRegistry.get().increment("campaign_supplement_retry_total")
        refreshed = self._jobs_by_audience(context, campaign_id).get(
            str(audience_id), {}
        )
        return self._to_out(context, audience, refreshed)

    # ------------------------------------------------------------------ helpers

    def _dispatch_enroll(
        self,
        workspace_id: UUID,
        campaign_id: UUID,
        activation_id: UUID,
        audience_id: UUID,
    ) -> None:
        from app.services.task_dispatch import get_task_producer

        get_task_producer().send_task(
            "campaigns.enroll_activation_chunk",
            kwargs={
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "activation_id": str(activation_id),
                "audience_id": str(audience_id),
            },
            queue="campaigns",
        )

    def _jobs_by_audience(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> dict[str, dict[str, Mapping[str, Any]]]:
        by_audience: dict[str, dict[str, Mapping[str, Any]]] = {}
        jobs: Sequence[RowMapping] = self.repo.list_supplement_jobs(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        for job in jobs:
            phases = by_audience.setdefault(str(job["audience_id"]), {})
            phases[job["phase"]] = dict(job)
        return by_audience

    def _to_out(
        self,
        context: WorkspaceContext,
        audience: RowMapping,
        jobs: Mapping[str, Mapping[str, Any]],
        *,
        total_candidates: int | None = None,
    ) -> SupplementOut:
        audience_id = UUID(str(audience["id"]))
        capture = self.repo.get_capture_job_for_audience(
            workspace_id=context.workspace_id, audience_id=audience_id
        )
        counts = None
        exclusions: dict[str, int] = {}
        if audience["status"] in ("READY", "FAILED"):
            counts = self.repo.get_audience_member_counts(
                workspace_id=context.workspace_id, audience_id=audience_id
            )
            exclusions = dict(
                self.repo.get_audience_exclusion_reason_counts(
                    workspace_id=context.workspace_id, audience_id=audience_id
                )
            )
        enroll = jobs.get("ENROLL")
        render = jobs.get("RENDER")
        return SupplementOut(
            id=audience_id,
            campaign_id=audience["campaign_id"],
            revision=audience["revision"],
            status=audience["status"],
            started_at=audience["started_at"],
            completed_at=audience["completed_at"],
            total_candidates=(
                capture["total_count"] if capture else total_candidates
            ),
            processed_count=capture["processed_count"] if capture else 0,
            accepted_count=counts["accepted"] if counts else None,
            excluded_count=counts["excluded"] if counts else None,
            exclusions={str(k): int(v) for k, v in exclusions.items() if k},
            enroll_state=enroll["state"] if enroll else "NOT_STARTED",
            render_state=render["state"] if render else "NOT_STARTED",
            enrolled_count=int(enroll["processed_count"]) if enroll else 0,
            error_reason=(
                (capture["error_reason"] if capture else None)
                or (enroll["error_reason"] if enroll else None)
                or (render["error_reason"] if render else None)
            ),
        )
