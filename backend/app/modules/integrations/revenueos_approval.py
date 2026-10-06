"""RevenueOS sample review and approval (ADR-0024): read the sample emails of a
hyper-personalized campaign, and approve them and start it.

Approval and activation are the existing commands (PersonalizationApiService,
RevenueOSStartService); every rule about stale, failed or unfinished samples
stays in them.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.modules.integrations.revenueos_start import (
    RevenueOSCampaignStartOut,
    RevenueOSStartService,
)
from app.modules.personalization.api_service import PersonalizationApiService
from app.modules.personalization.schemas import ApproveIn, PreviewBatchOut

logger = logging.getLogger(__name__)

_STARTED_STATUSES = frozenset({"RUNNING", "SCHEDULED"})


SamplesStatus = Literal["none", "generating", "ready", "failed", "stale"]


class RevenueOSSampleOut(BaseModel):
    # Which email of the sequence this is a sample of (1 = the first).
    email_number: int
    state: Literal["PENDING", "OK", "FAILED"]
    recipient_first_name: str | None = None
    recipient_company: str | None = None
    subject: str | None = None
    body_html: str | None = None
    # True when the recipient had too little data and got the reference email.
    fallback_used: bool = False
    failure_codes: list[str] = Field(default_factory=list)


class RevenueOSSamplesOut(BaseModel):
    campaign_id: UUID
    campaign_status: str
    # none: not requested yet. generating: wait and ask again. ready: can be
    # approved. failed: at least one could not be written. stale: the objective
    # or emails changed after they were written.
    samples_status: SamplesStatus
    approval_status: Literal["NONE", "APPROVED", "STALE"]
    samples: list[RevenueOSSampleOut] = Field(default_factory=list)


def _samples_status(batch: PreviewBatchOut | None) -> SamplesStatus:
    if batch is None:
        return "none"
    if batch.stale:
        return "stale"
    if not batch.complete:
        return "generating"
    return "ready" if batch.all_ok else "failed"


class RevenueOSApprovalService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.personalization = PersonalizationApiService(session)

    def samples(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> RevenueOSSamplesOut:
        batch = self.personalization.latest_batch(context, campaign_id)
        campaign = self.personalization.campaigns.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        assert campaign is not None  # latest_batch already answered 404
        ordered = sorted(batch.items, key=lambda i: i.step_position) if batch else []
        return RevenueOSSamplesOut(
            campaign_id=campaign_id,
            campaign_status=campaign["status"],
            samples_status=_samples_status(batch),
            approval_status=self.personalization.approval_status(
                context, campaign_id
            ).status,
            samples=[
                RevenueOSSampleOut(
                    email_number=number,
                    state=item.state,
                    recipient_first_name=item.recipient.first_name,
                    recipient_company=item.recipient.company,
                    subject=item.subject,
                    body_html=item.body_html,
                    fallback_used=item.fallback_used,
                    failure_codes=item.failure_codes,
                )
                for number, item in enumerate(ordered, start=1)
            ],
        )

    def approve_and_start(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> RevenueOSCampaignStartOut:
        batch = self.personalization.latest_batch(context, campaign_id)
        campaign = self.personalization.campaigns.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        assert campaign is not None
        if campaign["status"] in _STARTED_STATUSES:
            # A repeat after success: the start service answers already_started.
            return RevenueOSStartService(self.session).start(context, campaign_id)

        approval = self.personalization.approval_status(context, campaign_id)
        if approval.status != "APPROVED":
            if batch is None:
                raise AppError(
                    "samples_not_ready",
                    "No sample emails have been generated for this campaign yet",
                    status_code=409,
                )
            # Refuses stale, unfinished or failed samples with its own codes.
            self.personalization.approve(
                context,
                campaign_id,
                ApproveIn(batch_id=batch.batch_id, config_digest=batch.current_digest),
            )
            logger.info(
                "RevenueOS samples approved",
                extra={
                    "workspace_id": str(context.workspace_id),
                    "campaign_id": str(campaign_id),
                },
            )
        return RevenueOSStartService(self.session).start(context, campaign_id)
