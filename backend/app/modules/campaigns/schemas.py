from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Weekday bit order (documented here since no migration comment defines it):
# bit 0 = Monday ... bit 6 = Sunday (ISO week order). A `weekdays` list of
# ISO weekday integers (1=Monday .. 7=Sunday) is the wire format; the service
# layer converts to/from the `weekday_set` integer bitmask stored in
# campaign_settings_versions.weekday_set.

# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------


CampaignType = Literal["STANDARD", "HYPER_PERSONALIZED"]
# Optional orderings of the campaign list. With none given the list keeps its
# original id order (and its original cursors).
CampaignSort = Literal["updated_desc", "created_desc", "name_asc"]


class CampaignCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    # Chosen once and immutable (ADR-0011).
    campaign_type: CampaignType = "STANDARD"


class CampaignUpdateIn(BaseModel):
    # The campaign type cannot be changed after creation; an unknown field is
    # rejected rather than silently ignored.
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class CampaignArchiveIn(BaseModel):
    expected_version: int = Field(ge=1)


class CampaignDuplicateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)


class CampaignListItem(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    description: str | None = None
    status: str
    campaign_type: CampaignType = "STANDARD"
    draft_sequence_id: UUID | None = None
    draft_audience_id: UUID | None = None
    current_settings_id: UUID | None = None
    # True once the campaign was activated (it then has send history and can only
    # be erased, never purged); erased_at is set once its personal data was erased.
    is_activated: bool = False
    erased_at: datetime | None = None
    paused_at: datetime | None = None
    start_at: datetime | None = None
    # Display name of the member who created it; None when they have no name
    # set or are no longer in the workspace directory.
    creator_name: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class CampaignPage(BaseModel):
    items: list[CampaignListItem]
    next_cursor: str | None = None


class CampaignCountsOut(BaseModel):
    # Campaigns per status, for the statuses that have any.
    by_status: dict[str, int]
    not_archived: int
    total: int


class CampaignDetailOut(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    description: str | None = None
    creator_id: UUID
    status: str
    campaign_type: CampaignType = "STANDARD"
    start_at: datetime | None = None
    draft_sequence_id: UUID | None = None
    draft_audience_id: UUID | None = None
    current_settings_id: UUID | None = None
    planning_status: str
    archived_at: datetime | None = None
    error_reason: str | None = None
    is_activated: bool = False
    erased_at: datetime | None = None
    # Pause bookkeeping (migration 0040). paused_at is set only while PAUSED.
    paused_at: datetime | None = None
    paused_total_seconds: int = 0
    last_resumed_at: datetime | None = None
    # Only set on the response to a pause command: messages that were already
    # QUEUED/SENDING/UNKNOWN_OUTCOME and cannot be recalled.
    outstanding_attempts: int | None = None
    version: int
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Sequence + steps
# ---------------------------------------------------------------------------


class SequenceStepCreateIn(BaseModel):
    kind: Literal["EMAIL", "WAIT"]
    position: int = Field(ge=1)
    # EMAIL payload
    email_subject: str | None = Field(default=None, min_length=1, max_length=500)
    email_body_html: str | None = Field(default=None, max_length=200_000)
    # "" clears; None means "not provided".
    email_preheader: str | None = Field(default=None, max_length=255)
    source_template_version_id: UUID | None = None
    # EMAIL only: also insert a WAIT of this many minutes directly before the
    # new step, atomically, so alternation can never be left half-built.
    leading_wait_minutes: int | None = Field(default=None, gt=0, le=525_600)
    # WAIT payload (at most one year)
    wait_duration_minutes: int | None = Field(default=None, gt=0, le=525_600)


class SequenceStepUpdateIn(BaseModel):
    expected_version: int = Field(ge=1)
    email_subject: str | None = Field(default=None, min_length=1, max_length=500)
    email_body_html: str | None = Field(default=None, max_length=200_000)
    # "" clears the pre-header; None leaves it unchanged.
    email_preheader: str | None = Field(default=None, max_length=255)
    source_template_version_id: UUID | None = None
    wait_duration_minutes: int | None = Field(default=None, gt=0, le=525_600)


class SequenceStepReorderEntry(BaseModel):
    step_id: UUID
    position: int = Field(ge=1)


class SequenceStepsReorderIn(BaseModel):
    steps: list[SequenceStepReorderEntry] = Field(min_length=1)


class StepAttachmentOut(BaseModel):
    id: UUID
    step_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    disposition: Literal["ATTACHMENT", "INLINE"]
    # The token used in the body as <img src="cid:CONTENT_ID">.
    content_id: str
    created_at: datetime


class StepAttachmentUrlOut(BaseModel):
    url: str
    expires_in: int


class SequenceStepOut(BaseModel):
    id: UUID
    sequence_id: UUID
    campaign_id: UUID
    position: int
    kind: str
    email_subject: str | None = None
    email_body_html: str | None = None
    email_preheader: str | None = None
    attachments: list[StepAttachmentOut] = Field(default_factory=list)
    email_variable_schema: dict[str, Any] | None = None
    wait_duration_minutes: int | None = None
    source_template_version_id: UUID | None = None
    # Activated campaigns only (ADR-0020): how many leads already received this
    # email, and how many unsent messages an edit would still change.
    sent_count: int = 0
    unsent_count: int = 0
    # Personalized campaigns: emails already written (frozen) vs still to be
    # written from the step's current reference.
    written_count: int = 0
    unwritten_count: int = 0
    version: int
    created_at: datetime
    updated_at: datetime


class SequenceOut(BaseModel):
    id: UUID | None = None
    campaign_id: UUID
    revision: int
    status: str
    steps: list[SequenceStepOut]
    # True when the current user may edit this activated sequence right now
    # (campaign paused, campaigns.execute). For a personalized campaign only the
    # wording of steps with no written email can change (see written_count).
    # DRAFT editing is unchanged and reported by the campaign status as before.
    paused_edit_allowed: bool = False
    # Steps at or before this position were already reached by a lead: their
    # content and waits may change, their place in the sequence may not.
    structure_locked_through: int = 0


class PreviewRecipientOut(BaseModel):
    audience_member_id: UUID
    lead_id: UUID
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    # Exactly what the message will render with (the captured snapshot), so the
    # preview never disagrees with what is sent.
    variables: dict[str, Any]


class PreviewRecipientsOut(BaseModel):
    # "NONE" until the campaign has a ready audience; the UI then falls back to
    # sample data.
    source: Literal["AUDIENCE", "NONE"]
    items: list[PreviewRecipientOut]
    total: int | None = None
    next_cursor: int | None = None


class StepTestSendIn(BaseModel):
    mailbox_id: UUID
    recipient_email: str = Field(min_length=3, max_length=320)
    # The sender must explicitly confirm the destination: a test email goes to a
    # real inbox, so it is never sent to a default or implied address.
    confirm_recipient: bool
    audience_member_id: UUID | None = None
    # Unsaved editor content overrides the saved step for the test only.
    email_subject: str | None = Field(default=None, min_length=1, max_length=500)
    email_body_html: str | None = Field(default=None, max_length=200_000)
    email_preheader: str | None = Field(default=None, max_length=255)


# ---------------------------------------------------------------------------
# Mailbox assignment
# ---------------------------------------------------------------------------


class CampaignMailboxAssignIn(BaseModel):
    mailbox_id: UUID


class CampaignMailboxesReorderIn(BaseModel):
    mailbox_ids: list[UUID] = Field(min_length=1)


class CampaignMailboxOut(BaseModel):
    mailbox_id: UUID
    provider: str
    email_address: str
    sender_display_name: str | None = None
    connection_state: str
    health_state: str
    policy_state: str
    policy_reason: str | None = None
    active: bool
    allocation_position: int


# ---------------------------------------------------------------------------
# Schedule / settings
# ---------------------------------------------------------------------------


class CampaignSettingsCreateIn(BaseModel):
    timezone: str = Field(min_length=1, max_length=100)
    weekdays: list[int] = Field(min_length=1, max_length=7)
    window_start_local: time
    window_end_local: time
    daily_limit: int | None = Field(default=None, gt=0, le=100_000)

    @field_validator("weekdays")
    @classmethod
    def weekdays_must_be_iso(cls, value: list[int]) -> list[int]:
        if any(day < 1 or day > 7 for day in value):
            raise ValueError("weekdays must be ISO weekday integers 1 (Mon) - 7 (Sun)")
        if len(set(value)) != len(value):
            raise ValueError("weekdays must not contain duplicates")
        return sorted(set(value))

    @model_validator(mode="after")
    def window_start_before_end(self) -> CampaignSettingsCreateIn:
        if self.window_start_local >= self.window_end_local:
            raise ValueError(
                "window_start_local must be before window_end_local "
                "(same-day windows only; overnight windows are not supported in MVP)"
            )
        return self


class CampaignSettingsOut(BaseModel):
    id: UUID
    campaign_id: UUID
    revision: int
    timezone: str
    weekdays: list[int]
    window_start_local: time
    window_end_local: time
    daily_limit: int | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------


class AudienceSelectIn(BaseModel):
    list_ids: list[UUID] = Field(default_factory=list)
    lead_ids: list[UUID] = Field(default_factory=list)


class AudienceOut(BaseModel):
    id: UUID
    campaign_id: UUID
    revision: int
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    is_committed: bool = False
    total_candidates: int | None = None
    processed_count: int = 0
    accepted_count: int | None = None
    excluded_count: int | None = None
    error_reason: str | None = None
    # What this revision was captured from, so a client can add to it (the next
    # capture replaces the selection rather than extending it).
    selected_list_ids: list[UUID] = Field(default_factory=list)
    selected_lead_ids: list[UUID] = Field(default_factory=list)


class SupplementOut(BaseModel):
    """A set of leads being added to a paused, activated campaign (ADR-0020).

    `status` is the capture; `enroll_state` is the later step that turns the
    accepted leads into enrollments. A lead already in the campaign, suppressed
    or archived is excluded (counted in `exclusions`), never enrolled twice."""

    id: UUID
    campaign_id: UUID
    revision: int
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    total_candidates: int | None = None
    processed_count: int = 0
    accepted_count: int | None = None
    excluded_count: int | None = None
    exclusions: dict[str, int] = Field(default_factory=dict)
    # NOT_STARTED | PENDING | PROCESSING | READY | FAILED
    enroll_state: str = "NOT_STARTED"
    render_state: str = "NOT_STARTED"
    enrolled_count: int = 0
    error_reason: str | None = None


class AudienceExclusionBreakdown(BaseModel):
    """Why excluded_count leads were excluded, for the audience review UI.

    archived_lead and suppressed are exact counts from campaign_audience_members.
    invalid_address_estimate is a residual (total_candidates minus every row
    actually stored) because a lead with no resolvable email address never
    gets a row there -- see workers/campaigns.py's insertable filter -- so it
    can only be known in aggregate, not per lead.
    """

    archived_lead: int = 0
    suppressed: int = 0
    invalid_address_estimate: int = 0


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class PreflightIssue(BaseModel):
    code: str
    message: str
    field_path: str | None = None


class PreflightResult(BaseModel):
    ready: bool
    errors: list[PreflightIssue]
    warnings: list[PreflightIssue]


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class CampaignReviewOut(BaseModel):
    campaign: CampaignDetailOut
    sequence: SequenceOut | None = None
    mailboxes: list[CampaignMailboxOut]
    settings: CampaignSettingsOut | None = None
    audience: AudienceOut | None = None
    preflight: PreflightResult


# ---------------------------------------------------------------------------
# Activation / planning
# ---------------------------------------------------------------------------


class ActivateIn(BaseModel):
    expected_version: int = Field(ge=1)
    start_at: datetime | None = None


class PauseIn(BaseModel):
    expected_version: int = Field(ge=1)


class ResumeIn(BaseModel):
    expected_version: int = Field(ge=1)
    # Leads that already finished the sequence are given the emails appended while the
    # campaign was paused (ADR-0020). Off unless the user asks: a finished lead is never
    # contacted again by surprise.
    include_completed: bool = False


class PlanningJobStatusOut(BaseModel):
    phase: str
    state: str
    processed_count: int
    total_count: int | None = None
    error_reason: str | None = None


class CampaignPlanningOut(BaseModel):
    campaign_id: UUID
    planning_status: str
    enroll: PlanningJobStatusOut | None = None
    render: PlanningJobStatusOut | None = None
