"""RevenueOS launch (ADR-0022): one request that provisions the user and
workspace, connects the mailbox, adds the recipients as leads, stores the
campaign and starts it. A HYPER_PERSONALIZED campaign (ADR-0023) is stored
with its objective and sample emails are requested; a person approves them
and starts it in the app.

It is an orchestration of the other RevenueOS services and of LeadService;
every rule stays where it already lives.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.db.context import enter_api_scope
from app.modules.campaigns.audience_service import AudienceService
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import AudienceSelectIn, CampaignSettingsCreateIn
from app.modules.integrations.revenueos_intake import (
    IntakeCampaignIn,
    IntakeEmailIn,
    RevenueOSCampaignIntakeIn,
    RevenueOSIntakeService,
    validate_email_waits,
)
from app.modules.integrations.revenueos_provisioning import (
    RevenueOSProvisioningService,
    RevenueOSUserProvisionIn,
    clean_user_email,
)
from app.modules.integrations.revenueos_start import RevenueOSStartService
from app.modules.integrations.supabase_auth_admin import EnsuredUser
from app.modules.leads.normalization import normalize_email
from app.modules.leads.service import LeadService
from app.modules.mailboxes.repository import MailboxRepository
from app.modules.mailboxes.schemas import SmtpConnectRequest
from app.modules.personalization.api_service import PersonalizationApiService
from app.modules.personalization.config_schema import PersonalizationConfig
from app.modules.personalization.schemas import PreviewCreateIn
from app.schemas.leads import LeadCreateIn, LeadListCreateIn, LeadProfileIn

logger = logging.getLogger(__name__)

LAUNCH_OPERATION = "revenueos.launch"

# Bounds one request; larger audiences go through a CSV import.
_MAX_RECIPIENTS = 500
_MAX_EMAILS = 10
# How long the request waits for the capture worker before handing the start
# back to the caller as "pending".
_START_WAIT_SECONDS = 15.0
_START_POLL_SECONDS = 1.0


class LaunchUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    workspace_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Literal["ADMIN", "MANAGER", "MEMBER", "VIEWER"] = "ADMIN"

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return clean_user_email(value)


class LaunchRecipientIn(LeadProfileIn):
    """A lead. The profile fields (company_industry, company_website, city,
    country, ...) are what a hyper-personalized email is written from; a
    recipient with too few of them gets the reference email instead."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    first_name: str | None = Field(default=None, max_length=200)
    last_name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)


class RevenueOSLaunchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # RevenueOS's own id for this campaign; repeating it never creates a
    # second one.
    reference: str = Field(min_length=1, max_length=200)
    # RevenueOS's own id for the client. Launches that share it share one
    # workspace, user and mailbox, so a client can have many campaigns.
    # Omitted: the campaign reference is used, one workspace per launch.
    client_reference: str | None = Field(default=None, min_length=1, max_length=200)
    user: LaunchUserIn
    # Omitted: the workspace's existing connected mailbox is used.
    smtp: SmtpConnectRequest | None = None
    recipients: list[LaunchRecipientIn] = Field(
        min_length=1, max_length=_MAX_RECIPIENTS
    )
    campaign: IntakeCampaignIn
    emails: list[IntakeEmailIn] = Field(min_length=1, max_length=_MAX_EMAILS)
    schedule: CampaignSettingsCreateIn
    # False stores a draft for a person to start. Not used for a
    # HYPER_PERSONALIZED campaign, which a person always approves and starts.
    auto_start: bool = True
    campaign_type: Literal["STANDARD", "HYPER_PERSONALIZED"] = "STANDARD"
    # HYPER_PERSONALIZED only, and required for it: what the campaign is
    # trying to achieve. `emails` are then the reference templates.
    objective: PersonalizationConfig | None = None

    @field_validator("reference", "client_reference")
    @classmethod
    def _reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reference must not be blank")
        return cleaned

    @property
    def resolved_client_reference(self) -> str:
        return self.client_reference or self.reference

    @property
    def is_hyper(self) -> bool:
        return self.campaign_type == "HYPER_PERSONALIZED"

    @property
    def wants_auto_start(self) -> bool:
        return self.auto_start and not self.is_hyper

    @model_validator(mode="after")
    def _waits_match_sequence_rules(self) -> RevenueOSLaunchIn:
        validate_email_waits(self.emails)
        if self.is_hyper and self.objective is None:
            raise ValueError("a HYPER_PERSONALIZED campaign needs an objective")
        if not self.is_hyper and self.objective is not None:
            raise ValueError("objective is only for a HYPER_PERSONALIZED campaign")
        return self


