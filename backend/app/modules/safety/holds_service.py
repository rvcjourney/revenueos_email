"""Listing and releasing a mailbox's safety holds, as the signed-in person.

A hold is placed by automatic protection (a critical bounce rate) or by reply sync
(a provider checkpoint that expired). While one is active the mailbox does not
send. Only a person with the mailboxes.manage permission can release one, and each
release is recorded in the audit log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.audit import record_user_audit
from app.core.errors import AppError
from app.modules.safety.evaluator import AUTO_BOUNCE_PREFIX

HoldKind = Literal["HIGH_BOUNCE_RATE", "RESYNC", "OTHER"]
_RESYNC_PREFIX = "resync:"


def hold_kind(source_work_identity: str) -> HoldKind:
    if source_work_identity.startswith(AUTO_BOUNCE_PREFIX):
        return "HIGH_BOUNCE_RATE"
    if source_work_identity.startswith(_RESYNC_PREFIX):
        return "RESYNC"
    return "OTHER"


class SafetyHoldOut(BaseModel):
    id: UUID
    kind: HoldKind
    status: Literal["ACTIVE", "RESOLVED"]
    reason: str
    created_at: datetime
    resolved_at: datetime | None = None


def _out(row: Any) -> SafetyHoldOut:
    return SafetyHoldOut(
        id=row["id"],
        kind=hold_kind(row["source_work_identity"]),
        status=row["status"],
        reason=row["reason"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


_COLUMNS = "id, source_work_identity, status, reason, created_at, resolved_at"


class SafetyHoldService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _require_mailbox(self, context: WorkspaceContext, mailbox_id: UUID) -> None:
        found = self.session.execute(
            text(
                "SELECT 1 FROM public.mailboxes "
                "WHERE workspace_id = :ws AND id = :mid"
            ),
            {"ws": str(context.workspace_id), "mid": str(mailbox_id)},
        ).first()
        if found is None:
            raise AppError("not_found", "Mailbox not found", status_code=404)

    def list_active(
        self, context: WorkspaceContext, mailbox_id: UUID
    ) -> list[SafetyHoldOut]:
        self._require_mailbox(context, mailbox_id)
        rows = (
            self.session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS} FROM public.safety_holds
                    WHERE workspace_id = :ws AND target_mailbox_id = :mid
                      AND status = 'ACTIVE'
                    ORDER BY created_at DESC
                    """  # noqa: S608 -- _COLUMNS is a constant
                ),
                {"ws": str(context.workspace_id), "mid": str(mailbox_id)},
            )
            .mappings()
            .all()
        )
        return [_out(r) for r in rows]

    def release(
        self, context: WorkspaceContext, mailbox_id: UUID, hold_id: UUID
    ) -> SafetyHoldOut:
        """Release one hold. Releasing a hold that is already released changes
        nothing, so a double click or a retry is harmless."""
        self._require_mailbox(context, mailbox_id)
        params = {
            "ws": str(context.workspace_id),
            "mid": str(mailbox_id),
            "hid": str(hold_id),
        }
        select = text(
            f"""
            SELECT {_COLUMNS} FROM public.safety_holds
            WHERE workspace_id = :ws AND target_mailbox_id = :mid AND id = :hid
            """  # noqa: S608 -- _COLUMNS is a constant
        )
        row = self.session.execute(select, params).mappings().first()
        if row is None:
            raise AppError("not_found", "Safety hold not found", status_code=404)
        if row["status"] != "ACTIVE":
            return _out(row)

        released = self.session.execute(
            text(
                """
                UPDATE public.safety_holds
                SET status = 'RESOLVED',
                    resolved_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :ws AND target_mailbox_id = :mid AND id = :hid
                  AND status = 'ACTIVE'
                """
            ),
            params,
        )
        if not getattr(released, "rowcount", 0):
            # Someone released it between the read and the write.
            return _out(self.session.execute(select, params).mappings().one())

        record_user_audit(
            self.session,
            context,
            action="mailbox.safety_hold_released",
            target_type="mailbox",
            target_id=mailbox_id,
            after_state={
                "hold_id": str(hold_id),
                "kind": hold_kind(row["source_work_identity"]),
                "reason": row["reason"],
            },
        )
        return _out(self.session.execute(select, params).mappings().one())
