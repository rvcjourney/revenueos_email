from __future__ import annotations

import hmac
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_db
from app.core.config import Settings
from app.core.errors import AppError
from app.core.permissions import has_permission
from app.db.context import set_transaction_context
from app.modules.integrations.revenueos_intake import (
    RevenueOSCampaignIntakeIn,
    RevenueOSCampaignIntakeOut,
    RevenueOSIntakeService,
)

router = APIRouter()


def require_revenueos_actor(
    revenueos_key: str | None = Header(default=None, alias="X-RevenueOS-Key"),
) -> UUID:
    """Authenticate RevenueOS by its shared key (ADR-0019) and return the user
    it acts as.

    The key proves the caller is RevenueOS; it carries no identity of its own.
    The acting user is fixed in server configuration, never taken from the
    request, so a caller cannot choose whom to act as. Without both settings the
    route is disabled rather than open. Touches no database.
    """
    settings = Settings.current()
    expected = settings.revenueos_intake_key.get_secret_value()
    if not expected or not settings.revenueos_actor_user_id:
        raise AppError(
            "integration_not_configured",
            "The RevenueOS integration is not configured",
            status_code=503,
        )
    if not revenueos_key or not hmac.compare_digest(
        revenueos_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise AppError(
            "unauthenticated", "Invalid RevenueOS integration key", status_code=401
        )
    return UUID(settings.revenueos_actor_user_id)


def get_revenueos_context(
    workspace_id: UUID,
    # Declared before `db`: FastAPI resolves parameters in order, so a wrong key
    # is rejected before a pooled connection is checked out.
    actor_id: UUID = Depends(require_revenueos_actor),
    db: Session = Depends(get_db),
) -> WorkspaceContext:
    """The acting user's re-validated membership in the requested workspace.

    Same contract as get_workspace_context + require_permission: the role is
    resolved from the current ACTIVE membership under RLS, a workspace the
    acting user does not belong to is 404, and campaigns.draft is required.
    The key therefore grants nothing in a workspace RevenueOS was not added to.
    """
    set_transaction_context(db, user_id=actor_id, workspace_id=workspace_id)
    role_code = db.execute(text("SELECT public.app_current_workspace_role()")).scalar()
    if role_code is None:
        raise AppError("not_found", "Workspace not found", status_code=404)
    if not has_permission(role_code, "campaigns.draft"):
        raise AppError(
            "forbidden",
            "You do not have permission to perform this action",
            status_code=403,
        )
    return WorkspaceContext(
        workspace_id=workspace_id, user_id=actor_id, role_code=role_code
    )


@router.post(
    "/integrations/revenueos/campaigns",
    response_model=RevenueOSCampaignIntakeOut,
    status_code=status.HTTP_201_CREATED,
)
def store_revenueos_campaign(
    payload: RevenueOSCampaignIntakeIn,
    response: Response,
    context: WorkspaceContext = Depends(get_revenueos_context),
    db: Session = Depends(get_db),
) -> RevenueOSCampaignIntakeOut:
    """Store a complete STANDARD draft campaign sent by RevenueOS. Never
    activates it: a person reviews the audience and starts it in the UI."""
    result = RevenueOSIntakeService(db).store_campaign(context, payload)
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return result
