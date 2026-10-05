"""RevenueOS campaign start (ADR-0021): commit a captured audience and activate
the campaign, by calling the two services the Review page calls.

Nothing is decided here that those services do not already decide: the
audience must be READY, preflight must pass, and activation is idempotent.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.modules.campaigns.activation_service import CampaignActivationService
from app.modules.campaigns.audience_service import AudienceService
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import ActivateIn

logger = logging.getLogger(__name__)

_STARTED_STATUSES = frozenset({"RUNNING", "SCHEDULED"})


class RevenueOSCampaignStartOut(BaseModel):
    # "already_started" when the campaign was running before this request.
    status: Literal["started", "already_started"]
    campaign_id: UUID
    campaign_status: str
    audience_id: UUID | None = None


class RevenueOSStartService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)

    def start(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> RevenueOSCampaignStartOut:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if campaign["status"] in _STARTED_STATUSES:
            return RevenueOSCampaignStartOut(
                status="already_started",
                campaign_id=campaign_id,
                campaign_status=campaign["status"],
                audience_id=campaign["activated_audience_id"],
            )
        if campaign["status"] != "DRAFT":
            # PAUSED, ERROR, COMPLETED, ARCHIVED: someone decided that in the
            # app, and this route never overrides a person.
            raise AppError(
                "state_conflict",
                f"Campaign is {campaign['status']} and cannot be started",
                status_code=409,
            )

        audience = self.repo.get_latest_audience(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if audience is None:
            raise AppError(
                "validation_error", "Campaign has no audience", status_code=422
            )
        if audience["status"] == "CAPTURING":
            # Capture is a worker task that normally finishes within seconds of
            # the intake request; the caller retries.
            raise AppError(
                "audience_not_ready",
                "The audience is still being captured. Retry shortly.",
                status_code=409,
            )
        if audience["status"] != "READY":
            raise AppError(
                "validation_error",
                f"Audience capture ended as {audience['status']}; send the "
                "campaign again under a new reference",
                status_code=422,
            )

        audience_id = UUID(str(audience["id"]))
        if str(campaign["draft_audience_id"]) != str(audience_id):
            AudienceService(self.session).commit_audience(
                context, campaign_id, audience_id
            )
            campaign = self.repo.get_campaign(
                workspace_id=context.workspace_id, campaign_id=campaign_id
            )
            assert campaign is not None

        version = int(campaign["version"])
        activated = CampaignActivationService(self.session).activate(
            context,
            campaign_id,
            ActivateIn(expected_version=version),
            # A retry after success never reaches here (the status check above
            # answers it), so the key only has to be unique per attempt state.
            f"revenueos.start:{campaign_id}:{version}",
        )
        logger.info(
            "RevenueOS campaign started",
            extra={
                "workspace_id": str(context.workspace_id),
                "campaign_id": str(campaign_id),
                "audience_id": str(audience_id),
                "campaign_status": activated.status,
            },
        )
        return RevenueOSCampaignStartOut(
            status="started",
            campaign_id=campaign_id,
            campaign_status=activated.status,
            audience_id=audience_id,
        )
