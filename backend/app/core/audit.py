from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext


def record_user_audit(
    session: Session,
    context: WorkspaceContext,
    *,
    action: str,
    target_type: str,
    target_id: UUID,
    after_state: dict[str, Any] | None = None,
) -> None:
    """Write a USER audit event for a change a person made.

    Runs as `app_api`, whose insert policy requires the actor to be the current
    user and the workspace the current one, so an event cannot be forged for
    someone else. Joins the caller's transaction.
    """
    session.execute(
        text(
            """
            INSERT INTO public.audit_events
                (workspace_id, actor_kind, actor_id, action, target_type,
                 target_id, after_state)
            VALUES
                (:workspace_id, 'USER', :actor_id, :action, :target_type,
                 :target_id, CAST(:after_state AS jsonb))
            """
        ),
        {
            "workspace_id": str(context.workspace_id),
            "actor_id": str(context.user_id),
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "after_state": json.dumps(after_state or {}),
        },
    )
