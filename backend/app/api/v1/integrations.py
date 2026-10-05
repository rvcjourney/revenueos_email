from __future__ import annotations

import hmac
from dataclasses import dataclass
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
from app.modules.integrations.revenueos_provisioning import (
    RevenueOSProvisioningService,
    RevenueOSUserProvisionIn,
    RevenueOSUserProvisionOut,
)
from app.modules.integrations.supabase_auth_admin import (
    AuthAdminError,
    AuthAdminRejectedError,
    AuthAdminUnavailableError,
    EnsuredUser,
    SupabaseAuthAdminClient,
)

router = APIRouter()
# Routes that are not scoped to one workspace (they create it).
provisioning_router = APIRouter()


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


@dataclass(frozen=True)
class ProvisionRequest:
    actor_id: UUID
    payload: RevenueOSUserProvisionIn
    user: EnsuredUser


def prepare_revenueos_user(
    payload: RevenueOSUserProvisionIn,
    actor_id: UUID = Depends(require_revenueos_actor),
) -> ProvisionRequest:
    """Find or create the person's account before any database work (ADR-0020).

    Resolved before `get_db` on purpose: the account lives in Supabase Auth,
    reached over the network, and a pooled connection must not be held while
    waiting on it. Creating the account first is safe to repeat: a retry finds
    it by email.
    """
    settings = Settings.current()
    if (
        not settings.revenueos_provisioning_enabled
        or not settings.supabase_service_role_key
    ):
        raise AppError(
            "integration_not_configured",
            "RevenueOS user provisioning is not enabled",
            status_code=503,
        )
    try:
        user = SupabaseAuthAdminClient(settings).ensure_user(payload.email)
    except AuthAdminUnavailableError as exc:
        raise AppError(
            "service_unavailable",
            "The account service is temporarily unavailable. Please retry.",
            status_code=503,
        ) from exc
    except AuthAdminRejectedError as exc:
        raise AppError(
            "validation_error",
            "The account service refused this email address",
            status_code=422,
        ) from exc
    except AuthAdminError as exc:
        raise AppError(
            "provider_error",
            "The account service returned an unexpected response",
            status_code=502,
        ) from exc
    return ProvisionRequest(actor_id=actor_id, payload=payload, user=user)


@provisioning_router.post(
    "/integrations/revenueos/users",
    response_model=RevenueOSUserProvisionOut,
    status_code=status.HTTP_201_CREATED,
)
def provision_revenueos_user(
    response: Response,
    # Declared before `db`: see prepare_revenueos_user.
    prepared: ProvisionRequest = Depends(prepare_revenueos_user),
    db: Session = Depends(get_db),
) -> RevenueOSUserProvisionOut:
    """Give a person an account and a workspace owned by the RevenueOS acting
    user. Never returns or accepts a password."""
    result = RevenueOSProvisioningService(db).provision(
        actor_id=prepared.actor_id, payload=prepared.payload, user=prepared.user
    )
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return result
