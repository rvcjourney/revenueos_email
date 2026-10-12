from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.core.permissions import has_permission
from app.modules.campaigns.attachment_service import to_attachment_out
from app.modules.campaigns.attachments import extract_cid_references
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import (
    PreviewRecipientOut,
    PreviewRecipientsOut,
    SequenceOut,
    SequenceStepCreateIn,
    SequenceStepOut,
    SequenceStepsReorderIn,
    SequenceStepUpdateIn,
)
from app.modules.templates.sanitizer import (
    BRANDED_PROFILE,
    DEFAULT_PROFILE,
    sanitize_email_html,
)
from app.modules.templates.variables import validate_template_content

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# Wait inserted by "duplicate step" when the source step has no following wait.
_DEFAULT_DUPLICATE_WAIT_MINUTES = 2 * 24 * 60


def _normalize_preheader(preheader: str | None) -> str | None:
    """Trim; empty means "no pre-header". Control characters are rejected here so
    the user gets a 422 instead of a CHECK-constraint failure."""
    if preheader is None:
        return None
    cleaned = preheader.strip()
    if not cleaned:
        return None
    if _CONTROL_CHARS_RE.search(cleaned):
        raise AppError(
            "validation_error",
            "Pre-header must not contain control characters",
            status_code=422,
        )
    return cleaned


def describe_sequence_problem(steps: Any) -> str | None:
    """Why a sequence cannot run, or None. The rules preflight applies at
    activation that edits can break: it starts and ends with an email, emails
    and waits alternate, every email has a subject."""
    ordered = sorted(steps, key=lambda step: step["position"])
    if not ordered:
        return "A campaign needs at least one email"
    if [step["position"] for step in ordered] != list(range(1, len(ordered) + 1)):
        return "Step positions are not contiguous"
    for index, step in enumerate(ordered):
        expected = "EMAIL" if index % 2 == 0 else "WAIT"
        if step["kind"] != expected:
            return "Emails and waits must alternate, starting and ending with an email"
        if step["kind"] == "EMAIL" and not (step["email_subject"] or "").strip():
            return "Every email needs a subject"
    if ordered[-1]["kind"] != "EMAIL":
        return "Emails and waits must alternate, starting and ending with an email"
    return None