class LaunchRejectedRecipient(BaseModel):
    email: str
    reason: str


class RevenueOSLaunchOut(BaseModel):
    status: Literal["launched"] = "launched"
    reference: str
    client_reference: str
    # True when this reference was already launched and nothing new was created.
    duplicate: bool

    user_id: UUID
    email: str
    user_created: bool
    workspace_id: UUID
    workspace_name: str
    role: str

    mailbox_id: UUID | None = None
    # null when the request carried no `smtp` block.
    mailbox_status: Literal["connected", "updated", "failed"] | None = None
    mailbox_error: str | None = None

    list_id: UUID | None = None
    recipients_created: int = 0
    recipients_existing: int = 0
    recipients_rejected: list[LaunchRejectedRecipient] = Field(default_factory=list)

    campaign_id: UUID | None = None
    campaign_status: str | None = None
    campaign_type: Literal["STANDARD", "HYPER_PERSONALIZED"] = "STANDARD"
    # started / already_started: running. pending: capture not finished in time,
    # send the same request again. failed: the reason is in start_error.
    # blocked: no campaign was stored. not_requested: auto_start was false.
    # awaiting_approval: hyper-personalized, samples requested; a person
    # approves them and starts the campaign in the app.
    start_status: Literal[
        "started",
        "already_started",
        "pending",
        "failed",
        "blocked",
        "not_requested",
        "awaiting_approval",
    ]
    start_error: str | None = None


