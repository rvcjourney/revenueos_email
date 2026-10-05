"""RevenueOS campaign intake (ADR-0019): one request that stores a complete
STANDARD draft campaign by composing the existing campaign services.

Nothing here activates a campaign or sends email. Every rule (content
sanitizing, variable validation, workspace scoping of mailbox/list/lead ids)
stays owned by the service that already enforces it for the UI.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.modules.campaigns.audience_service import AudienceService
from app.modules.campaigns.mailbox_service import CampaignMailboxService
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import (
    AudienceSelectIn,
    CampaignCreateIn,
    CampaignMailboxAssignIn,
    CampaignSettingsCreateIn,
    SequenceStepCreateIn,
)
from app.modules.campaigns.sequence_service import SequenceService
from app.modules.campaigns.service import CampaignService
from app.modules.campaigns.settings_service import CampaignSettingsService

logger = logging.getLogger(__name__)

INTAKE_OPERATION = "revenueos.campaign_intake"

_MINUTES_PER_DAY = 1440
# Bounds one request; a sequence built in the UI has no such cap.
_MAX_EMAILS = 10


class IntakeCampaignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class IntakeEmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=500)
    body_html: str = Field(min_length=1, max_length=200_000)
    preheader: str | None = Field(default=None, max_length=255)
    # Days to wait after the previous email; a WAIT step is at most one year.
    wait_days_before: int = Field(default=0, ge=0, le=365)


def validate_email_waits(emails: list[IntakeEmailIn]) -> None:
    # A sequence must alternate EMAIL and WAIT and start with an EMAIL
    # (preflight), so only follow-ups carry a wait, and they must have one.
    if emails[0].wait_days_before != 0:
        raise ValueError("the first email must have wait_days_before = 0")
    if any(email.wait_days_before < 1 for email in emails[1:]):
        raise ValueError("every follow-up email needs wait_days_before >= 1")


class RevenueOSCampaignIntakeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # RevenueOS's own id for this campaign. It is the idempotency key: the same
    # reference never creates a second campaign.
    reference: str = Field(min_length=1, max_length=200)
    campaign: IntakeCampaignIn
    emails: list[IntakeEmailIn] = Field(min_length=1, max_length=_MAX_EMAILS)
    schedule: CampaignSettingsCreateIn
    audience: AudienceSelectIn
    mailbox_id: UUID

    @field_validator("reference")
    @classmethod
    def _reference(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reference must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _waits_match_sequence_rules(self) -> RevenueOSCampaignIntakeIn:
        validate_email_waits(self.emails)
        return self


class RevenueOSCampaignIntakeOut(BaseModel):
    status: Literal["stored"] = "stored"
    reference: str
    campaign_id: UUID
    campaign_status: str
    audience_id: UUID | None = None
    audience_status: str | None = None
    # True when this reference was already stored and nothing new was created.
    duplicate: bool


def _payload_hash(payload: RevenueOSCampaignIntakeIn) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RevenueOSIntakeService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)

    def store_campaign(
        self, context: WorkspaceContext, payload: RevenueOSCampaignIntakeIn
    ) -> RevenueOSCampaignIntakeOut:
        payload_hash = _payload_hash(payload)
        existing = self.repo.get_command_receipt(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            operation=INTAKE_OPERATION,
            request_key=payload.reference,
        )
        if existing is not None:
            return self._replay(context, payload, existing, payload_hash)

        campaign = CampaignService(self.session).create_campaign(
            context,
            CampaignCreateIn(
                name=payload.campaign.name,
                description=payload.campaign.description,
                campaign_type="STANDARD",
            ),
        )
        try:
            receipt = self.repo.insert_pending_command_receipt(
                workspace_id=context.workspace_id,
                actor_id=context.user_id,
                operation=INTAKE_OPERATION,
                request_key=payload.reference,
                payload_hash=payload_hash,
                resource_type="campaign",
                resource_id=campaign.id,
            )
        except IntegrityError as exc:
            # command_receipts_identity_key: a simultaneous request with the
            # same reference won. The failed INSERT leaves this transaction
            # valid only for rollback, which also removes the campaign above.
            raise AppError(
                "conflict",
                "A request with this reference is already being processed. "
                "Please retry shortly.",
                status_code=409,
            ) from exc

        sequence = SequenceService(self.session)
        for index, email in enumerate(payload.emails):
            sequence.add_step(
                context,
                campaign.id,
                SequenceStepCreateIn(
                    kind="EMAIL",
                    # With a leading wait the WAIT takes this position and the
                    # EMAIL the next one, so follow-up N lands on 2N / 2N + 1.
                    position=1 if index == 0 else index * 2,
                    email_subject=email.subject,
                    email_body_html=email.body_html,
                    email_preheader=email.preheader,
                    leading_wait_minutes=(
                        email.wait_days_before * _MINUTES_PER_DAY
                        if email.wait_days_before
                        else None
                    ),
                ),
            )
        CampaignSettingsService(self.session).create_settings_version(
            context, campaign.id, payload.schedule
        )
        CampaignMailboxService(self.session).assign_mailbox(
            context, campaign.id, CampaignMailboxAssignIn(mailbox_id=payload.mailbox_id)
        )

        stored = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign.id
        )
        assert stored is not None
        self.repo.complete_command_receipt(
            workspace_id=context.workspace_id,
            receipt_id=UUID(str(receipt["id"])),
            response_version=stored["version"],
        )
        # Last, because it enqueues the capture task: any failure above must
        # roll back without leaving a task that points at nothing.
        audience = AudienceService(self.session).select_audience(
            context, campaign.id, payload.audience
        )

        logger.info(
            "RevenueOS campaign intake stored",
            extra={
                "workspace_id": str(context.workspace_id),
                "campaign_id": str(campaign.id),
                "audience_id": str(audience.id),
                "email_count": len(payload.emails),
            },
        )
        return RevenueOSCampaignIntakeOut(
            reference=payload.reference,
            campaign_id=campaign.id,
            campaign_status=stored["status"],
            audience_id=audience.id,
            audience_status=audience.status,
            duplicate=False,
        )

    def _replay(
        self,
        context: WorkspaceContext,
        payload: RevenueOSCampaignIntakeIn,
        receipt: RowMapping,
        payload_hash: str,
    ) -> RevenueOSCampaignIntakeOut:
        if receipt["payload_hash"] != payload_hash:
            raise AppError(
                "conflict",
                "This reference was already used with different campaign content",
                status_code=409,
            )
        campaign_id = UUID(str(receipt["resource_id"]))
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if receipt["status"] != "COMPLETED" or campaign is None:
            # A receipt only commits together with its finished campaign, so
            # this means the campaign was removed afterwards. Never recreate it
            # silently under a reference that was already acknowledged.
            raise AppError(
                "conflict",
                "This reference was already used and its campaign is no longer "
                "available",
                status_code=409,
            )
        audience_id = campaign["draft_audience_id"]
        if audience_id is not None:
            audience = self.repo.get_audience(
                workspace_id=context.workspace_id,
                campaign_id=campaign_id,
                audience_id=UUID(str(audience_id)),
            )
        else:
            audience = self.repo.get_latest_audience(
                workspace_id=context.workspace_id, campaign_id=campaign_id
            )
        return RevenueOSCampaignIntakeOut(
            reference=payload.reference,
            campaign_id=campaign_id,
            campaign_status=campaign["status"],
            audience_id=audience["id"] if audience is not None else None,
            audience_status=audience["status"] if audience is not None else None,
            duplicate=True,
        )
