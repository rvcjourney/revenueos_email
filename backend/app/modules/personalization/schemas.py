"""HTTP contracts of the personalization API (ADR-0011)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.modules.campaigns.schemas import SequenceOut
from app.modules.personalization.brand_kit import BrandKit
from app.modules.personalization.config_schema import (
    CompanyProfile,
    PersonalizationConfig,
)

ApprovalStatus = Literal["NONE", "APPROVED", "STALE"]

MAX_REGEN_INSTRUCTIONS = 1000


class ApprovalOut(BaseModel):
    status: ApprovalStatus
    approved_at: datetime | None = None
    approved_by: UUID | None = None


class PersonalizationCapabilitiesOut(BaseModel):
    enabled: bool
    model: str | None = None
    # Whether this API process can make interactive AI calls (company analysis and
    # reference drafting): the flag, the model and the key are all present. When
    # false the manual objective and step editor keep working (ADR-0016).
    ai_drafting_available: bool = False


class PersonalizationStateOut(BaseModel):
    campaign_id: UUID
    campaign_type: str
    enabled: bool
    model: str | None = None
    config: PersonalizationConfig | None = None
    # The draft sequence's version, sent back as `expected_version` when saving.
    config_version: int | None = None
    # What a sample approval is bound to right now (objective + reference
    # templates + prompt version + model).
    config_digest: str
    approval: ApprovalOut


class PersonalizationConfigIn(BaseModel):
    config: PersonalizationConfig
    expected_version: int | None = Field(default=None, ge=1)


class PreviewCreateIn(BaseModel):
    # Client-generated: makes a retried request return the same batch instead of
    # spending more of the daily preview budget.
    batch_id: UUID
    # Exactly one lead, chosen by the user: every sample is a model call per
    # email step, so the server never fans out to leads the user did not pick.
    audience_member_ids: list[UUID] = Field(min_length=1, max_length=1)


class RevenueOSPreviewCreateIn(PreviewCreateIn):
    # RevenueOS names 3-4 different leads so its reviewer sees the range
    # (ADR-0025). Still only leads it picked: the server never fans out.
    audience_member_ids: list[UUID] = Field(min_length=1, max_length=4)


class PreviewRecipientOut(BaseModel):
    audience_member_id: UUID
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    title: str | None = None


class PreviewItemOut(BaseModel):
    id: UUID
    recipient: PreviewRecipientOut
    step_id: UUID
    step_position: int
    state: Literal["PENDING", "OK", "FAILED"]
    subject: str | None = None
    body_html: str | None = None
    facts: list[dict[str, Any]] = Field(default_factory=list)
    research_summary: dict[str, Any] | None = None
    fallback_used: bool = False
    failure_codes: list[str] = Field(default_factory=list)


class PreviewBatchOut(BaseModel):
    batch_id: UUID
    config_digest: str
    current_digest: str
    # True when the objective/templates changed after this batch was generated.
    stale: bool
    created_at: datetime
    expires_at: datetime
    complete: bool
    all_ok: bool
    items: list[PreviewItemOut]


class ApproveIn(BaseModel):
    batch_id: UUID
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class BudgetOut(BaseModel):
    generation_used: int
    generation_cap: int
    preview_used: int
    preview_cap: int
    fetch_used: int
    fetch_cap: int


class GenerationProgressOut(BaseModel):
    campaign_id: UUID
    pending: int
    succeeded: int
    failed: int
    superseded: int
    fallback: int
    oldest_pending_at: datetime | None = None
    failure_codes: dict[str, int] = Field(default_factory=dict)
    budget: BudgetOut


class BusinessInfoIn(BaseModel):
    """Bounds only guard the payload size; the precise rules (and their error
    code) are applied by the analyzer so they exist once."""

    company_name: str = Field(max_length=500)
    description: str = Field(max_length=10_000)


class CompanyAnalysisIn(BaseModel):
    # Exactly one of the two; enforced by the service so the error code is stable.
    url: str | None = Field(default=None, max_length=4096)
    business: BusinessInfoIn | None = None


class SuggestionsOut(BaseModel):
    objective: str = ""
    offer: str = ""
    cta: str = ""
    tone: str = ""


class CompanyAnalysisOut(BaseModel):
    source: Literal["WEBSITE", "MANUAL"]
    final_url: str | None = None
    profile: CompanyProfile
    # Only for a website: a business description has nothing to extract a brand from.
    brand: BrandKit | None = None
    suggestions: SuggestionsOut
    pages_read: int
    warnings: list[str] = Field(default_factory=list)
    brand_warnings: list[str] = Field(default_factory=list)
    model: str | None = None


class LayoutPreviewIn(BaseModel):
    brand: BrandKit
    company_name: str = Field(default="", max_length=120)
    site_url: str | None = Field(default=None, max_length=1000)
    cta_label: str | None = Field(default=None, max_length=40)


class LayoutPreviewOut(BaseModel):
    html: str
    warnings: list[str] = Field(default_factory=list)


class ReferenceTemplatesIn(BaseModel):
    scope: Literal["ALL", "STEP"] = "ALL"
    # STEP only: the email step to regenerate.
    step_id: UUID | None = None
    # Only used when the sequence has no email steps yet: how many follow-ups to
    # create after the first email.
    follow_up_count: int | None = Field(default=None, ge=1, le=5)
    # STEP only: what the user wants changed in the regenerated email.
    instructions: str | None = Field(default=None, max_length=MAX_REGEN_INSTRUCTIONS)

    @model_validator(mode="after")
    def _instructions_only_for_step(self) -> ReferenceTemplatesIn:
        if self.instructions is not None:
            self.instructions = self.instructions.strip() or None
        if self.instructions is not None and self.scope != "STEP":
            raise ValueError("instructions apply only when regenerating one email")
        return self


class ReferenceTemplatesOut(BaseModel):
    sequence: SequenceOut
    model: str
    theme: str = ""
    attempts: int = 1
    warnings: list[str] = Field(default_factory=list)