def _payload_hash(payload: RevenueOSLaunchIn) -> str:
    content = {
        "campaign": payload.campaign.model_dump(mode="json"),
        "emails": [email.model_dump(mode="json") for email in payload.emails],
        "schedule": payload.schedule.model_dump(mode="json"),
        "recipients": sorted(r.email.strip().lower() for r in payload.recipients),
    }
    if payload.objective is not None:
        # Added only when present, so a STANDARD launch stored before this
        # key existed keeps its hash and still replays.
        content["objective"] = payload.objective.canonical()
    encoded = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RevenueOSLaunchService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)

    def launch(
        self,
        *,
        actor_id: UUID,
        payload: RevenueOSLaunchIn,
        user: EnsuredUser,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> RevenueOSLaunchOut:
        provisioned = RevenueOSProvisioningService(self.session).provision(
            actor_id=actor_id,
            payload=RevenueOSUserProvisionIn(
                # The client, not the campaign, is what a workspace belongs to.
                reference=payload.resolved_client_reference,
                email=payload.user.email,
                workspace_name=payload.user.workspace_name,
                role=payload.user.role,
                smtp=payload.smtp,
            ),
            user=user,
        )
        result = RevenueOSLaunchOut(
            reference=payload.reference,
            client_reference=payload.resolved_client_reference,
            duplicate=False,
            user_id=provisioned.user_id,
            email=provisioned.email,
            user_created=provisioned.user_created,
            workspace_id=provisioned.workspace_id,
            workspace_name=provisioned.workspace_name,
            role=provisioned.role,
            mailbox_id=provisioned.mailbox_id,
            mailbox_status=provisioned.mailbox_status,
            mailbox_error=provisioned.mailbox_error,
            campaign_type=payload.campaign_type,
            start_status="blocked",
        )
        # Provisioning returns only when the acting user is the Owner of the
        # workspace it bootstrapped; the database still enforces it under RLS.
        context = WorkspaceContext(
            workspace_id=provisioned.workspace_id, user_id=actor_id, role_code="OWNER"
        )

        self._store(context, payload, result)
        if result.campaign_id is None:
            return result
        if not payload.is_hyper and not payload.auto_start:
            result.start_status = "not_requested"
            return result

        # The capture worker can only see the audience once this is committed,
        # and nothing below may undo what was stored above.
        self.session.commit()
        if payload.is_hyper:
            self._request_samples(context, result, sleep, clock)
        else:
            self._start(context, result, sleep, clock)
        return result

    def _store(
        self,
        context: WorkspaceContext,
        payload: RevenueOSLaunchIn,
        result: RevenueOSLaunchOut,
    ) -> None:
        payload_hash = _payload_hash(payload)
        receipt = self.repo.get_command_receipt(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            operation=LAUNCH_OPERATION,
            request_key=payload.reference,
        )
        if receipt is not None:
            if receipt["payload_hash"] != payload_hash:
                raise AppError(
                    "conflict",
                    "This reference was already launched with a different campaign "
                    "or different recipients",
                    status_code=409,
                )
            campaign_id = UUID(str(receipt["resource_id"]))
            campaign = self.repo.get_campaign(
                workspace_id=context.workspace_id, campaign_id=campaign_id
            )
            if receipt["status"] != "COMPLETED" or campaign is None:
                raise AppError(
                    "conflict",
                    "This reference was already launched and its campaign is no "
                    "longer available",
                    status_code=409,
                )
            result.duplicate = True
            result.campaign_id = campaign_id
            result.campaign_status = campaign["status"]
            return

        mailbox_id = self._usable_mailbox(context.workspace_id, result)
        if mailbox_id is None:
            result.start_error = (
                "No connected mailbox: " + result.mailbox_error
                if result.mailbox_error
                else "The workspace has no connected mailbox; send an smtp block"
            )
            return
        result.mailbox_id = mailbox_id

        list_id = self._add_recipients(context, payload, result)
        intake = RevenueOSIntakeService(self.session).store_campaign(
            context,
            RevenueOSCampaignIntakeIn(
                reference=payload.reference,
                campaign=payload.campaign,
                emails=payload.emails,
                schedule=payload.schedule,
                audience=AudienceSelectIn(list_ids=[list_id]),
                mailbox_id=mailbox_id,
            ),
            objective=payload.objective,
        )
        try:
            launch_receipt = self.repo.insert_pending_command_receipt(
                workspace_id=context.workspace_id,
                actor_id=context.user_id,
                operation=LAUNCH_OPERATION,
                request_key=payload.reference,
                payload_hash=payload_hash,
                resource_type="campaign",
                resource_id=intake.campaign_id,
            )
        except IntegrityError as exc:
            raise AppError(
                "conflict",
                "A request with this reference is already being processed. "
                "Please retry shortly.",
                status_code=409,
            ) from exc
        self.repo.complete_command_receipt(
            workspace_id=context.workspace_id,
            receipt_id=UUID(str(launch_receipt["id"])),
            response_version=1,
        )
        result.campaign_id = intake.campaign_id
        result.campaign_status = intake.campaign_status
        logger.info(
            "RevenueOS launch stored",
            extra={
                "workspace_id": str(context.workspace_id),
                "campaign_id": str(intake.campaign_id),
                "recipients_created": result.recipients_created,
                "recipients_existing": result.recipients_existing,
                "recipients_rejected": len(result.recipients_rejected),
            },
        )

    def _usable_mailbox(
        self, workspace_id: UUID, result: RevenueOSLaunchOut
    ) -> UUID | None:
        if result.mailbox_status in ("connected", "updated"):
            return result.mailbox_id
        # No smtp block, or an update that was refused: a failed update leaves
        # the mailbox on its previous, working credentials.
        for row in MailboxRepository(self.session).list_mailboxes(workspace_id):
            if row["connection_state"] == "CONNECTED":
                return UUID(str(row["id"]))
        return None

    def _add_recipients(
        self,
        context: WorkspaceContext,
        payload: RevenueOSLaunchIn,
        result: RevenueOSLaunchOut,
    ) -> UUID:
        """A list of its own per launch, so the campaign's audience is exactly
        the recipients of this request and nobody a person added elsewhere."""
        leads = LeadService(self.session)
        list_id = leads.create_list(
            context, LeadListCreateIn(name=f"RevenueOS {payload.reference}"[:200])
        ).id
        for recipient in payload.recipients:
            try:
                # A savepoint: one bad address must not undo the others.
                with self.session.begin_nested():
                    leads.create_lead(
                        context,
                        LeadCreateIn(**recipient.model_dump(), list_id=list_id),
                    )
                result.recipients_created += 1
            except AppError as exc:
                if exc.code == "duplicate_lead" and self._add_existing_lead(
                    context, leads, list_id, recipient.email
                ):
                    result.recipients_existing += 1
                else:
                    result.recipients_rejected.append(
                        LaunchRejectedRecipient(
                            email=recipient.email, reason=exc.message
                        )
                    )
        if result.recipients_created + result.recipients_existing == 0:
            raise AppError(
                "validation_error",
                "None of the recipients could be added",
                status_code=422,
                details=[r.model_dump() for r in result.recipients_rejected],
            )
        result.list_id = list_id
        return list_id

    def _add_existing_lead(
        self,
        context: WorkspaceContext,
        leads: LeadService,
        list_id: UUID,
        email: str,
    ) -> bool:
        """The address is already a lead here: reuse it, never overwrite the
        details a person may have edited. An archived lead stays out."""
        row = (
            self.session.execute(
                text(
                    """
                    SELECT id FROM leads
                    WHERE workspace_id = :workspace_id
                      AND canonical_address = :canonical_address
                      AND normalization_version = 1
                      AND status = 'ACTIVE'
                    """
                ),
                {
                    "workspace_id": str(context.workspace_id),
                    "canonical_address": normalize_email(email).canonical,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        leads.repo.add_member(
            workspace_id=context.workspace_id,
            list_id=list_id,
            lead_id=UUID(str(row["id"])),
            actor_id=context.user_id,
        )
        return True

    def _start(
        self,
        context: WorkspaceContext,
        result: RevenueOSLaunchOut,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        assert result.campaign_id is not None
        deadline = clock() + _START_WAIT_SECONDS
        while True:
            # Each attempt is its own transaction: the previous one ended, and
            # with it the request's role and row-level-security context.
            enter_api_scope(
                self.session,
                user_id=context.user_id,
                workspace_id=context.workspace_id,
            )
            try:
                started = RevenueOSStartService(self.session).start(
                    context, result.campaign_id
                )
            except AppError as exc:
                self.session.rollback()
                if exc.code == "audience_not_ready" and clock() < deadline:
                    sleep(_START_POLL_SECONDS)
                    continue
                result.start_status = (
                    "pending" if exc.code == "audience_not_ready" else "failed"
                )
                result.start_error = exc.message
                return
            self.session.commit()
            result.start_status = started.status
            result.campaign_status = started.campaign_status
            return

    def _request_samples(
        self,
        context: WorkspaceContext,
        result: RevenueOSLaunchOut,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        """Hyper-personalized: confirm the captured audience and ask for sample
        emails for its first recipient. Approving them and starting stays with a
        person (owner decision, ADR-0023). Safe to repeat."""
        assert result.campaign_id is not None
        deadline = clock() + _START_WAIT_SECONDS
        while True:
            enter_api_scope(
                self.session,
                user_id=context.user_id,
                workspace_id=context.workspace_id,
            )
            try:
                waiting = self._request_samples_once(context, result)
            except AppError as exc:
                self.session.rollback()
                result.start_status = "failed"
                result.start_error = exc.message
                return
            if not waiting:
                return
            self.session.rollback()
            if clock() >= deadline:
                result.start_status = "pending"
                result.start_error = (
                    "The audience is still being captured. Send the same request again."
                )
                return
            sleep(_START_POLL_SECONDS)

    def _request_samples_once(
        self, context: WorkspaceContext, result: RevenueOSLaunchOut
    ) -> bool:
        """True while the audience is still being captured."""
        campaign_id = result.campaign_id
        assert campaign_id is not None
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        result.campaign_status = campaign["status"]
        if campaign["status"] != "DRAFT":
            # A person already approved and started it (or stopped it).
            result.start_status = "already_started"
            return False

        audience = self.repo.get_latest_audience(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if audience is None or audience["status"] == "CAPTURING":
            return True
        if audience["status"] != "READY":
            raise AppError(
                "validation_error",
                f"Audience capture ended as {audience['status']}",
                status_code=422,
            )
        audience_id = UUID(str(audience["id"]))
        if str(campaign["draft_audience_id"]) != str(audience_id):
            AudienceService(self.session).commit_audience(
                context, campaign_id, audience_id
            )
        members = self.repo.list_accepted_audience_members(
            workspace_id=context.workspace_id,
            audience_id=audience_id,
            after_ordinal=None,
            limit=1,
        )
        if not members:
            raise AppError(
                "validation_error",
                "No recipient was accepted into the audience",
                status_code=422,
            )
        # create_previews commits and dispatches the generation task. The batch
        # id is derived from the campaign, so a repeat spends nothing more.
        PersonalizationApiService(self.session).create_previews(
            context,
            campaign_id,
            PreviewCreateIn(
                batch_id=uuid.uuid5(uuid.NAMESPACE_URL, f"revenueos:{campaign_id}"),
                audience_member_ids=[UUID(str(members[0]["id"]))],
            ),
        )
        result.start_status = "awaiting_approval"
        return False
