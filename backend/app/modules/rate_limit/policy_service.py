from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.audit import record_user_audit
from app.core.config import Settings
from app.core.errors import AppError
from app.modules.rate_limit.schemas import RatePolicySpec, RateScopeKind

# The unit the send path reserves capacity in (sending/service.py CAPACITY_UNIT;
# a test keeps the two equal).
MAILBOX_LIMIT_UNIT = "MESSAGE"
# A mailbox's volume limit is one MAILBOX-kind row over a rolling 24 hours.
DAILY_WINDOW_SECONDS = 86_400
MIN_DAILY_CAP = 1
MAX_DAILY_CAP = 500
MAX_SPACING_SECONDS = 3_600
# A MAILBOX policy only counts as a volume cap when its window is at least this
# long. A short window (say 1 message per 10 s) paces a mailbox but would still
# allow thousands of emails a day.
MIN_CAP_WINDOW_SECONDS = 3_600


def has_mailbox_volume_cap(policies: list[RatePolicySpec]) -> bool:
    """Whether the policies resolved for a send include a real per-mailbox cap.

    The send path refuses to send without one: a mailbox must never be
    unlimited because a row is missing.
    """
    return any(
        p.kind == RateScopeKind.MAILBOX and p.window_seconds >= MIN_CAP_WINDOW_SECONDS
        for p in policies
    )


_UPSERT_LIMIT = text(
    """
    INSERT INTO public.tenant_rate_policies
        (workspace_id, mailbox_id, kind, unit, window_seconds, limit_value,
         window_kind, min_spacing_seconds)
    VALUES
        (:workspace_id, :mailbox_id, 'MAILBOX', :unit, :window_seconds,
         :limit_value, 'ROLLING', :min_spacing_seconds)
    ON CONFLICT (workspace_id, mailbox_id, unit, window_seconds, window_kind)
        WHERE kind = 'MAILBOX'
    DO UPDATE SET limit_value = EXCLUDED.limit_value,
                  min_spacing_seconds = EXCLUDED.min_spacing_seconds
    RETURNING limit_value, min_spacing_seconds
    """
)

_INSERT_DEFAULT_LIMIT = text(
    """
    INSERT INTO public.tenant_rate_policies
        (workspace_id, mailbox_id, kind, unit, window_seconds, limit_value,
         window_kind, min_spacing_seconds)
    VALUES
        (:workspace_id, :mailbox_id, 'MAILBOX', :unit, :window_seconds,
         :limit_value, 'ROLLING', :min_spacing_seconds)
    ON CONFLICT DO NOTHING
    """
)


def insert_default_mailbox_limit(
    session: Session,
    *,
    workspace_id: UUID,
    mailbox_id: UUID,
    settings: Settings | None = None,
) -> None:
    """Give a mailbox its default limit unless it already has one (idempotent).

    Called in the same transaction that creates the mailbox, by the role that
    creates it, so a mailbox never exists without a limit.
    """
    settings = settings or Settings.current()
    session.execute(
        _INSERT_DEFAULT_LIMIT,
        {
            "workspace_id": str(workspace_id),
            "mailbox_id": str(mailbox_id),
            "unit": MAILBOX_LIMIT_UNIT,
            "window_seconds": DAILY_WINDOW_SECONDS,
            "limit_value": settings.mailbox_default_daily_cap,
            "min_spacing_seconds": settings.mailbox_default_min_spacing_seconds,
        },
    )


class MailboxLimitsIn(BaseModel):
    daily_cap: int = Field(ge=MIN_DAILY_CAP, le=MAX_DAILY_CAP)
    min_spacing_seconds: int = Field(ge=0, le=MAX_SPACING_SECONDS)


class MailboxLimitsOut(BaseModel):
    # None when the mailbox has no limit row: it cannot send until one is set.
    daily_cap: int | None
    min_spacing_seconds: int | None
    configured: bool
    default_daily_cap: int
    default_min_spacing_seconds: int
    max_daily_cap: int = MAX_DAILY_CAP
    max_spacing_seconds: int = MAX_SPACING_SECONDS


class MailboxLimitsService:
    """Reads and changes one mailbox's sending limit, as the API role.

    Row-level security does the authorizing (MANAGER and above, this
    workspace's MAILBOX-kind rows); this only validates and reads back.
    """

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or Settings.current()

    def _require_mailbox(self, workspace_id: UUID, mailbox_id: UUID) -> None:
        row = self.session.execute(
            text(
                "SELECT 1 FROM public.mailboxes "
                "WHERE workspace_id = :workspace_id AND id = :mailbox_id"
            ),
            {"workspace_id": str(workspace_id), "mailbox_id": str(mailbox_id)},
        ).first()
        if row is None:
            raise AppError("not_found", "Mailbox not found", status_code=404)

    def _out(self, row: Any | None) -> MailboxLimitsOut:
        return MailboxLimitsOut(
            daily_cap=row["limit_value"] if row else None,
            min_spacing_seconds=row["min_spacing_seconds"] if row else None,
            configured=row is not None,
            default_daily_cap=self.settings.mailbox_default_daily_cap,
            default_min_spacing_seconds=self.settings.mailbox_default_min_spacing_seconds,
        )

    def get(self, context: WorkspaceContext, mailbox_id: UUID) -> MailboxLimitsOut:
        self._require_mailbox(context.workspace_id, mailbox_id)
        row = (
            self.session.execute(
                text(
                    """
                    SELECT limit_value, min_spacing_seconds
                    FROM public.tenant_rate_policies
                    WHERE workspace_id = :workspace_id AND mailbox_id = :mailbox_id
                      AND kind = 'MAILBOX' AND unit = :unit
                      AND window_seconds = :window_seconds AND window_kind = 'ROLLING'
                    """
                ),
                {
                    "workspace_id": str(context.workspace_id),
                    "mailbox_id": str(mailbox_id),
                    "unit": MAILBOX_LIMIT_UNIT,
                    "window_seconds": DAILY_WINDOW_SECONDS,
                },
            )
            .mappings()
            .first()
        )
        return self._out(row)

    def set(
        self, context: WorkspaceContext, mailbox_id: UUID, payload: MailboxLimitsIn
    ) -> MailboxLimitsOut:
        self._require_mailbox(context.workspace_id, mailbox_id)
        row = (
            self.session.execute(
                _UPSERT_LIMIT,
                {
                    "workspace_id": str(context.workspace_id),
                    "mailbox_id": str(mailbox_id),
                    "unit": MAILBOX_LIMIT_UNIT,
                    "window_seconds": DAILY_WINDOW_SECONDS,
                    "limit_value": payload.daily_cap,
                    "min_spacing_seconds": payload.min_spacing_seconds,
                },
            )
            .mappings()
            .first()
        )
        record_user_audit(
            self.session,
            context,
            action="mailbox.limits_updated",
            target_type="mailbox",
            target_id=mailbox_id,
            after_state={
                "daily_cap": payload.daily_cap,
                "min_spacing_seconds": payload.min_spacing_seconds,
            },
        )
        return self._out(row)
