from __future__ import annotations

from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_db, get_workspace_context
from app.core.permissions import require_permission
from app.modules.bulk.runner import run_bulk
from app.modules.campaigns.activation_service import CampaignActivationService
from app.modules.campaigns.attachment_service import StepAttachmentService
from app.modules.campaigns.attachments import MAX_FILE_BYTES
from app.modules.campaigns.audience_service import AudienceService
from app.modules.campaigns.mailbox_service import CampaignMailboxService
from app.modules.campaigns.preflight import PreflightService
from app.modules.campaigns.review import ReviewService
from app.modules.campaigns.schemas import (
    ActivateIn,
    AudienceExclusionBreakdown,
    AudienceOut,
    AudienceSelectIn,
    CampaignArchiveIn,
    CampaignCountsOut,
    CampaignCreateIn,
    CampaignDetailOut,
    CampaignDuplicateIn,
    CampaignMailboxAssignIn,
    CampaignMailboxesReorderIn,
    CampaignMailboxOut,
    CampaignPage,
    CampaignPlanningOut,
    CampaignReviewOut,
    CampaignSettingsCreateIn,
    CampaignSettingsOut,
    CampaignSort,
    CampaignType,
    CampaignUpdateIn,
    PauseIn,
    PreflightResult,
    PreviewRecipientsOut,
    ResumeIn,
    SequenceOut,
    SequenceStepCreateIn,
    SequenceStepOut,
    SequenceStepsReorderIn,
    SequenceStepUpdateIn,
    StepAttachmentOut,
    StepAttachmentUrlOut,
    StepTestSendIn,
    SupplementOut,
)
from app.modules.campaigns.sequence_service import SequenceService
from app.modules.campaigns.service import CampaignService
from app.modules.campaigns.supplement_service import SupplementService
from app.modules.campaigns.settings_service import CampaignSettingsService
from app.modules.campaigns.step_test_send_service import StepTestSendService
from app.modules.leads.pagination import DEFAULT_LIMIT
from app.modules.mailboxes.schemas import MailboxTestSendResult
from app.schemas.bulk import BulkActionIn, BulkActionOut

router = APIRouter()


# ---------------------------------------------------------------------------
# Campaign CRUD
# ---------------------------------------------------------------------------


