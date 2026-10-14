"""RevenueOS sample review and approval (ADR-0024): read the sample emails of a
hyper-personalized campaign, and approve them and start it. Feedback (ADR-0025):
save a reviewer's notes in the objective and write new samples for the same
recipients.

Approval, the objective, samples and activation are the existing commands
(PersonalizationApiService, RevenueOSStartService); every rule about stale,
failed or unfinished samples stays in them.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.modules.integrations.revenueos_start import (
    RevenueOSCampaignStartOut,
    RevenueOSStartService,
)
from app.modules.personalization.api_service import PersonalizationApiService
from app.modules.personalization.config_schema import PersonalizationConfig
from app.modules.personalization.schemas import (
    ApproveIn,
    PersonalizationConfigIn,
    PreviewBatchOut,
    PreviewItemOut,
    RevenueOSPreviewCreateIn,
)

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


class RevenueOSSampleEmailOut(BaseModel):
    email_number: int
    state: Literal["PENDING", "OK", "FAILED"]
    subject: str | None = None
    body_html: str | None = None
    fallback_used: bool = False
    failure_codes: list[str] = Field(default_factory=list)


class RevenueOSSampleRecipientOut(BaseModel):
    audience_member_id: UUID
    email: str | None = None
    first_name: str | None = None
    company: str | None = None
    title: str | None = None
    emails: list[RevenueOSSampleEmailOut] = Field(default_factory=list)


class RevenueOSSamplesOut(BaseModel):
    campaign_id: UUID
    campaign_status: str
    # none: not requested yet. generating: wait and ask again. ready: can be
    # approved. failed: at least one could not be written. stale: the objective
    # or emails changed after they were written.
    samples_status: SamplesStatus
    approval_status: Literal["NONE", "APPROVED", "STALE"]
    # The version shown: send both back to approve, or batch_id with feedback.
    batch_id: UUID | None = None
    config_digest: str | None = None
    recipients: list[RevenueOSSampleRecipientOut] = Field(default_factory=list)
    # Every sample in one list, recipient by recipient (the ADR-0024 shape).
    samples: list[RevenueOSSampleOut] = Field(default_factory=list)


class RevenueOSFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # The batch the reviewer read. Sending the same one again changes nothing.
    batch_id: UUID
    feedback: str = Field(min_length=1, max_length=1000)


class RevenueOSFeedbackOut(BaseModel):
    campaign_id: UUID
    # The new samples, written with the feedback; read them with GET samples.
    batch_id: UUID
    samples_status: SamplesStatus
    # True when this feedback was already applied and nothing new was done.
    duplicate: bool


def _samples_status(batch: PreviewBatchOut | None) -> SamplesStatus:
    if batch is None:
        return "none"
    if batch.stale:
        return "stale"
    if not batch.complete:
        return "generating"
    return "ready" if batch.all_ok else "failed"


def _feedback_batch_id(batch_id: UUID) -> UUID:
    """The samples written from the feedback on `batch_id`. Derived, so a repeat
    finds them instead of writing (and paying for) them again."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"revenueos-feedback:{batch_id}")


