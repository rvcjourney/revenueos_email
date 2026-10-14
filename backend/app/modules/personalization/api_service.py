"""Personalization API service (ADR-0011): objective, sample previews, approval,
generation progress. Runs as the request's app_api session; authorization is
enforced by the route's capability dependency AND by RLS, never by the UI."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import RowMapping
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.config import Settings
from app.core.errors import AppError
from app.modules.campaigns.repository import CampaignRepository
from app.modules.personalization.api_repository import PersonalizationApiRepository
from app.modules.personalization.brand_kit import brand_warnings
from app.modules.personalization.brand_layout import render_layout_preview
from app.modules.personalization.company_analysis import (
    AnalysisOutcome,
    CompanyAnalyzer,
)
from app.modules.personalization.config_schema import (
    PersonalizationConfig,
    compute_approval_digest,
    parse_config,
)
from app.modules.personalization.ports import DraftingModel
from app.modules.personalization.reference_templates import ReferenceTemplateService
from app.modules.personalization.research.egress import SafeFetcher
from app.modules.personalization.schemas import (
    ApprovalOut,
    ApproveIn,
    BudgetOut,
    CompanyAnalysisIn,
    CompanyAnalysisOut,
    GenerationProgressOut,
    LayoutPreviewIn,
    LayoutPreviewOut,
    PersonalizationCapabilitiesOut,
    PersonalizationConfigIn,
    PersonalizationStateOut,
    PreviewBatchOut,
    PreviewCreateIn,
    PreviewItemOut,
    PreviewRecipientOut,
    ReferenceTemplatesIn,
    ReferenceTemplatesOut,
    SuggestionsOut,
)
from app.modules.personalization.version import CAMPAIGN_TYPE_HYPER



def _draft_email_steps(steps: list[RowMapping]) -> list[RowMapping]:
    return [s for s in steps if s["kind"] == "EMAIL"]


class PersonalizationApiService:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        *,
        drafting_model_factory: Callable[[], DraftingModel] | None = None,
        fetcher: SafeFetcher | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or Settings.current()
        self.campaigns = CampaignRepository(session)
        self.repo = PersonalizationApiRepository(session)
        # Injectable so tests never build the real adapter (no network in tests).
        self._drafting_model_factory = drafting_model_factory
        self._fetcher = fetcher

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _campaign(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        *,
        require_draft: bool = False,
        require_enabled: bool = False,
    ) -> RowMapping:
        campaign = self.campaigns.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if campaign.get("campaign_type") != CAMPAIGN_TYPE_HYPER:
            raise AppError(
                "not_personalized_campaign",
                "This campaign is not a hyper-personalized campaign",
                status_code=409,
            )
        if require_enabled and not self.settings.personalization_enabled:
            raise AppError(
                "personalization_disabled",
                "Hyper-personalized campaigns are not enabled for this deployment",
                status_code=409,
            )
        if require_draft and campaign["status"] != "DRAFT":
            raise AppError(
                "state_conflict",
                "The objective and samples can only be edited while the "
                "campaign is DRAFT",
                status_code=409,
            )
        return campaign

    def _sequence_and_steps(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> tuple[RowMapping | None, list[RowMapping]]:
        sequence = self.campaigns.get_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if sequence is None:
            return None, []
        steps = self.campaigns.list_steps_ordered(
            workspace_id=context.workspace_id, sequence_id=UUID(str(sequence["id"]))
        )
        return sequence, steps

    def current_digest(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> tuple[str, RowMapping | None, list[RowMapping]]:
        sequence, steps = self._sequence_and_steps(context, campaign_id)
        config = sequence["personalization_config"] if sequence is not None else None
        digest = compute_approval_digest(
            config=config, steps=steps, model=self.settings.personalization_model
        )
        return digest, sequence, steps

    def approval_status(
        self, context: WorkspaceContext, campaign_id: UUID, digest: str | None = None
    ) -> ApprovalOut:
        if digest is None:
            digest, _, _ = self.current_digest(context, campaign_id)
        approvals = self.repo.list_approvals(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        for row in approvals:
            if row["config_digest"] == digest:
                return ApprovalOut(
                    status="APPROVED",
                    approved_at=row["approved_at"],
                    approved_by=row["approved_by"],
                )
        if approvals:
            return ApprovalOut(status="STALE")
        return ApprovalOut(status="NONE")

    # ------------------------------------------------------------------
    # capabilities / state / objective
    # ------------------------------------------------------------------

    def capabilities(self) -> PersonalizationCapabilitiesOut:
        enabled = self.settings.personalization_enabled
        return PersonalizationCapabilitiesOut(
            enabled=enabled,
            model=self.settings.personalization_model if enabled else None,
            ai_drafting_available=self.settings.personalization_drafting_ready,
        )

    # ------------------------------------------------------------------
    # AI authoring (ADR-0016): company analysis and layout preview
    # ------------------------------------------------------------------

    def _require_drafting_ready(self) -> None:
        if not self.settings.personalization_drafting_ready:
            raise AppError(
                "personalization_not_configured",
                "AI drafting isn't available in this environment. You can still "
                "write the objective and emails by hand.",
                status_code=503,
            )

    def _build_drafting_model(self) -> DraftingModel:
        if self._drafting_model_factory is not None:
            return self._drafting_model_factory()
        # Imported here so importing this module never constructs an adapter.
        from app.modules.personalization.factory import build_drafting_model

        return build_drafting_model(self.settings)

    def _release_connection(self) -> None:
        """End the request's transaction so the pooled connection is free while we
        wait on the network or the model (a person is waiting, and the pool is
        small and shared). Nothing after this may touch the database without
        `enter_api_scope`."""
        self.session.commit()

    def analyze_company(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: CompanyAnalysisIn,
    ) -> CompanyAnalysisOut:
        """Study a website, or a typed description. Reads the campaign to authorize,
        then releases the connection and does everything else without one. Writes
        nothing: the user reviews the result and saves it themselves."""
        self._campaign(context, campaign_id, require_draft=True, require_enabled=True)
        has_url = bool(payload.url and payload.url.strip())
        if has_url == (payload.business is not None):
            raise AppError(
                "company_source_required",
                "Provide either a website or your business details.",
                status_code=422,
            )
        self._require_drafting_ready()
        workspace_ref = str(context.workspace_id)
        self._release_connection()

        model = self._build_drafting_model()
        try:
            analyzer = CompanyAnalyzer(
                model=model,
                fetcher=self._fetcher,
                fetch_deadline_seconds=(
                    self.settings.personalization_analysis_fetch_deadline_seconds
                ),
            )
            if has_url:
                outcome = analyzer.analyze_website(
                    workspace_ref=workspace_ref, raw_url=str(payload.url)
                )
            else:
                assert payload.business is not None
                outcome = analyzer.analyze_business(
                    workspace_ref=workspace_ref,
                    company_name=payload.business.company_name,
                    description=payload.business.description,
                )
        finally:
            close = getattr(model, "close", None)
            if callable(close):
                close()
        return self._analysis_out(outcome)

    @staticmethod
    def _analysis_out(outcome: AnalysisOutcome) -> CompanyAnalysisOut:
        return CompanyAnalysisOut(
            source=outcome.source,  # type: ignore[arg-type]
            final_url=outcome.final_url,
            profile=outcome.profile,
            brand=outcome.brand,
            suggestions=SuggestionsOut(**outcome.suggestions),
            pages_read=outcome.pages_read,
            warnings=list(outcome.warnings),
            brand_warnings=brand_warnings(outcome.brand) if outcome.brand else [],
            model=outcome.model,
        )

    def generate_reference_templates(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: ReferenceTemplatesIn,
    ) -> ReferenceTemplatesOut:
        """Draft every reference email (or regenerate one) from the saved
        objective. See ReferenceTemplateService for the three phases."""
        service = ReferenceTemplateService(
            self.session,
            self.settings,
            campaign_guard=self._campaign,
            sequence_reader=self._sequence_and_steps,
            drafting_model_factory=self._build_drafting_model,
            audit=self.campaigns.record_audit_event,
        )
        sequence, outcome, warnings = service.generate(
            context,
            campaign_id,
            scope=payload.scope,
            step_id=payload.step_id,
            follow_up_count=payload.follow_up_count,
            instructions=payload.instructions,
        )
        return ReferenceTemplatesOut(
            sequence=sequence,
            model=outcome.model,
            theme=outcome.theme,
            attempts=outcome.attempts,
            warnings=warnings,
        )

    def layout_preview(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: LayoutPreviewIn,
    ) -> LayoutPreviewOut:
        """The brand layout around sample text, from the real renderer. No model,
        no fetch and no write, so it works even when AI drafting is unavailable."""
        self._campaign(context, campaign_id, require_draft=True, require_enabled=True)
        try:
            html = render_layout_preview(
                payload.brand,
                company_name=payload.company_name,
                site_url=payload.site_url,
                cta_label=payload.cta_label,
            )
        except ValueError as exc:
            raise AppError(
                "invalid_brand",
                "Some brand settings aren't valid. Check the colours and web "
                "addresses.",
                status_code=422,
            ) from exc
        return LayoutPreviewOut(html=html, warnings=brand_warnings(payload.brand))

    def get_state(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> PersonalizationStateOut:
        campaign = self._campaign(context, campaign_id)
        digest, sequence, _ = self.current_digest(context, campaign_id)
        config: PersonalizationConfig | None = None
        if sequence is not None:
            try:
                config = parse_config(sequence["personalization_config"])
            except ValidationError:
                config = None
        return PersonalizationStateOut(
            campaign_id=campaign_id,
            campaign_type=campaign["campaign_type"],
            enabled=self.settings.personalization_enabled,
            model=self.settings.personalization_model or None,
            config=config,
            config_version=int(sequence["version"]) if sequence is not None else None,
            config_digest=digest,
            approval=self.approval_status(context, campaign_id, digest),
        )

    def put_config(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: PersonalizationConfigIn,
    ) -> PersonalizationStateOut:
        self._campaign(context, campaign_id, require_draft=True, require_enabled=True)
        sequence = self.campaigns.get_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        ) or self.campaigns.create_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        updated = self.campaigns.set_sequence_personalization_config(
            workspace_id=context.workspace_id,
            sequence_id=UUID(str(sequence["id"])),
            config=payload.config.canonical(),
            expected_version=payload.expected_version,
        )
        if updated is None:
            raise AppError(
                "conflict",
                "The objective was modified by another user. Please refresh and "
                "try again.",
                status_code=409,
            )
        self.campaigns.record_audit_event(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            action="campaign.personalization_config",
            target_id=campaign_id,
        )
        return self.get_state(context, campaign_id)

    # ------------------------------------------------------------------
    # previews
    # ------------------------------------------------------------------

    def create_previews(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: PreviewCreateIn,
    ) -> PreviewBatchOut:
        campaign = self._campaign(
            context, campaign_id, require_draft=True, require_enabled=True
        )
        existing = self.repo.list_batch(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            batch_id=payload.batch_id,
        )
        if existing:
            # A retried request: same batch, no new spend, no second dispatch.
            return self._batch_out(context, campaign_id, payload.batch_id, existing)

        digest, sequence, steps = self.current_digest(context, campaign_id)
        try:
            config = (
                parse_config(sequence["personalization_config"]) if sequence else None
            )
        except ValidationError:
            config = None
        if config is None:
            raise AppError(
                "objective_missing",
                "Define the campaign objective before generating samples",
                status_code=422,
            )
        email_steps = _draft_email_steps(steps)
        if not email_steps:
            raise AppError(
                "sequence_empty",
                "Add at least one email step before generating samples",
                status_code=422,
            )

        members = self._sample_members(context, campaign, campaign_id, payload)
        rows = [
            {
                "audience_member_id": str(member["id"]),
                "lead_id": str(member["lead_id"]),
                "step_id": str(step["id"]),
            }
            for member in members
            for step in email_steps
        ]

        used = self.repo.usage_today(
            workspace_id=context.workspace_id, day=datetime.now(UTC).date()
        ).get("PREVIEW", 0)
        if used + len(rows) > self.settings.personalization_daily_preview_cap:
            raise AppError(
                "preview_budget_exhausted",
                "The daily limit for sample generation has been reached. "
                "Try again tomorrow.",
                status_code=429,
            )

        self.repo.insert_previews(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            batch_id=payload.batch_id,
            config_digest=digest,
            created_by=context.user_id,
            expires_at=datetime.now(UTC)
            + timedelta(hours=self.settings.personalization_preview_ttl_hours),
            rows=rows,
        )
        written = self.repo.list_batch(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            batch_id=payload.batch_id,
        )
        out = self._batch_out(context, campaign_id, payload.batch_id, written)
        self.session.commit()

        from app.services.task_dispatch import get_task_producer

        get_task_producer().send_task(
            "personalization.generate_previews",
            kwargs={
                "workspace_id": str(context.workspace_id),
                "campaign_id": str(campaign_id),
                "batch_id": str(payload.batch_id),
            },
            queue="personalization",
        )
        return out

    def _sample_members(
        self,
        context: WorkspaceContext,
        campaign: RowMapping,
        campaign_id: UUID,
        payload: PreviewCreateIn,
    ) -> list[Any]:
        audience_id = campaign["draft_audience_id"]
        if audience_id is None:
            raise AppError(
                "audience_not_selected",
                "Select and commit an audience before generating samples",
                status_code=422,
            )
        members = []
        for member_id in dict.fromkeys(payload.audience_member_ids):
            member = self.campaigns.get_accepted_audience_member(
                workspace_id=context.workspace_id,
                campaign_id=campaign_id,
                member_id=member_id,
            )
            if member is None:
                # Another tenant's or campaign's id resolves to nothing.
                raise AppError(
                    "not_found", "Audience member not found", status_code=404
                )
            members.append(member)
        return members

    def get_batch(
        self, context: WorkspaceContext, campaign_id: UUID, batch_id: UUID
    ) -> PreviewBatchOut:
        self._campaign(context, campaign_id)
        rows = self.repo.list_batch(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            batch_id=batch_id,
        )
        if not rows:
            raise AppError("not_found", "Preview batch not found", status_code=404)
        return self._batch_out(context, campaign_id, batch_id, rows)

    def latest_batch(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> PreviewBatchOut | None:
        self._campaign(context, campaign_id)
        batch_id = self.repo.latest_batch_id(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if batch_id is None:
            return None
        return self.get_batch(context, campaign_id, batch_id)

    def _batch_out(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        batch_id: UUID,
        rows: list[RowMapping],
    ) -> PreviewBatchOut:
        current, _, _ = self.current_digest(context, campaign_id)
        items = [
            PreviewItemOut(
                id=row["id"],
                recipient=PreviewRecipientOut(
                    audience_member_id=row["audience_member_id"],
                    email=_var(row, "email"),
                    first_name=_var(row, "first_name"),
                    last_name=_var(row, "last_name"),
                    company=_var(row, "company"),
                    title=_var(row, "title"),
                ),
                step_id=row["step_id"],
                step_position=int(row["step_position"]),
                state=row["state"],
                subject=row["subject"],
                body_html=row["body_html"],
                facts=list(row["facts"] or []),
                research_summary=row["research_summary"],
                fallback_used=bool(row["fallback_used"]),
                failure_codes=list(row["failure_codes"] or []),
            )
            for row in rows
        ]
        complete = all(item.state != "PENDING" for item in items)
        return PreviewBatchOut(
            batch_id=batch_id,
            config_digest=rows[0]["config_digest"],
            current_digest=current,
            stale=any(row["config_digest"] != current for row in rows),
            created_at=rows[0]["created_at"],
            expires_at=rows[0]["expires_at"],
            complete=complete,
            all_ok=complete and all(item.state == "OK" for item in items),
            items=items,
        )

    # ------------------------------------------------------------------
    # approval
    # ------------------------------------------------------------------

    def approve(
        self, context: WorkspaceContext, campaign_id: UUID, payload: ApproveIn
    ) -> PersonalizationStateOut:
        self._campaign(context, campaign_id, require_draft=True, require_enabled=True)
        digest, _, steps = self.current_digest(context, campaign_id)
        if payload.config_digest != digest:
            raise AppError(
                "approval_stale",
                "The objective or emails changed after these samples were generated. "
                "Generate new samples.",
                status_code=409,
            )
        rows = self.repo.list_batch(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            batch_id=payload.batch_id,
        )
        if not rows:
            raise AppError("not_found", "Preview batch not found", status_code=404)
        if any(row["config_digest"] != digest for row in rows):
            raise AppError(
                "approval_stale",
                "These samples were generated from earlier content. "
                "Generate new samples.",
                status_code=409,
            )
        if any(row["state"] == "PENDING" for row in rows):
            raise AppError(
                "previews_incomplete",
                "Samples are still being generated.",
                status_code=409,
            )
        if any(row["state"] != "OK" for row in rows):
            raise AppError(
                "previews_failed",
                "Some samples could not be generated. Fix the problem and "
                "generate again.",
                status_code=409,
            )
        email_step_count = len(_draft_email_steps(steps))
        per_member: dict[str, int] = {}
        for row in rows:
            key = str(row["audience_member_id"])
            per_member[key] = per_member.get(key, 0) + 1
        covered = sum(1 for n in per_member.values() if n >= email_step_count)
        if covered < 1:
            raise AppError(
                "previews_insufficient",
                "Review a sample that covers every email in the sequence.",
                status_code=409,
            )

        self.repo.insert_approval(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            config_digest=digest,
            batch_id=payload.batch_id,
            approved_by=context.user_id,
        )
        self.campaigns.record_audit_event(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            action="campaign.personalization_approved",
            target_id=campaign_id,
            after_state={"config_digest": digest},
        )
        return self.get_state(context, campaign_id)

    # ------------------------------------------------------------------
    # progress
    # ------------------------------------------------------------------

    def progress(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> GenerationProgressOut:
        self._campaign(context, campaign_id)
        counts = self.repo.progress(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        usage = self.repo.usage_today(
            workspace_id=context.workspace_id, day=datetime.now(UTC).date()
        )
        s = self.settings
        return GenerationProgressOut(
            campaign_id=campaign_id,
            budget=BudgetOut(
                generation_used=usage.get("GENERATION", 0),
                generation_cap=s.personalization_daily_generation_cap,
                preview_used=usage.get("PREVIEW", 0),
                preview_cap=s.personalization_daily_preview_cap,
                fetch_used=usage.get("FETCH", 0),
                fetch_cap=s.personalization_daily_fetch_cap,
            ),
            **counts,
        )


def _var(row: RowMapping, name: str) -> str | None:
    variables = row["frozen_variables"] or {}
    value = variables.get(name)
    return str(value) if value not in (None, "") else None