class SequenceService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)

    def _require_editable_campaign(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> tuple[RowMapping, bool]:
        """(campaign, paused_edit). DRAFT is edited as always. An activated,
        PAUSED campaign may be edited by campaigns.execute holders (ADR-0020); every
        other state is frozen. A personalized campaign allows far less: callers
        reject structure changes with `_refuse_hyper_structure`, and a step may only
        be edited while none of its emails has been written."""
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if campaign["status"] == "DRAFT":
            return campaign, False
        if campaign["status"] == "PAUSED" and campaign["activation_id"] is not None:
            if not has_permission(context.role_code, "campaigns.execute"):
                raise AppError(
                    "forbidden",
                    "Only a manager can edit the emails of a paused campaign",
                    status_code=403,
                )
            return campaign, True
        raise AppError(
            "state_conflict",
            "Sequence can only be edited while the campaign is DRAFT or paused",
            status_code=409,
        )

    @staticmethod
    def _is_hyper(campaign: RowMapping) -> bool:
        return campaign.get("campaign_type") == "HYPER_PERSONALIZED"

    def _refuse_hyper_structure(self, campaign: RowMapping, paused: bool) -> None:
        """A personalized campaign writes every email from its step's reference just
        before sending, so adding, removing, reordering or re-timing steps would
        change who gets what. Only the wording of an unwritten reference may change."""
        if paused and self._is_hyper(campaign):
            raise self._locked(
                "Steps of a personalized campaign can't be added, removed, "
                "reordered or re-timed once it has started. You can still improve "
                "the wording of an email that hasn't been written yet."
            )

    def _sequence_frontier(self, context: WorkspaceContext, sequence_id: UUID) -> int:
        return self.repo.get_sequence_frontier(
            workspace_id=context.workspace_id, sequence_id=sequence_id
        )

    @staticmethod
    def _locked(message: str) -> AppError:
        return AppError("step_locked", message, status_code=409)

    def _finish_paused_edit(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        sequence_id: UUID,
        *,
        operation: str,
        step_id: UUID | None = None,
        fields: list[str] | None = None,
    ) -> None:
        """After an edit to a frozen sequence: prove the result is still a valid
        Email/Wait sequence (raising rolls the whole request back), then audit.
        Ids and field names only -- never content."""
        steps = self.repo.list_steps_ordered(
            workspace_id=context.workspace_id, sequence_id=sequence_id
        )
        problem = describe_sequence_problem(steps)
        if problem is not None:
            raise AppError("sequence_invalid", problem, status_code=422)
        self.repo.record_campaign_audit(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            action="campaign.sequence_edited_while_paused",
            campaign_id=campaign_id,
            after_state={
                "operation": operation,
                "step_id": str(step_id) if step_id else None,
                "fields": fields or [],
            },
        )

    @staticmethod
    def _sanitizer_profile(
        campaign: RowMapping, sequence: RowMapping | None
    ) -> str:
        """The branded sanitizer profile (ADR-0017) applies only to a
        hyper-personalized campaign whose saved configuration chose HTML. It is
        decided here, from stored state -- never from the request -- so a client
        cannot opt in."""
        if campaign.get("campaign_type") != "HYPER_PERSONALIZED" or sequence is None:
            return DEFAULT_PROFILE
        config = sequence.get("personalization_config")
        if isinstance(config, dict) and config.get("email_format") == "HTML":
            return BRANDED_PROFILE
        return DEFAULT_PROFILE

    def _ensure_sequence(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> RowMapping:
        existing = self.repo.get_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if existing is not None:
            return existing
        return self.repo.create_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )

    def _get_template_version(
        self, context: WorkspaceContext, template_version_id: UUID
    ) -> RowMapping | None:
        return (
            self.session.execute(
                text(
                    """
                    SELECT id, subject, body_html, preheader, variable_schema
                    FROM template_versions
                    WHERE workspace_id = :workspace_id AND id = :template_version_id
                    """
                ),
                {
                    "workspace_id": str(context.workspace_id),
                    "template_version_id": str(template_version_id),
                },
            )
            .mappings()
            .first()
        )

    def _resolve_email_content(
        self,
        context: WorkspaceContext,
        *,
        source_template_version_id: UUID | None,
        email_subject: str | None,
        email_body_html: str | None,
        email_preheader: str | None = None,
        sanitize_body: bool = True,
        profile: str = DEFAULT_PROFILE,
    ) -> tuple[str, str, str | None, dict[str, Any]]:
        """Return (subject, body, preheader, variable_schema) for an EMAIL step.

        A template version supplies defaults only: any explicitly provided field
        wins, so edits made after picking a template are never silently replaced
        by the template's text. The template id stays as provenance. Content
        entering a step is always sanitized (allow-list) before it is stored.
        For the pre-header, None means "use the template's / none" and an empty
        string clears it.
        """
        subject = email_subject
        body = email_body_html
        preheader = email_preheader
        if source_template_version_id is not None:
            version = self._get_template_version(context, source_template_version_id)
            if version is None:
                raise AppError(
                    "validation_error",
                    "Template version not found in this workspace",
                    status_code=422,
                )
            if subject is None:
                subject = version["subject"]
            if body is None:
                body = version["body_html"]
            if preheader is None:
                preheader = version["preheader"]

        if not subject or not subject.strip():
            raise AppError(
                "validation_error",
                "email_subject is required for an EMAIL step without a "
                "template reference",
                status_code=422,
            )
        clean_body = (
            sanitize_email_html(body or "", profile=profile)
            if sanitize_body
            else (body or "")
        )
        clean_preheader = _normalize_preheader(preheader)
        variable_schema = validate_template_content(
            subject, clean_body, clean_preheader
        )
        return subject.strip(), clean_body, clean_preheader, variable_schema

    def get_sequence(self, context: WorkspaceContext, campaign_id: UUID) -> SequenceOut:
        campaign = self._require_campaign(context, campaign_id)
        sequence = self.repo.get_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if sequence is None:
            return SequenceOut(
                id=None,
                campaign_id=campaign_id,
                revision=0,
                status="EMPTY",
                steps=[],
            )
        steps = self.repo.list_steps_ordered(
            workspace_id=context.workspace_id, sequence_id=UUID(str(sequence["id"]))
        )
        by_step: dict[str, list[Any]] = {}
        for attachment in self.repo.list_sequence_attachments(
            workspace_id=context.workspace_id, sequence_id=UUID(str(sequence["id"]))
        ):
            by_step.setdefault(str(attachment["step_id"]), []).append(attachment)
        activated = campaign["activation_id"] is not None
        # Only the paused editor shows sent/unsent counts and the frontier, and both
        # walk the campaign's messages -- do not pay that on every page load of a
        # running campaign that may hold hundreds of thousands of them.
        paused = activated and campaign["status"] == "PAUSED"
        counts = (
            self.repo.count_step_messages(
                workspace_id=context.workspace_id, campaign_id=campaign_id
            )
            if paused
            else {}
        )
        return SequenceOut(
            id=sequence["id"],
            campaign_id=sequence["campaign_id"],
            revision=sequence["revision"],
            status=sequence["status"],
            steps=[
                self._to_step_out(
                    row, by_step.get(str(row["id"]), []), counts.get(str(row["id"]))
                )
                for row in steps
            ],
            paused_edit_allowed=(
                campaign["status"] == "PAUSED"
                and activated
                and has_permission(context.role_code, "campaigns.execute")
            ),
            structure_locked_through=(
                self._sequence_frontier(context, UUID(str(sequence["id"])))
                if paused
                else 0
            ),
        )

    def list_preview_recipients(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        *,
        limit: int,
        after_ordinal: int | None,
    ) -> PreviewRecipientsOut:
        """Prospects the sequence preview can render against: the campaign's
        activated audience, else its committed/latest READY one. Each item
        carries the frozen variables the real message renders with."""
        campaign = self._require_campaign(context, campaign_id)
        audience_id = campaign["activated_audience_id"] or campaign["draft_audience_id"]
        audience = None
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
        if audience is None or audience["status"] != "READY":
            return PreviewRecipientsOut(source="NONE", items=[], total=0)

        audience_uuid = UUID(str(audience["id"]))
        rows = self.repo.list_accepted_audience_members(
            workspace_id=context.workspace_id,
            audience_id=audience_uuid,
            after_ordinal=after_ordinal,
            limit=limit + 1,
        )
        page = rows[:limit]
        items = []
        for row in page:
            variables = dict(row["frozen_variables"] or {})
            items.append(
                PreviewRecipientOut(
                    audience_member_id=row["id"],
                    lead_id=row["lead_id"],
                    email=variables.get("email"),
                    first_name=variables.get("first_name"),
                    last_name=variables.get("last_name"),
                    company=variables.get("company"),
                    variables=variables,
                )
            )
        counts = self.repo.get_audience_member_counts(
            workspace_id=context.workspace_id, audience_id=audience_uuid
        )
        return PreviewRecipientsOut(
            source="AUDIENCE",
            items=items,
            total=counts["accepted"],
            next_cursor=(
                int(page[-1]["capture_ordinal"]) if len(rows) > limit and page else None
            ),
        )

    def _require_campaign(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> RowMapping:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        return campaign

    def add_step(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: SequenceStepCreateIn,
    ) -> SequenceStepOut:
        campaign, paused = self._require_editable_campaign(context, campaign_id)
        self._refuse_hyper_structure(campaign, paused)
        sequence = self._ensure_sequence(context, campaign_id)
        if paused:
            existing_steps = self.repo.list_steps_ordered(
                workspace_id=context.workspace_id,
                sequence_id=UUID(str(sequence["id"])),
            )
            target = max(1, min(payload.position, len(existing_steps) + 1))
            if target <= self._sequence_frontier(context, UUID(str(sequence["id"]))):
                raise self._locked(
                    "Leads have already reached that part of the sequence. Add "
                    "the new email after the last step they have received."
                )

        if payload.kind == "EMAIL":
            if payload.wait_duration_minutes is not None:
                raise AppError(
                    "validation_error",
                    "wait_duration_minutes must not be set for an EMAIL step",
                    status_code=422,
                )
            subject, body, preheader, variable_schema = self._resolve_email_content(
                context,
                source_template_version_id=payload.source_template_version_id,
                email_subject=payload.email_subject,
                email_body_html=payload.email_body_html,
                email_preheader=payload.email_preheader,
                profile=self._sanitizer_profile(campaign, sequence),
            )
            # A brand-new step has no uploaded images yet.
            self._validate_cid_references(context, None, body)
            position = payload.position
            if payload.leading_wait_minutes is not None:
                # Same transaction as the email insert below: a failure rolls
                # both back, so the sequence never gains a dangling WAIT.
                wait_row = self.repo.insert_step(
                    workspace_id=context.workspace_id,
                    sequence_id=UUID(str(sequence["id"])),
                    campaign_id=campaign_id,
                    position=position,
                    kind="WAIT",
                    email_subject=None,
                    email_body_html=None,
                    email_variable_schema=None,
                    wait_duration_minutes=payload.leading_wait_minutes,
                    source_template_version_id=None,
                )
                position = wait_row["position"] + 1
            row = self.repo.insert_step(
                workspace_id=context.workspace_id,
                sequence_id=UUID(str(sequence["id"])),
                campaign_id=campaign_id,
                position=position,
                kind="EMAIL",
                email_subject=subject,
                email_body_html=body,
                email_preheader=preheader,
                email_variable_schema=variable_schema,
                wait_duration_minutes=None,
                source_template_version_id=payload.source_template_version_id,
            )
        else:
            if payload.leading_wait_minutes is not None:
                raise AppError(
                    "validation_error",
                    "leading_wait_minutes is only valid for an EMAIL step",
                    status_code=422,
                )
            if payload.wait_duration_minutes is None:
                raise AppError(
                    "validation_error",
                    "wait_duration_minutes is required for a WAIT step",
                    status_code=422,
                )
            if (
                payload.email_subject
                or payload.email_body_html
                or payload.email_preheader
            ):
                raise AppError(
                    "validation_error",
                    "A WAIT step must not include email content",
                    status_code=422,
                )
            row = self.repo.insert_step(
                workspace_id=context.workspace_id,
                sequence_id=UUID(str(sequence["id"])),
                campaign_id=campaign_id,
                position=payload.position,
                kind="WAIT",
                email_subject=None,
                email_body_html=None,
                email_variable_schema=None,
                wait_duration_minutes=payload.wait_duration_minutes,
                source_template_version_id=None,
            )
        if paused:
            self._finish_paused_edit(
                context,
                campaign_id,
                UUID(str(sequence["id"])),
                operation="add_step",
                step_id=UUID(str(row["id"])),
            )
        return self._to_step_out(row)

    def update_step(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        step_id: UUID,
        payload: SequenceStepUpdateIn,
    ) -> SequenceStepOut:
        campaign, paused = self._require_editable_campaign(context, campaign_id)
        existing = self.repo.get_step(
            workspace_id=context.workspace_id, step_id=step_id
        )
        if existing is None or str(existing["campaign_id"]) != str(campaign_id):
            raise AppError("not_found", "Sequence step not found", status_code=404)
        if paused and self._is_hyper(campaign):
            self._require_unwritten_reference(context, existing, payload)

        if existing["kind"] == "EMAIL":
            content_changing = (
                payload.email_subject is not None
                or payload.email_body_html is not None
                or payload.email_preheader is not None
                or payload.source_template_version_id is not None
            )
            if content_changing:
                if payload.source_template_version_id is not None:
                    # Picking a template: template supplies whatever the caller
                    # did not explicitly send.
                    subject_in = payload.email_subject
                    body_in = payload.email_body_html
                    preheader_in = payload.email_preheader
                else:
                    subject_in = (
                        payload.email_subject
                        if payload.email_subject is not None
                        else existing["email_subject"]
                    )
                    body_in = (
                        payload.email_body_html
                        if payload.email_body_html is not None
                        else existing["email_body_html"]
                    )
                    preheader_in = (
                        payload.email_preheader
                        if payload.email_preheader is not None
                        else existing["email_preheader"]
                    )
                profile = DEFAULT_PROFILE
                if campaign.get("campaign_type") == "HYPER_PERSONALIZED":
                    profile = self._sanitizer_profile(
                        campaign,
                        self.repo.get_sequence(
                            workspace_id=context.workspace_id,
                            campaign_id=campaign_id,
                        ),
                    )
                subject, body, preheader, variable_schema = (
                    self._resolve_email_content(
                        context,
                        source_template_version_id=payload.source_template_version_id,
                        email_subject=subject_in,
                        email_body_html=body_in,
                        email_preheader=preheader_in,
                        # A subject/pre-header-only save must not rewrite a
                        # legacy body through the allow-list.
                        sanitize_body=(
                            payload.email_body_html is not None
                            or payload.source_template_version_id is not None
                        ),
                        profile=profile,
                    )
                )
                self._validate_cid_references(context, step_id, body)
            else:
                subject, body, preheader, variable_schema = None, None, None, None
            row = self.repo.update_step(
                workspace_id=context.workspace_id,
                step_id=step_id,
                expected_version=payload.expected_version,
                email_subject=subject,
                email_body_html=body,
                email_preheader=preheader,
                email_preheader_provided=content_changing,
                email_variable_schema=variable_schema,
                email_variable_schema_provided=content_changing,
                wait_duration_minutes=None,
                source_template_version_id=payload.source_template_version_id,
                source_template_version_id_provided=(
                    payload.source_template_version_id is not None
                ),
            )
        else:
            row = self.repo.update_step(
                workspace_id=context.workspace_id,
                step_id=step_id,
                expected_version=payload.expected_version,
                email_subject=None,
                email_body_html=None,
                email_variable_schema=None,
                email_variable_schema_provided=False,
                wait_duration_minutes=payload.wait_duration_minutes,
                source_template_version_id=None,
                source_template_version_id_provided=False,
            )
        if row is None:
            raise AppError("not_found", "Sequence step not found", status_code=404)
        if paused:
            self._finish_paused_edit(
                context,
                campaign_id,
                UUID(str(existing["sequence_id"])),
                operation="update_step",
                step_id=step_id,
                fields=sorted(payload.model_fields_set - {"expected_version"}),
            )
        return self._to_step_out(
            row,
            self.repo.list_step_attachments(
                workspace_id=context.workspace_id, step_id=step_id
            ),
        )

    def delete_step(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        step_id: UUID,
        *,
        with_adjacent_wait: bool = False,
    ) -> None:
        """Delete a step. With `with_adjacent_wait`, deleting an EMAIL step also
        removes the WAIT before it (or the one after it when it is first), in the
        same transaction, so Email/Wait alternation is not left broken."""
        campaign, paused = self._require_editable_campaign(context, campaign_id)
        self._refuse_hyper_structure(campaign, paused)
        sequence = self.repo.get_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if sequence is None:
            raise AppError("not_found", "Sequence step not found", status_code=404)
        frontier = 0
        if paused:
            frontier = self._sequence_frontier(context, UUID(str(sequence["id"])))
            doomed = self.repo.get_step(
                workspace_id=context.workspace_id, step_id=step_id
            )
            if doomed is not None and doomed["position"] <= frontier:
                raise self._locked(
                    "Leads have already received this email, so it can't be "
                    "removed. Edit it instead."
                )
        if with_adjacent_wait:
            steps = self.repo.list_steps_ordered(
                workspace_id=context.workspace_id,
                sequence_id=UUID(str(sequence["id"])),
            )
            index = next(
                (i for i, s in enumerate(steps) if str(s["id"]) == str(step_id)), None
            )
            if index is not None and steps[index]["kind"] == "EMAIL":
                neighbour = None
                if index > 0 and steps[index - 1]["kind"] == "WAIT":
                    neighbour = steps[index - 1]
                elif index + 1 < len(steps) and steps[index + 1]["kind"] == "WAIT":
                    neighbour = steps[index + 1]
                if neighbour is not None:
                    if paused and neighbour["position"] <= frontier:
                        raise self._locked(
                            "The wait before this email belongs to steps leads "
                            "have already reached, so it can't be removed."
                        )
                    self.repo.delete_step(
                        workspace_id=context.workspace_id,
                        sequence_id=UUID(str(sequence["id"])),
                        step_id=UUID(str(neighbour["id"])),
                    )
        deleted = self.repo.delete_step(
            workspace_id=context.workspace_id,
            sequence_id=UUID(str(sequence["id"])),
            step_id=step_id,
        )
        if not deleted:
            raise AppError("not_found", "Sequence step not found", status_code=404)
        if paused:
            self._finish_paused_edit(
                context,
                campaign_id,
                UUID(str(sequence["id"])),
                operation="delete_step",
                step_id=step_id,
            )

    def duplicate_step(
        self, context: WorkspaceContext, campaign_id: UUID, step_id: UUID
    ) -> SequenceOut:
        """Copy an EMAIL step directly after itself.

        The copy is inserted as a WAIT + EMAIL pair so the documented strict
        Email/Wait alternation (enforced by preflight) still holds. The wait
        reuses the duration of the WAIT that follows the source, if any. Both
        inserts run in the request transaction, so they commit or roll back
        together.
        """
        campaign, paused = self._require_editable_campaign(context, campaign_id)
        self._refuse_hyper_structure(campaign, paused)
        source = self.repo.get_step(workspace_id=context.workspace_id, step_id=step_id)
        if source is None or str(source["campaign_id"]) != str(campaign_id):
            raise AppError("not_found", "Sequence step not found", status_code=404)
        if source["kind"] != "EMAIL":
            raise AppError(
                "validation_error",
                "Only an EMAIL step can be duplicated",
                status_code=422,
            )

        sequence_id = UUID(str(source["sequence_id"]))
        if paused and source["position"] < self._sequence_frontier(
            context, sequence_id
        ):
            raise self._locked(
                "Leads have already moved past this email, so a copy can't be "
                "inserted after it. Duplicate the last email they received, or "
                "a later one."
            )
        steps = self.repo.list_steps_ordered(
            workspace_id=context.workspace_id, sequence_id=sequence_id
        )
        following = next(
            (s for s in steps if s["position"] == source["position"] + 1), None
        )
        wait_minutes = (
            following["wait_duration_minutes"]
            if following is not None and following["kind"] == "WAIT"
            else _DEFAULT_DUPLICATE_WAIT_MINUTES
        )

        wait_row = self.repo.insert_step(
            workspace_id=context.workspace_id,
            sequence_id=sequence_id,
            campaign_id=campaign_id,
            position=source["position"] + 1,
            kind="WAIT",
            email_subject=None,
            email_body_html=None,
            email_variable_schema=None,
            wait_duration_minutes=wait_minutes,
            source_template_version_id=None,
        )
        copy = self.repo.insert_step(
            workspace_id=context.workspace_id,
            sequence_id=sequence_id,
            campaign_id=campaign_id,
            position=wait_row["position"] + 1,
            kind="EMAIL",
            email_subject=source["email_subject"],
            email_body_html=source["email_body_html"],
            email_preheader=source["email_preheader"],
            email_variable_schema=source["email_variable_schema"],
            wait_duration_minutes=None,
            source_template_version_id=source["source_template_version_id"],
        )
        # Same storage objects and content ids: the copied body's image
        # references keep working without rewriting it.
        self.repo.copy_step_attachments(
            workspace_id=context.workspace_id,
            from_step_id=step_id,
            to_campaign_id=campaign_id,
            to_sequence_id=sequence_id,
            to_step_id=UUID(str(copy["id"])),
        )
        if paused:
            self._finish_paused_edit(
                context,
                campaign_id,
                sequence_id,
                operation="duplicate_step",
                step_id=UUID(str(copy["id"])),
            )
        return self.get_sequence(context, campaign_id)

    def reorder_steps(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: SequenceStepsReorderIn,
    ) -> SequenceOut:
        campaign, paused = self._require_editable_campaign(context, campaign_id)
        self._refuse_hyper_structure(campaign, paused)
        sequence = self.repo.get_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if sequence is None:
            raise AppError("not_found", "Sequence not found", status_code=404)

        sequence_id = UUID(str(sequence["id"]))
        existing = self.repo.list_steps_ordered(
            workspace_id=context.workspace_id, sequence_id=sequence_id
        )
        existing_ids = {str(row["id"]) for row in existing}

        requested_ids = {str(entry.step_id) for entry in payload.steps}
        if requested_ids != existing_ids:
            raise AppError(
                "validation_error",
                "Reorder must include exactly the current set of sequence steps",
                status_code=422,
            )
        positions = sorted(entry.position for entry in payload.steps)
        if positions != list(range(1, len(existing) + 1)):
            raise AppError(
                "validation_error",
                "Positions must be a contiguous 1..N sequence with no "
                "duplicates or gaps",
                status_code=422,
            )

        if paused:
            frontier = self._sequence_frontier(context, sequence_id)
            current = {str(row["id"]): row["position"] for row in existing}
            for entry in payload.steps:
                old = current[str(entry.step_id)]
                if entry.position != old and (
                    old <= frontier or entry.position <= frontier
                ):
                    raise self._locked(
                        "Leads have already reached the first "
                        f"{frontier} steps, so those can't be moved. You can "
                        "reorder the steps after them."
                    )

        ordering = [(entry.step_id, entry.position) for entry in payload.steps]
        self.repo.reorder_steps(
            workspace_id=context.workspace_id,
            sequence_id=sequence_id,
            ordering=ordering,
        )
        if paused:
            self._finish_paused_edit(
                context, campaign_id, sequence_id, operation="reorder_steps"
            )
        return self.get_sequence(context, campaign_id)

    def _require_unwritten_reference(
        self,
        context: WorkspaceContext,
        step: RowMapping,
        payload: SequenceStepUpdateIn,
    ) -> None:
        """Paused personalized campaign: only an EMAIL step's wording, and only while
        none of its emails has been written (the database guard enforces the same)."""
        if step["kind"] != "EMAIL" or payload.wait_duration_minutes is not None:
            raise self._locked(
                "Waits of a personalized campaign can't be changed once it has started."
            )
        if self.repo.count_written_step_messages(
            workspace_id=context.workspace_id, step_id=UUID(str(step["id"]))
        ):
            raise self._locked(
                "Emails for this step have already been written, so its wording "
                "can't change. Steps whose emails haven't been written yet can "
                "still be edited."
            )
        body = payload.email_body_html
        if body is not None and extract_cid_references(body):
            raise AppError(
                "reference_inline_image_unsupported",
                "Inline images can't be used in the reference email of a "
                "personalized campaign.",
                status_code=422,
            )

    def _validate_cid_references(
        self, context: WorkspaceContext, step_id: UUID | None, body_html: str | None
    ) -> None:
        """Every inline image in the body (src="cid:...") must be an image that
        was uploaded to this step, otherwise the email would go out with a broken
        image (or, for pasted templates, a reference to another step's file)."""
        referenced = extract_cid_references(body_html)
        if not referenced:
            return
        available: set[str] = set()
        if step_id is not None:
            available = {
                r["content_id"]
                for r in self.repo.list_step_attachments(
                    workspace_id=context.workspace_id, step_id=step_id
                )
                if r["disposition"] == "INLINE"
            }
        if referenced - available:
            raise AppError(
                "validation_error",
                "An image in the email is not available on this step. Remove it "
                "or insert the image again.",
                status_code=422,
            )

    def _to_step_out(
        self,
        row: RowMapping,
        attachments: Any = (),
        counts: tuple[int, int, int, int] | None = None,
    ) -> SequenceStepOut:
        return SequenceStepOut(
            id=row["id"],
            sequence_id=row["sequence_id"],
            campaign_id=row["campaign_id"],
            position=row["position"],
            kind=row["kind"],
            email_subject=row["email_subject"],
            email_body_html=row["email_body_html"],
            email_preheader=row.get("email_preheader"),
            attachments=[to_attachment_out(a) for a in attachments],
            email_variable_schema=row["email_variable_schema"],
            wait_duration_minutes=row["wait_duration_minutes"],
            source_template_version_id=row["source_template_version_id"],
            sent_count=counts[0] if counts else 0,
            unsent_count=counts[1] if counts else 0,
            written_count=counts[2] if counts else 0,
            unwritten_count=counts[3] if counts else 0,
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
