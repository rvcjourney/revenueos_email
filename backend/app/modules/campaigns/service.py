from __future__ import annotations

import base64
import json
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.config import Settings
from app.core.errors import AppError
from app.core.permissions import has_permission
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import (
    CampaignCountsOut,
    CampaignCreateIn,
    CampaignDetailOut,
    CampaignDuplicateIn,
    CampaignListItem,
    CampaignPage,
    CampaignUpdateIn,
)
from app.modules.leads.pagination import encode_cursor, normalize_limit
from app.modules.personalization.version import (
    CAMPAIGN_TYPE_HYPER,
    CAMPAIGN_TYPE_STANDARD,
)

_ALLOWED_STATUS_FILTERS = {
    "DRAFT",
    "SCHEDULED",
    "RUNNING",
    "PAUSED",
    "ERROR",
    "COMPLETED",
    "ARCHIVED",
}


_SORT_KEY_COLUMNS = {
    "updated_desc": "updated_at",
    "created_desc": "created_at",
    "name_asc": "name",
}


def _encode_sorted_cursor(sort: str, row: RowMapping) -> str:
    value = row[_SORT_KEY_COLUMNS[sort]]
    key = value.lower() if sort == "name_asc" else value.isoformat()
    payload = json.dumps(
        {"id": str(row["id"]), "s": sort, "k": key}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_list_cursor(
    cursor: str | None, sort: str | None
) -> tuple[str | None, UUID | None]:
    """(sort key, id) to continue after. A cursor only continues the ordering
    that issued it; any other one is rejected like a malformed cursor, rather
    than quietly resuming somewhere unrelated."""
    if not cursor:
        return None, None
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if data.get("s") != sort:
            raise ValueError("cursor belongs to another ordering")
        if sort is not None and not isinstance(data["k"], str):
            raise ValueError("cursor has no sort key")
        return data.get("k"), UUID(str(data["id"]))
    except Exception as exc:
        raise AppError(
            "validation_error", "Cursor is invalid", status_code=422
        ) from exc


def _require_personalization_enabled() -> None:
    """Hyper-personalized campaigns exist only where the operator enabled the
    feature (ADR-0011/0012): fail closed instead of creating a campaign that can
    never be activated."""
    if not Settings.current().personalization_enabled:
        raise AppError(
            "personalization_disabled",
            "Hyper-personalized campaigns are not enabled for this deployment",
            status_code=409,
        )


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise AppError("validation_error", "Campaign name is required", status_code=422)
    if len(cleaned) > 200:
        raise AppError(
            "validation_error",
            "Campaign name must be 200 characters or fewer",
            status_code=422,
        )
    return cleaned


class CampaignService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)

    def create_campaign(
        self, context: WorkspaceContext, payload: CampaignCreateIn
    ) -> CampaignDetailOut:
        clean_name = _validate_name(payload.name)
        if payload.campaign_type == CAMPAIGN_TYPE_HYPER:
            _require_personalization_enabled()
        row = self.repo.create_campaign(
            workspace_id=context.workspace_id,
            name=clean_name,
            description=payload.description,
            creator_id=context.user_id,
            campaign_type=payload.campaign_type,
        )
        return self._to_detail_out(row)

    def get_campaign(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> CampaignDetailOut:
        row = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if row is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        return self._to_detail_out(row)

    def list_campaigns(
        self,
        context: WorkspaceContext,
        *,
        limit: int,
        cursor: str | None,
        query: str | None,
        status: str | None,
        campaign_type: str | None = None,
        sort: str | None = None,
    ) -> CampaignPage:
        effective_limit = normalize_limit(limit)
        after_key, after_id = _decode_list_cursor(cursor, sort)

        clean_status = status if status in _ALLOWED_STATUS_FILTERS else None
        clean_query = query.strip() if query else None

        # Archived campaigns leave the everyday list; they stay reachable by
        # filtering on ARCHIVED, or with the explicit ALL value.
        rows = self.repo.list_campaigns(
            workspace_id=context.workspace_id,
            limit=effective_limit + 1,
            after_id=after_id,
            status=clean_status,
            query=clean_query,
            include_archived=status == "ALL",
            campaign_type=campaign_type,
            sort=sort,
            after_key=after_key,
        )
        has_more = len(rows) > effective_limit
        page_rows = rows[:effective_limit]

        names = self.repo.member_display_names() if page_rows else {}
        items = [
            self._to_list_item(row, creator_name=names.get(str(row["creator_id"])))
            for row in page_rows
        ]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = (
                encode_cursor(last["id"])
                if sort is None
                else _encode_sorted_cursor(sort, last)
            )
        return CampaignPage(items=items, next_cursor=next_cursor)

    def count_campaigns(
        self,
        context: WorkspaceContext,
        *,
        query: str | None,
        campaign_type: str | None = None,
    ) -> CampaignCountsOut:
        by_status = self.repo.count_campaigns_by_status(
            workspace_id=context.workspace_id,
            query=query.strip() if query else None,
            campaign_type=campaign_type,
        )
        total = sum(by_status.values())
        return CampaignCountsOut(
            by_status=by_status,
            not_archived=total - by_status.get("ARCHIVED", 0),
            total=total,
        )

    def update_campaign(
        self, context: WorkspaceContext, campaign_id: UUID, payload: CampaignUpdateIn
    ) -> CampaignDetailOut:
        clean_name = _validate_name(payload.name) if payload.name is not None else None
        row = self.repo.update_campaign(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            expected_version=payload.expected_version,
            name=clean_name,
            description=payload.description,
            description_provided="description" in payload.model_fields_set,
            actor_id=context.user_id,
            may_edit_paused=has_permission(context.role_code, "campaigns.execute"),
        )
        if row is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        return self._to_detail_out(row)

    def archive_campaign(
        self, context: WorkspaceContext, campaign_id: UUID, expected_version: int
    ) -> CampaignDetailOut:
        row = self.repo.archive_campaign(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            expected_version=expected_version,
            actor_id=context.user_id,
            may_archive_activated=has_permission(
                context.role_code, "campaigns.execute"
            ),
        )
        if row is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        return self._to_detail_out(row)

    def duplicate_campaign(
        self, context: WorkspaceContext, campaign_id: UUID, payload: CampaignDuplicateIn
    ) -> CampaignDetailOut:
        """Copy metadata + sequence steps + mailbox assignments + settings.

        Audience is deliberately never copied -- CAMPAIGN_ENGINE.md requires
        re-selection/re-capture for a fresh outreach intent, and campaign_audiences
        rows cannot be reassigned to a different campaign_id.
        """
        source = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if source is None:
            raise AppError("not_found", "Campaign not found", status_code=404)

        dup_name = (
            _validate_name(payload.name)
            if payload.name
            else f"{source['name']} (Copy)"[:200]
        )
        source_type = source.get("campaign_type", CAMPAIGN_TYPE_STANDARD)
        if source_type == CAMPAIGN_TYPE_HYPER:
            _require_personalization_enabled()
        new_campaign = self.repo.create_campaign(
            workspace_id=context.workspace_id,
            name=dup_name,
            description=source["description"],
            creator_id=context.user_id,
            campaign_type=source_type,
        )
        new_campaign_id = UUID(str(new_campaign["id"]))

        source_sequence = self.repo.get_sequence(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if source_sequence is not None:
            new_sequence = self.repo.create_sequence(
                workspace_id=context.workspace_id, campaign_id=new_campaign_id
            )
            if source_sequence.get("personalization_config"):
                # The objective is copied; sample approvals never are -- the copy
                # must be previewed and approved on its own (ADR-0011).
                self.repo.set_sequence_personalization_config(
                    workspace_id=context.workspace_id,
                    sequence_id=UUID(str(new_sequence["id"])),
                    config=dict(source_sequence["personalization_config"]),
                )
            source_steps = self.repo.list_steps_ordered(
                workspace_id=context.workspace_id,
                sequence_id=UUID(str(source_sequence["id"])),
            )
            for step in source_steps:
                new_step = self.repo.insert_step(
                    workspace_id=context.workspace_id,
                    sequence_id=UUID(str(new_sequence["id"])),
                    campaign_id=new_campaign_id,
                    position=step["position"],
                    kind=step["kind"],
                    email_subject=step["email_subject"],
                    email_body_html=step["email_body_html"],
                    email_preheader=step["email_preheader"],
                    email_same_thread=bool(step.get("email_same_thread")),
                    email_variable_schema=step["email_variable_schema"],
                    wait_duration_minutes=step["wait_duration_minutes"],
                    source_template_version_id=(
                        UUID(str(step["source_template_version_id"]))
                        if step["source_template_version_id"]
                        else None
                    ),
                )
                if step["kind"] == "EMAIL":
                    # Images/attachments come along (shared storage objects, same
                    # content ids, so the copied body still resolves them).
                    self.repo.copy_step_attachments(
                        workspace_id=context.workspace_id,
                        from_step_id=UUID(str(step["id"])),
                        to_campaign_id=new_campaign_id,
                        to_sequence_id=UUID(str(new_sequence["id"])),
                        to_step_id=UUID(str(new_step["id"])),
                    )

        source_mailboxes = self.repo.list_campaign_mailboxes(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        for row in source_mailboxes:
            self.repo.insert_campaign_mailbox(
                workspace_id=context.workspace_id,
                campaign_id=new_campaign_id,
                mailbox_id=UUID(str(row["mailbox_id"])),
                allocation_position=row["allocation_position"],
            )

        settings_versions = self.repo.list_settings_versions(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if settings_versions:
            latest = settings_versions[0]
            self.repo.create_settings_version(
                workspace_id=context.workspace_id,
                campaign_id=new_campaign_id,
                timezone=latest["timezone"],
                weekday_set=latest["weekday_set"],
                window_start_local=latest["window_start_local"],
                window_end_local=latest["window_end_local"],
                daily_limit=latest["daily_limit"],
            )

        return self.get_campaign(context, new_campaign_id)

    def _to_list_item(
        self, row: RowMapping, *, creator_name: str | None = None
    ) -> CampaignListItem:
        return CampaignListItem(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            campaign_type=row.get("campaign_type", CAMPAIGN_TYPE_STANDARD),
            draft_sequence_id=row["draft_sequence_id"],
            draft_audience_id=row["draft_audience_id"],
            current_settings_id=row["current_settings_id"],
            is_activated=row.get("activation_id") is not None,
            erased_at=row.get("erased_at"),
            paused_at=row.get("paused_at"),
            start_at=row.get("start_at"),
            creator_name=creator_name,
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _to_detail_out(self, row: RowMapping) -> CampaignDetailOut:
        return CampaignDetailOut(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            description=row["description"],
            creator_id=row["creator_id"],
            status=row["status"],
            campaign_type=row.get("campaign_type", CAMPAIGN_TYPE_STANDARD),
            start_at=row["start_at"],
            draft_sequence_id=row["draft_sequence_id"],
            draft_audience_id=row["draft_audience_id"],
            current_settings_id=row["current_settings_id"],
            planning_status=row["planning_status"],
            archived_at=row["archived_at"],
            error_reason=row["error_reason"],
            is_activated=row.get("activation_id") is not None,
            erased_at=row.get("erased_at"),
            paused_at=row.get("paused_at"),
            paused_total_seconds=int(row.get("paused_total_seconds") or 0),
            last_resumed_at=row.get("last_resumed_at"),
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