@router.get("/campaigns", response_model=CampaignPage)
def list_campaigns(
    limit: int = Query(DEFAULT_LIMIT, ge=1),
    cursor: str | None = Query(default=None, max_length=512),
    q: str | None = Query(default=None, max_length=200),
    status_filter: str | None = Query(default=None, alias="status"),
    type_filter: CampaignType | None = Query(default=None, alias="type"),
    sort: CampaignSort | None = Query(default=None),
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> CampaignPage:
    return CampaignService(db).list_campaigns(
        context,
        limit=limit,
        cursor=cursor,
        query=q,
        status=status_filter,
        campaign_type=type_filter,
        sort=sort,
    )


@router.get("/campaigns/counts", response_model=CampaignCountsOut)
def count_campaigns(
    q: str | None = Query(default=None, max_length=200),
    type_filter: CampaignType | None = Query(default=None, alias="type"),
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> CampaignCountsOut:
    """How many campaigns each status holds, for the list's status tabs."""
    return CampaignService(db).count_campaigns(
        context, query=q, campaign_type=type_filter
    )


@router.post(
    "/campaigns", response_model=CampaignDetailOut, status_code=status.HTTP_201_CREATED
)
def create_campaign(
    payload: CampaignCreateIn,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=200
    ),
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> CampaignDetailOut:
    return CampaignService(db).create_campaign(context, payload)


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> CampaignDetailOut:
    return CampaignService(db).get_campaign(context, campaign_id)


@router.patch("/campaigns/{campaign_id}", response_model=CampaignDetailOut)
def update_campaign(
    campaign_id: UUID,
    payload: CampaignUpdateIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> CampaignDetailOut:
    return CampaignService(db).update_campaign(context, campaign_id, payload)


@router.post("/campaigns/{campaign_id}/archive", response_model=CampaignDetailOut)
def archive_campaign(
    campaign_id: UUID,
    payload: CampaignArchiveIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> CampaignDetailOut:
    return CampaignService(db).archive_campaign(
        context, campaign_id, payload.expected_version
    )


@router.post("/campaigns/bulk-archive", response_model=BulkActionOut)
def bulk_archive_campaigns(
    payload: BulkActionIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> BulkActionOut:
    service = CampaignService(db)
    return run_bulk(
        db,
        payload,
        lambda item: service.archive_campaign(context, item.id, item.expected_version),
    )


@router.post(
    "/campaigns/{campaign_id}/duplicate",
    response_model=CampaignDetailOut,
    status_code=status.HTTP_201_CREATED,
)
def duplicate_campaign(
    campaign_id: UUID,
    payload: CampaignDuplicateIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> CampaignDetailOut:
    return CampaignService(db).duplicate_campaign(context, campaign_id, payload)


# ---------------------------------------------------------------------------
# Activation / planning
# ---------------------------------------------------------------------------


@router.post("/campaigns/{campaign_id}/activate", response_model=CampaignDetailOut)
def activate_campaign(
    campaign_id: UUID,
    payload: ActivateIn,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=200
    ),
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
) -> CampaignDetailOut:
    return CampaignActivationService(db).activate(
        context, campaign_id, payload, idempotency_key
    )


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignDetailOut)
def pause_campaign(
    campaign_id: UUID,
    payload: PauseIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
) -> CampaignDetailOut:
    return CampaignActivationService(db).pause(context, campaign_id, payload)


@router.post("/campaigns/{campaign_id}/resume", response_model=CampaignDetailOut)
def resume_campaign(
    campaign_id: UUID,
    payload: ResumeIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
) -> CampaignDetailOut:
    return CampaignActivationService(db).resume(context, campaign_id, payload)


@router.post(
    "/campaigns/{campaign_id}/planning/retry",
    response_model=CampaignPlanningOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_campaign_planning(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
) -> CampaignPlanningOut:
    return CampaignActivationService(db).retry_planning(context, campaign_id)


@router.get("/campaigns/{campaign_id}/planning", response_model=CampaignPlanningOut)
def get_campaign_planning(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> CampaignPlanningOut:
    return CampaignActivationService(db).get_planning_status(context, campaign_id)


# ---------------------------------------------------------------------------
# Sequence + steps
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/sequence", response_model=SequenceOut)
def get_sequence(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> SequenceOut:
    return SequenceService(db).get_sequence(context, campaign_id)


@router.post(
    "/campaigns/{campaign_id}/sequence/steps",
    response_model=SequenceStepOut,
    status_code=status.HTTP_201_CREATED,
)
def add_sequence_step(
    campaign_id: UUID,
    payload: SequenceStepCreateIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> SequenceStepOut:
    return SequenceService(db).add_step(context, campaign_id, payload)


@router.patch(
    "/campaigns/{campaign_id}/sequence/steps/{step_id}", response_model=SequenceStepOut
)
def update_sequence_step(
    campaign_id: UUID,
    step_id: UUID,
    payload: SequenceStepUpdateIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> SequenceStepOut:
    return SequenceService(db).update_step(context, campaign_id, step_id, payload)


@router.get(
    "/campaigns/{campaign_id}/sequence/preview-recipients",
    response_model=PreviewRecipientsOut,
)
def list_sequence_preview_recipients(
    campaign_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    after_ordinal: int | None = Query(default=None, ge=0),
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> PreviewRecipientsOut:
    return SequenceService(db).list_preview_recipients(
        context, campaign_id, limit=limit, after_ordinal=after_ordinal
    )


@router.post(
    "/campaigns/{campaign_id}/sequence/steps/{step_id}/test-send",
    response_model=MailboxTestSendResult,
)
def test_send_sequence_step(
    campaign_id: UUID,
    step_id: UUID,
    payload: StepTestSendIn,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=200
    ),
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
) -> MailboxTestSendResult:
    return StepTestSendService(db).send(
        context, campaign_id, step_id, payload, idempotency_key
    )


@router.post(
    "/campaigns/{campaign_id}/sequence/steps/{step_id}/attachments",
    response_model=StepAttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
def upload_step_attachment(
    campaign_id: UUID,
    step_id: UUID,
    response: Response,
    file: UploadFile = File(...),
    disposition: str = Form(default="ATTACHMENT"),
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> StepAttachmentOut:
    # Read at most one byte over the cap so an oversized upload is rejected
    # without buffering the whole body.
    data = file.file.read(MAX_FILE_BYTES + 1)
    attachment, created = StepAttachmentService(db).upload(
        context,
        campaign_id,
        step_id,
        filename=file.filename,
        data=data,
        disposition=disposition,
    )
    if not created:
        # The same file was already attached: idempotent no-op.
        response.status_code = status.HTTP_200_OK
    return attachment


@router.delete(
    "/campaigns/{campaign_id}/sequence/steps/{step_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_step_attachment(
    campaign_id: UUID,
    step_id: UUID,
    attachment_id: UUID,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> None:
    StepAttachmentService(db).delete(context, campaign_id, step_id, attachment_id)


@router.get(
    "/campaigns/{campaign_id}/sequence/steps/{step_id}/attachments/{attachment_id}/url",
    response_model=StepAttachmentUrlOut,
)
def get_step_attachment_url(
    campaign_id: UUID,
    step_id: UUID,
    attachment_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> StepAttachmentUrlOut:
    return StepAttachmentService(db).signed_url(
        context, campaign_id, step_id, attachment_id
    )


@router.post(
    "/campaigns/{campaign_id}/sequence/steps/{step_id}/duplicate",
    response_model=SequenceOut,
    status_code=status.HTTP_201_CREATED,
)
def duplicate_sequence_step(
    campaign_id: UUID,
    step_id: UUID,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> SequenceOut:
    return SequenceService(db).duplicate_step(context, campaign_id, step_id)


@router.post(
    "/campaigns/{campaign_id}/sequence/steps/reorder", response_model=SequenceOut
)
def reorder_sequence_steps(
    campaign_id: UUID,
    payload: SequenceStepsReorderIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> SequenceOut:
    return SequenceService(db).reorder_steps(context, campaign_id, payload)


@router.delete(
    "/campaigns/{campaign_id}/sequence/steps/{step_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_sequence_step(
    campaign_id: UUID,
    step_id: UUID,
    with_adjacent_wait: bool = Query(default=False),
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> None:
    SequenceService(db).delete_step(
        context, campaign_id, step_id, with_adjacent_wait=with_adjacent_wait
    )


# ---------------------------------------------------------------------------
# Mailbox assignment
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign_id}/mailboxes", response_model=list[CampaignMailboxOut]
)
def list_campaign_mailboxes(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[CampaignMailboxOut]:
    return CampaignMailboxService(db).list_mailboxes(context, campaign_id)


@router.post(
    "/campaigns/{campaign_id}/mailboxes", response_model=list[CampaignMailboxOut]
)
def assign_campaign_mailbox(
    campaign_id: UUID,
    payload: CampaignMailboxAssignIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> list[CampaignMailboxOut]:
    return CampaignMailboxService(db).assign_mailbox(context, campaign_id, payload)


@router.delete(
    "/campaigns/{campaign_id}/mailboxes/{mailbox_id}",
    response_model=list[CampaignMailboxOut],
)
def unassign_campaign_mailbox(
    campaign_id: UUID,
    mailbox_id: UUID,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> list[CampaignMailboxOut]:
    return CampaignMailboxService(db).unassign_mailbox(context, campaign_id, mailbox_id)


@router.post(
    "/campaigns/{campaign_id}/mailboxes/reorder",
    response_model=list[CampaignMailboxOut],
)
def reorder_campaign_mailboxes(
    campaign_id: UUID,
    payload: CampaignMailboxesReorderIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> list[CampaignMailboxOut]:
    return CampaignMailboxService(db).reorder_mailboxes(context, campaign_id, payload)


# ---------------------------------------------------------------------------
# Schedule / settings
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign_id}/settings", response_model=list[CampaignSettingsOut]
)
def list_campaign_settings(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[CampaignSettingsOut]:
    return list(CampaignSettingsService(db).list_history(context, campaign_id))


@router.post(
    "/campaigns/{campaign_id}/settings",
    response_model=CampaignSettingsOut,
    status_code=status.HTTP_201_CREATED,
)
def create_campaign_settings(
    campaign_id: UUID,
    payload: CampaignSettingsCreateIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> CampaignSettingsOut:
    return CampaignSettingsService(db).create_settings_version(
        context, campaign_id, payload
    )


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/audience", response_model=AudienceOut | None)
def get_committed_audience(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> AudienceOut | None:
    return AudienceService(db).get_committed_audience(context, campaign_id)


@router.post(
    "/campaigns/{campaign_id}/audience/select",
    response_model=AudienceOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def select_audience(
    campaign_id: UUID,
    payload: AudienceSelectIn,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=200
    ),
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> AudienceOut:
    return AudienceService(db).select_audience(context, campaign_id, payload)


# Supplement routes are registered before "/audience/{audience_id}" on purpose:
# "supplements" would otherwise be parsed as an audience id (and rejected as 422).
@router.get(
    "/campaigns/{campaign_id}/audience/supplements",
    response_model=list[SupplementOut],
)
def list_supplement_audiences(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[SupplementOut]:
    return SupplementService(db).list_supplements(context, campaign_id)


@router.post(
    "/campaigns/{campaign_id}/audience/supplements",
    response_model=SupplementOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def select_supplement_audience(
    campaign_id: UUID,
    payload: AudienceSelectIn,
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
) -> SupplementOut:
    return SupplementService(db).select(context, campaign_id, payload)


@router.post(
    "/campaigns/{campaign_id}/audience/supplements/{audience_id}/enroll",
    response_model=SupplementOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def enroll_supplement_audience(
    campaign_id: UUID,
    audience_id: UUID,
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
) -> SupplementOut:
    return SupplementService(db).enroll(context, campaign_id, audience_id)


@router.post(
    "/campaigns/{campaign_id}/audience/supplements/{audience_id}/retry",
    response_model=SupplementOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_supplement_audience(
    campaign_id: UUID,
    audience_id: UUID,
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
) -> SupplementOut:
    return SupplementService(db).retry(context, campaign_id, audience_id)


@router.get(
    "/campaigns/{campaign_id}/audience/{audience_id}", response_model=AudienceOut
)
def get_audience_capture_status(
    campaign_id: UUID,
    audience_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> AudienceOut:
    return AudienceService(db).get_audience(context, campaign_id, audience_id)


@router.get(
    "/campaigns/{campaign_id}/audience/{audience_id}/exclusions",
    response_model=AudienceExclusionBreakdown,
)
def get_audience_exclusions(
    campaign_id: UUID,
    audience_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> AudienceExclusionBreakdown:
    return AudienceService(db).get_audience_exclusions(
        context, campaign_id, audience_id
    )


@router.post(
    "/campaigns/{campaign_id}/audience/{audience_id}/commit", response_model=AudienceOut
)
def commit_audience(
    campaign_id: UUID,
    audience_id: UUID,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> AudienceOut:
    return AudienceService(db).commit_audience(context, campaign_id, audience_id)


@router.post(
    "/campaigns/{campaign_id}/audience/{audience_id}/abandon",
    status_code=status.HTTP_204_NO_CONTENT,
)
def abandon_audience(
    campaign_id: UUID,
    audience_id: UUID,
    context: WorkspaceContext = Depends(require_permission("campaigns.draft")),
    db: Session = Depends(get_db),
) -> None:
    AudienceService(db).abandon_audience(context, campaign_id, audience_id)


# ---------------------------------------------------------------------------
# Preflight + Review
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/preflight", response_model=PreflightResult)
def run_preflight(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> PreflightResult:
    return PreflightService(db).run(context, campaign_id)


@router.get("/campaigns/{campaign_id}/review", response_model=CampaignReviewOut)
def get_review(
    campaign_id: UUID,
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> CampaignReviewOut:
    return ReviewService(db).get_review(context, campaign_id)
