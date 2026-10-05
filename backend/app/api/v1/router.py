from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.analytics import router as analytics_router
from app.api.v1.campaigns import router as campaigns_router
from app.api.v1.erasure import router as erasure_router
from app.api.v1.health import router as health_router
from app.api.v1.imports import router as imports_router
from app.api.v1.inbox import router as inbox_router
from app.api.v1.integrations import (
    provisioning_router as integrations_provisioning_router,
)
from app.api.v1.integrations import router as integrations_router
from app.api.v1.invitations import router as invitations_router
from app.api.v1.leads import router as leads_router
from app.api.v1.mailboxes import (
    callback_router as mailboxes_callback_router,
)
from app.api.v1.mailboxes import (
    router as mailboxes_router,
)
from app.api.v1.me import router as me_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.personalization import router as personalization_router
from app.api.v1.suppressions import router as suppressions_router
from app.api.v1.team import router as team_router
from app.api.v1.templates import router as templates_router
from app.api.v1.tracking import router as tracking_router
from app.api.v1.unsubscribe import router as unsubscribe_router
from app.api.v1.usage import router as usage_router
from app.api.v1.webhooks import router as webhooks_router
from app.api.v1.workspaces import router as workspaces_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(me_router)
api_router.include_router(invitations_router)
api_router.include_router(admin_router)
api_router.include_router(webhooks_router)
api_router.include_router(unsubscribe_router)
api_router.include_router(tracking_router)
api_router.include_router(mailboxes_callback_router, tags=["mailboxes"])
api_router.include_router(integrations_provisioning_router, tags=["integrations"])
api_router.include_router(workspaces_router, prefix="/workspaces", tags=["workspaces"])
api_router.include_router(
    mailboxes_router,
    prefix="/workspaces/{workspace_id}",
    tags=["mailboxes"],
)
api_router.include_router(
    leads_router,
    prefix="/workspaces/{workspace_id}",
    tags=["leads"],
)
api_router.include_router(
    templates_router,
    prefix="/workspaces/{workspace_id}",
    tags=["templates"],
)
api_router.include_router(
    imports_router,
    prefix="/workspaces/{workspace_id}",
    tags=["imports"],
)
api_router.include_router(
    suppressions_router,
    prefix="/workspaces/{workspace_id}",
    tags=["suppressions"],
)
api_router.include_router(
    campaigns_router,
    prefix="/workspaces/{workspace_id}",
    tags=["campaigns"],
)
api_router.include_router(
    personalization_router,
    prefix="/workspaces/{workspace_id}",
    tags=["personalization"],
)
api_router.include_router(
    inbox_router,
    prefix="/workspaces/{workspace_id}",
    tags=["inbox"],
)
api_router.include_router(
    analytics_router,
    prefix="/workspaces/{workspace_id}/analytics",
    tags=["analytics"],
)
api_router.include_router(
    team_router,
    prefix="/workspaces/{workspace_id}",
    tags=["team"],
)
api_router.include_router(
    notifications_router,
    prefix="/workspaces/{workspace_id}",
    tags=["notifications"],
)
api_router.include_router(
    usage_router,
    prefix="/workspaces/{workspace_id}",
    tags=["usage"],
)
api_router.include_router(
    erasure_router,
    prefix="/workspaces/{workspace_id}",
    tags=["erasure"],
)
api_router.include_router(
    integrations_router,
    prefix="/workspaces/{workspace_id}",
    tags=["integrations"],
)