def _member_ids(batch: PreviewBatchOut) -> list[UUID]:
    return list(
        dict.fromkeys(item.recipient.audience_member_id for item in batch.items)
    )


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
        recipients: dict[UUID, RevenueOSSampleRecipientOut] = {}
        flat: list[RevenueOSSampleOut] = []
        if batch is not None:
            positions = sorted({item.step_position for item in batch.items})
            order = _member_ids(batch)
            for item in sorted(
                batch.items,
                key=lambda i: (
                    order.index(i.recipient.audience_member_id),
                    i.step_position,
                ),
            ):
                number = positions.index(item.step_position) + 1
                person = item.recipient
                recipients.setdefault(
                    person.audience_member_id,
                    RevenueOSSampleRecipientOut(
                        audience_member_id=person.audience_member_id,
                        email=person.email,
                        first_name=person.first_name,
                        company=person.company,
                        title=person.title,
                    ),
                ).emails.append(_email_out(number, item))
                flat.append(
                    RevenueOSSampleOut(
                        **_email_out(number, item).model_dump(),
                        recipient_first_name=person.first_name,
                        recipient_company=person.company,
                    )
                )
        return RevenueOSSamplesOut(
            campaign_id=campaign_id,
            campaign_status=campaign["status"],
            samples_status=_samples_status(batch),
            approval_status=self.personalization.approval_status(
                context, campaign_id
            ).status,
            batch_id=batch.batch_id if batch else None,
            config_digest=batch.config_digest if batch else None,
            recipients=list(recipients.values()),
            samples=flat,
        )

    def feedback(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: RevenueOSFeedbackIn,
    ) -> RevenueOSFeedbackOut:
        """Add the feedback to the objective, then write new samples for the same
        recipients. Both are one transaction: create_previews commits them
        together, or nothing changes. Safe to repeat."""
        latest = self.personalization.latest_batch(context, campaign_id)
        new_id = _feedback_batch_id(payload.batch_id)
        if self.personalization.repo.list_batch(
            workspace_id=context.workspace_id, campaign_id=campaign_id, batch_id=new_id
        ):
            done = self.personalization.get_batch(context, campaign_id, new_id)
            return RevenueOSFeedbackOut(
                campaign_id=campaign_id,
                batch_id=new_id,
                samples_status=_samples_status(done),
                duplicate=True,
            )
        if latest is None or latest.batch_id != payload.batch_id:
            raise AppError(
                "batch_not_latest",
                "These are not the latest samples. Read the samples again.",
                status_code=409,
            )

        state = self.personalization.get_state(context, campaign_id)
        if state.config is None:
            raise AppError(
                "objective_missing",
                "Define the campaign objective before giving feedback",
                status_code=422,
            )
        earlier = state.config.feedback
        merged = f"{earlier}\n\n{payload.feedback}" if earlier else payload.feedback
        try:
            config = PersonalizationConfig.model_validate(
                {**state.config.canonical(), "feedback": merged}
            )
        except ValidationError as exc:
            raise AppError(
                "feedback_too_long",
                "There is too much feedback on this campaign to add more.",
                status_code=422,
            ) from exc
        # Requires a DRAFT campaign; a concurrent edit answers 409 conflict.
        self.personalization.put_config(
            context,
            campaign_id,
            PersonalizationConfigIn(
                config=config, expected_version=state.config_version
            ),
        )
        batch = self.personalization.create_previews(
            context,
            campaign_id,
            RevenueOSPreviewCreateIn(
                batch_id=new_id, audience_member_ids=_member_ids(latest)
            ),
        )
        logger.info(
            "RevenueOS sample feedback applied",
            extra={
                "workspace_id": str(context.workspace_id),
                "campaign_id": str(campaign_id),
            },
        )
        return RevenueOSFeedbackOut(
            campaign_id=campaign_id,
            batch_id=new_id,
            samples_status=_samples_status(batch),
            duplicate=False,
        )

    def approve_and_start(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        version: ApproveIn | None = None,
    ) -> RevenueOSCampaignStartOut:
        """`version` is the batch and digest the reviewer read (ADR-0025): any
        other samples are refused. Without it the latest batch is approved."""
        batch = self.personalization.latest_batch(context, campaign_id)
        campaign = self.personalization.campaigns.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        assert campaign is not None
        if campaign["status"] in _STARTED_STATUSES:
            # A repeat after success: the start service answers already_started.
            return RevenueOSStartService(self.session).start(context, campaign_id)

        if version is not None:
            if batch is None or batch.batch_id != version.batch_id:
                raise AppError(
                    "batch_not_latest",
                    "These are not the latest samples. Read the samples again.",
                    status_code=409,
                )
            if version.config_digest != batch.current_digest:
                raise AppError(
                    "approval_stale",
                    "The objective or emails changed after these samples were "
                    "generated. Read the samples again.",
                    status_code=409,
                )

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


def _email_out(number: int, item: PreviewItemOut) -> RevenueOSSampleEmailOut:
    return RevenueOSSampleEmailOut(
        email_number=number,
        state=item.state,
        subject=item.subject,
        body_html=item.body_html,
        fallback_used=item.fallback_used,
        failure_codes=item.failure_codes,
    )
