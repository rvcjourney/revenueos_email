from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.config import Settings
from app.core.errors import AppError
from app.core.metrics import MetricsRegistry
from app.core.permissions import has_permission
from app.modules.campaigns.message_rendering import compute_sequence_content_digest
from app.modules.campaigns.preflight import PreflightService
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import (
    ActivateIn,
    CampaignDetailOut,
    CampaignPlanningOut,
    PauseIn,
    PlanningJobStatusOut,
    ResumeIn,
)
from app.modules.personalization.version import (
    CAMPAIGN_TYPE_HYPER,
    CAMPAIGN_TYPE_STANDARD,
)

logger = logging.getLogger(__name__)

_ACTIVATE_OPERATION = "campaign.activate"


def _hash_payload(data: dict[str, object]) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class CampaignActivationService:
    """Owns every transition that moves a campaign off DRAFT: activate,
    pause, resume. No other service is permitted to write campaigns.status
    except archiving (CampaignService.archive_campaign), which the database
    guard and the repository both refuse for RUNNING campaigns."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)

    def activate(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: ActivateIn,
        idempotency_key: str,
    ) -> CampaignDetailOut:
        if payload.start_at is not None and payload.start_at.tzinfo is None:
            raise AppError(
                "validation_error",
                "start_at must include a UTC offset",
                status_code=422,
            )

        payload_hash = _hash_payload(
            {
                "expected_version": payload.expected_version,
                "start_at": _iso_or_none(payload.start_at),
            }
        )
        receipt = self._resolve_command_receipt(
            context, campaign_id, idempotency_key, payload_hash
        )
        if receipt["status"] == "COMPLETED":
            existing = self.repo.get_campaign(
                workspace_id=context.workspace_id, campaign_id=campaign_id
            )
            if existing is None:
                raise AppError("not_found", "Campaign not found", status_code=404)
            return self._to_detail_out(existing)
        receipt_id = UUID(str(receipt["id"]))

        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            for_update=True,
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if campaign["status"] != "DRAFT":
            raise AppError(
                "state_conflict",
                "Only a DRAFT campaign can be activated",
                status_code=409,
            )
        if campaign["version"] != payload.expected_version:
            raise AppError(
                "conflict",
                "Campaign was modified by another user. Please refresh and try again.",
                status_code=409,
            )

        preflight = PreflightService(self.session).run(context, campaign_id)
        if not preflight.ready:
            raise AppError(
                "preflight_failed",
                "Campaign is not ready to activate",
                status_code=422,
                details=preflight.model_dump(mode="json"),
            )

        now = datetime.now(UTC)
        if payload.start_at is None or payload.start_at <= now:
            effective_start_at = now
            target_status = "RUNNING"
        else:
            effective_start_at = payload.start_at
            target_status = "SCHEDULED"

        sequence_id = campaign["draft_sequence_id"]
        if sequence_id is None:
            raise AppError(
                "internal_error",
                "Campaign has no sequence to activate",
                status_code=500,
            )
        audience_id = campaign["draft_audience_id"]
        if audience_id is None:
            raise AppError(
                "internal_error",
                "Campaign has no committed audience",
                status_code=500,
            )
        sequence_id = UUID(str(sequence_id))
        audience_id = UUID(str(audience_id))

        steps = self.repo.list_steps_ordered(
            workspace_id=context.workspace_id, sequence_id=sequence_id
        )
        attachments_by_step: dict[str, list[dict]] = {}
        for attachment in self.repo.list_sequence_attachments(
            workspace_id=context.workspace_id, sequence_id=sequence_id
        ):
            attachments_by_step.setdefault(str(attachment["step_id"]), []).append(
                dict(attachment)
            )
        sequence_row = self.repo.get_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        # The objective is frozen with the reference templates (ADR-0011); only
        # hyper-personalized sequences carry one, so standard digests are unchanged.
        objective = (
            dict(sequence_row["personalization_config"])
            if campaign.get("campaign_type") == CAMPAIGN_TYPE_HYPER
            and sequence_row is not None
            and sequence_row.get("personalization_config")
            else None
        )
        content_digest = compute_sequence_content_digest(
            [
                {**dict(step), "attachments": attachments_by_step.get(str(step["id"]))}
                for step in steps
            ],
            personalization_config=objective,
        )
        frozen_sequence = self.repo.freeze_sequence(
            workspace_id=context.workspace_id,
            sequence_id=sequence_id,
            content_digest=content_digest,
        )
        if frozen_sequence is None:
            raise AppError(
                "conflict",
                "Sequence was concurrently modified. Please refresh and try again.",
                status_code=409,
            )

        activation_id = uuid.uuid4()
        activated = self.repo.activate_campaign(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
            activation_id=activation_id,
            activated_sequence_id=sequence_id,
            activated_audience_id=audience_id,
            start_at=effective_start_at,
            target_status=target_status,
            actor_id=context.user_id,
        )
        if activated is None:
            raise AppError(
                "conflict",
                "Campaign was concurrently activated. Please refresh and try again.",
                status_code=409,
            )

        counts = self.repo.get_audience_member_counts(
            workspace_id=context.workspace_id, audience_id=audience_id
        )
        total_count = counts["accepted"]

        for phase in ("ENROLL", "RENDER"):
            self.repo.insert_planning_job(
                workspace_id=context.workspace_id,
                campaign_id=campaign_id,
                activation_id=activation_id,
                sequence_id=sequence_id,
                phase=phase,
                total_count=total_count,
            )

        self.repo.complete_command_receipt(
            workspace_id=context.workspace_id,
            receipt_id=receipt_id,
            response_version=int(activated["version"]),
        )

        self.session.commit()

        self._dispatch_enroll_task(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            activation_id=activation_id,
        )

        return self._to_detail_out(activated)

    def pause(
        self, context: WorkspaceContext, campaign_id: UUID, payload: PauseIn
    ) -> CampaignDetailOut:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            for_update=True,
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        # Idempotent: a repeat (double click, retried request after a lost
        # response) reports the state it already reached. Checked before the
        # version so a stale tab cannot turn a no-op into a conflict.
        if campaign["status"] == "PAUSED":
            return self._to_detail_out(
                campaign,
                outstanding_attempts=self.repo.count_outstanding_attempts(
                    workspace_id=context.workspace_id, campaign_id=campaign_id
                ),
            )
        if campaign["status"] not in ("RUNNING", "SCHEDULED"):
            raise AppError(
                "state_conflict",
                "Only a RUNNING or SCHEDULED campaign can be paused",
                status_code=409,
            )
        if campaign["version"] != payload.expected_version:
            raise AppError(
                "conflict",
                "Campaign was modified by another user. Please refresh and try again.",
                status_code=409,
            )
        updated = self.repo.pause_campaign(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
            actor_id=context.user_id,
            previous_status=campaign["status"],
        )
        if updated is None:
            raise AppError(
                "conflict",
                "Campaign was concurrently updated. Please refresh and try again.",
                status_code=409,
            )
        outstanding = self.repo.count_outstanding_attempts(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        self.session.commit()
        logger.info(
            "CAMPAIGN_PAUSED workspace=%s campaign=%s from=%s outstanding_attempts=%d",
            context.workspace_id,
            campaign_id,
            campaign["status"],
            outstanding,
        )
        MetricsRegistry.get().increment("campaign_paused_total")
        return self._to_detail_out(updated, outstanding_attempts=outstanding)

    def resume(
        self, context: WorkspaceContext, campaign_id: UUID, payload: ResumeIn
    ) -> CampaignDetailOut:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            for_update=True,
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        # Idempotent, see pause(). Never re-bumps the generation.
        if campaign["status"] in ("RUNNING", "SCHEDULED"):
            return self._to_detail_out(campaign)
        if campaign["status"] != "PAUSED":
            raise AppError(
                "state_conflict",
                "Only a PAUSED campaign can be resumed",
                status_code=409,
            )
        if campaign["version"] != payload.expected_version:
            raise AppError(
                "conflict",
                "Campaign was modified by another user. Please refresh and try again.",
                status_code=409,
            )

        preflight = PreflightService(self.session).run_for_resume(context, campaign)
        if not preflight.ready:
            logger.warning(
                "CAMPAIGN_RESUME_FAILED workspace=%s campaign=%s reasons=%s",
                context.workspace_id,
                campaign_id,
                ",".join(issue.code for issue in preflight.errors),
            )
            MetricsRegistry.get().increment("campaign_resume_blocked_total")
            raise AppError(
                "resume_blocked",
                "This campaign can't resume yet. Fix the listed problems first.",
                status_code=422,
                details=preflight.model_dump(mode="json"),
            )

        start_at = campaign["start_at"]
        now = self.repo.database_now()
        target_status = (
            "RUNNING" if start_at is None or start_at <= now else "SCHEDULED"
        )
        if payload.include_completed and (
            target_status != "RUNNING" or campaign.get("campaign_type") != "STANDARD"
        ):
            raise AppError(
                "validation_error",
                "Finished leads can only be given the new emails when a standard "
                "campaign resumes straight to running.",
                status_code=422,
            )

        updated = self.repo.resume_campaign(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
            target_status=target_status,
            actor_id=context.user_id,
        )
        if updated is None:
            raise AppError(
                "conflict",
                "Campaign was concurrently updated. Please refresh and try again.",
                status_code=409,
            )
        self.session.commit()
        logger.info(
            "CAMPAIGN_RESUMED workspace=%s campaign=%s to=%s generation=%s",
            context.workspace_id,
            campaign_id,
            target_status,
            updated["schedule_generation"],
        )
        MetricsRegistry.get().increment("campaign_resumed_total")
        if target_status == "RUNNING":
            self._dispatch_reconcile_task(
                workspace_id=context.workspace_id,
                campaign_id=campaign_id,
                generation=int(updated["schedule_generation"]),
            )
            if payload.include_completed:
                self._dispatch_reopen_task(
                    context,
                    campaign_id,
                    generation=int(updated["schedule_generation"]),
                )
        return self._to_detail_out(updated)

    def get_planning_status(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> CampaignPlanningOut:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if campaign["activation_id"] is None:
            return CampaignPlanningOut(
                campaign_id=campaign_id,
                planning_status=campaign["planning_status"],
                enroll=None,
                render=None,
            )
        jobs = self.repo.get_planning_jobs(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            activation_id=UUID(str(campaign["activation_id"])),
        )
        by_phase = {row["phase"]: row for row in jobs}
        return CampaignPlanningOut(
            campaign_id=campaign_id,
            planning_status=campaign["planning_status"],
            enroll=self._job_out(by_phase.get("ENROLL")),
            render=self._job_out(by_phase.get("RENDER")),
        )

    def retry_planning(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> CampaignPlanningOut:
        """Re-run the activation's own FAILED enroll/render job (cursor kept).

        The API cannot write the planning tables, so this dispatches the worker task
        that resets the job; a repeat finds nothing FAILED and does nothing.
        """
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if not has_permission(context.role_code, "campaigns.execute"):
            raise AppError(
                "forbidden",
                "Only a manager can retry campaign planning",
                status_code=403,
            )
        if campaign["activation_id"] is None or campaign["status"] == "ARCHIVED":
            raise AppError(
                "state_conflict",
                "This campaign has no planning to retry",
                status_code=409,
            )
        self.repo.record_campaign_audit(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            action="campaign.planning_retry",
            campaign_id=campaign_id,
            after_state={},
        )
        self.session.commit()
        from app.services.task_dispatch import get_task_producer

        get_task_producer().send_task(
            "campaigns.retry_planning_job",
            kwargs={
                "workspace_id": str(context.workspace_id),
                "campaign_id": str(campaign_id),
                "activation_id": str(campaign["activation_id"]),
            },
            queue="campaigns",
        )
        return self.get_planning_status(context, campaign_id)

    def _resolve_command_receipt(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        idempotency_key: str,
        payload_hash: str,
    ) -> RowMapping:
        existing = self.repo.get_command_receipt(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            operation=_ACTIVATE_OPERATION,
            request_key=idempotency_key,
        )
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise AppError(
                    "conflict",
                    "This Idempotency-Key was already used with a different "
                    "activation request",
                    status_code=409,
                )
            return existing
        try:
            return self.repo.insert_pending_command_receipt(
                workspace_id=context.workspace_id,
                actor_id=context.user_id,
                operation=_ACTIVATE_OPERATION,
                request_key=idempotency_key,
                payload_hash=payload_hash,
                resource_type="campaign",
                resource_id=campaign_id,
            )
        except IntegrityError as exc:
            # command_receipts_identity_key: a truly simultaneous request
            # with the same idempotency key is inserting its own receipt
            # concurrently. Mirrors AudienceService.select_audience's
            # handling of its own analogous unique-index race -- report a
            # clean conflict rather than attempting a same-transaction
            # recovery (the failed INSERT has already put this transaction
            # in a state where only rollback is valid).
            raise AppError(
                "conflict",
                "An activation request with this Idempotency-Key is already "
                "being processed. Please retry shortly.",
                status_code=409,
            ) from exc

    def _dispatch_enroll_task(
        self, *, workspace_id: UUID, campaign_id: UUID, activation_id: UUID
    ) -> None:
        from app.services.task_dispatch import get_task_producer

        get_task_producer().send_task(
            "campaigns.enroll_activation_chunk",
            kwargs={
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "activation_id": str(activation_id),
            },
            queue="campaigns",
        )

    def _dispatch_reopen_task(
        self, context: WorkspaceContext, campaign_id: UUID, *, generation: int
    ) -> None:
        """Ask a worker to reopen finished leads, after the resume has committed.

        Deliberately not persisted: if this publish is lost nothing reopens, which is
        the safe outcome (the user can pause and resume again). Failing to publish
        never undoes the resume."""
        from app.services.task_dispatch import get_task_producer

        try:
            self.repo.record_campaign_audit(
                workspace_id=context.workspace_id,
                actor_id=context.user_id,
                action="campaign.reopen_completed_requested",
                campaign_id=campaign_id,
                after_state={"generation": generation},
            )
            self.session.commit()
            get_task_producer().send_task(
                "campaigns.reopen_completed_enrollments_chunk",
                kwargs={
                    "workspace_id": str(context.workspace_id),
                    "campaign_id": str(campaign_id),
                    "generation": generation,
                },
                queue="campaigns",
            )
        except Exception:
            logger.exception(
                "CAMPAIGN_REOPEN_DISPATCH_FAILED workspace=%s campaign=%s",
                context.workspace_id,
                campaign_id,
            )
            self.session.rollback()

    def _dispatch_reconcile_task(
        self, *, workspace_id: UUID, campaign_id: UUID, generation: int
    ) -> None:
        """Publish the post-resume reconcile pass (ADR-0020), now and once more
        after the claim lease, which returns messages that were still QUEUED at
        resume. Failing to publish must not undo the resume: the generation
        fence keeps every old message unclaimable, and the scheduler's
        stale-generation sweep re-publishes. Fails closed, never open."""
        from app.services.task_dispatch import get_task_producer

        kwargs = {
            "workspace_id": str(workspace_id),
            "campaign_id": str(campaign_id),
            "generation": generation,
        }
        lease = Settings.current().scheduler_claim_lease_seconds
        try:
            producer = get_task_producer()
            producer.send_task(
                "campaigns.reconcile_resume_chunk", kwargs=kwargs, queue="campaigns"
            )
            producer.send_task(
                "campaigns.reconcile_resume_chunk",
                kwargs=kwargs,
                queue="campaigns",
                countdown=lease + 60,
            )
        except Exception:
            logger.exception(
                "CAMPAIGN_RESUME_FAILED workspace=%s campaign=%s "
                "reason=reconcile_dispatch_failed; the stale-generation sweep "
                "will retry",
                workspace_id,
                campaign_id,
            )
            MetricsRegistry.get().increment("campaign_reconcile_dispatch_failed_total")

    def _job_out(self, row: RowMapping | None) -> PlanningJobStatusOut | None:
        if row is None:
            return None
        return PlanningJobStatusOut(
            phase=row["phase"],
            state=row["state"],
            processed_count=row["processed_count"],
            total_count=row["total_count"],
            error_reason=row["error_reason"],
        )

    def _to_detail_out(
        self, row: RowMapping, *, outstanding_attempts: int | None = None
    ) -> CampaignDetailOut:
        return CampaignDetailOut(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            description=row["description"],
            creator_id=row["creator_id"],
            status=row["status"],
            campaign_type=row.get("campaign_type", CAMPAIGN_TYPE_STANDARD),
            start_at=row["start_at"],
            draft_sequence_id=row["draft_sequence_id"],
            draft_audience_id=row["draft_audience_id"],
            current_settings_id=row["current_settings_id"],
            planning_status=row["planning_status"],
            archived_at=row["archived_at"],
            error_reason=row["error_reason"],
            is_activated=row.get("activation_id") is not None,
            erased_at=row.get("erased_at"),
            paused_at=row.get("paused_at"),
            paused_total_seconds=int(row.get("paused_total_seconds") or 0),
            last_resumed_at=row.get("last_resumed_at"),
            outstanding_attempts=outstanding_attempts,
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
