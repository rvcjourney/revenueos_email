from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import RowMapping
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.core.permissions import has_permission
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import CampaignSettingsCreateIn, CampaignSettingsOut

# Platform-wide safety ceiling for a single campaign's daily send volume.
# Campaign settings may never request more than this regardless of what the
# user configures; individual mailbox/workspace/provider caps are enforced
# separately by the rate-limiting layer, not here.
_PLATFORM_MAX_DAILY_LIMIT = 100_000


def _weekdays_to_bitmask(weekdays: list[int]) -> int:
    # Mon = bit 0 .. Sun = bit 6 (ISO weekday order).
    mask = 0
    for day in weekdays:
        mask |= 1 << (day - 1)
    return mask


def _bitmask_to_weekdays(weekday_set: int) -> list[int]:
    return [day for day in range(1, 8) if weekday_set & (1 << (day - 1))]


def _validate_timezone(tz_name: str) -> str:
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AppError(
            "validation_error",
            f"'{tz_name}' is not a valid IANA timezone name",
            status_code=422,
        ) from exc
    return tz_name


class CampaignSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)

    def _require_editable_campaign(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> RowMapping:
        """DRAFT, or an activated PAUSED campaign (ADR-0020). The database allows
        a new settings version in exactly those two cases (app_guard_campaign_config
        and the campaign_settings_versions insert policy), which for PAUSED also
        requires campaigns.execute -- checked here so the answer is a clear 403
        instead of a row-level-security error."""
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if campaign["status"] == "PAUSED" and campaign["activation_id"] is not None:
            if not has_permission(context.role_code, "campaigns.execute"):
                raise AppError(
                    "forbidden",
                    "Only a manager can change the schedule of a paused campaign",
                    status_code=403,
                )
            return campaign
        if campaign["status"] != "DRAFT":
            raise AppError(
                "state_conflict",
                "Schedule/settings can only change while the campaign is DRAFT "
                "or paused",
                status_code=409,
            )
        return campaign

    def get_current(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> CampaignSettingsOut | None:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        if campaign["current_settings_id"] is None:
            return None
        row = self.repo.get_settings_version(
            workspace_id=context.workspace_id,
            settings_id=UUID(str(campaign["current_settings_id"])),
        )
        return self._to_out(row) if row is not None else None

    def list_history(
        self, context: WorkspaceContext, campaign_id: UUID
    ) -> Sequence[CampaignSettingsOut]:
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)
        rows = self.repo.list_settings_versions(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        return [self._to_out(row) for row in rows]

    def create_settings_version(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        payload: CampaignSettingsCreateIn,
    ) -> CampaignSettingsOut:
        campaign = self._require_editable_campaign(context, campaign_id)
        clean_tz = _validate_timezone(payload.timezone)
        if (
            payload.daily_limit is not None
            and payload.daily_limit > _PLATFORM_MAX_DAILY_LIMIT
        ):
            raise AppError(
                "validation_error",
                f"daily_limit must not exceed the platform maximum of "
                f"{_PLATFORM_MAX_DAILY_LIMIT}",
                status_code=422,
            )
        row = self.repo.create_settings_version(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            timezone=clean_tz,
            weekday_set=_weekdays_to_bitmask(payload.weekdays),
            window_start_local=payload.window_start_local,
            window_end_local=payload.window_end_local,
            daily_limit=payload.daily_limit,
        )
        if campaign["status"] == "PAUSED":
            # The change applies on resume: the reconcile pass re-projects every
            # unsent message into the new windows. Ids only, never content.
            self.repo.record_campaign_audit(
                workspace_id=context.workspace_id,
                actor_id=context.user_id,
                action="campaign.settings_changed_while_paused",
                campaign_id=campaign_id,
                after_state={"settings_revision": int(row["revision"])},
            )
        return self._to_out(row)

    def _to_out(self, row: RowMapping) -> CampaignSettingsOut:
        return CampaignSettingsOut(
            id=row["id"],
            campaign_id=row["campaign_id"],
            revision=row["revision"],
            timezone=row["timezone"],
            weekdays=_bitmask_to_weekdays(row["weekday_set"]),
            window_start_local=row["window_start_local"],
            window_end_local=row["window_end_local"],
            daily_limit=row["daily_limit"],
            created_at=row["created_at"],
        )
