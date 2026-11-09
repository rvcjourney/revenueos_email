from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext, get_db
from app.core.config import Settings
from app.core.permissions import require_permission
from app.modules.mailboxes.repository import MailboxRepository
from app.modules.mailboxes.schemas import (
    DisconnectResponse,
    GmailConnectCompleteRequest,
    GmailConnectCompleteResponse,
    GmailConnectStartRequest,
    GmailConnectStartResponse,
    MailboxDetail,
    MailboxListItem,
    MailboxTestSendRequest,
    MailboxTestSendResult,
    MailboxUpdate,
    MicrosoftConnectCompleteRequest,
    MicrosoftConnectCompleteResponse,
    MicrosoftConnectStartRequest,
    MicrosoftConnectStartResponse,
    SmtpConnectRequest,
    SmtpConnectResponse,
    SmtpUpdateRequest,
)
from app.modules.mailboxes.service import MailboxService
from app.modules.rate_limit.policy_service import (
    MailboxLimitsIn,
    MailboxLimitsOut,
    MailboxLimitsService,
)
from app.modules.safety.holds_service import SafetyHoldOut, SafetyHoldService

# Router for workspace-scoped mailbox management
router = APIRouter()

# Router for global callbacks (e.g. Google OAuth redirect receiver)
callback_router = APIRouter()


@router.get("/mailboxes", response_model=list[MailboxListItem])
def list_mailboxes(
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> list[MailboxListItem]:
    service = MailboxService(MailboxRepository(db))
    return service.list_mailboxes(context.workspace_id)


@router.get("/mailboxes/{mailbox_id}", response_model=MailboxDetail)
def get_mailbox(
    mailbox_id: UUID,
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> MailboxDetail:
    service = MailboxService(MailboxRepository(db))
    return service.get_mailbox(context.workspace_id, mailbox_id)


@router.patch("/mailboxes/{mailbox_id}", response_model=MailboxDetail)
def update_mailbox(
    mailbox_id: UUID,
    payload: MailboxUpdate,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> MailboxDetail:
    service = MailboxService(MailboxRepository(db))
    return service.update_mailbox(context.workspace_id, mailbox_id, payload)


@router.get("/mailboxes/{mailbox_id}/limits", response_model=MailboxLimitsOut)
def get_mailbox_limits(
    mailbox_id: UUID,
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> MailboxLimitsOut:
    """The mailbox's sending limit: emails per rolling 24 hours and the minimum gap."""
    return MailboxLimitsService(db).get(context, mailbox_id)


@router.put("/mailboxes/{mailbox_id}/limits", response_model=MailboxLimitsOut)
def set_mailbox_limits(
    mailbox_id: UUID,
    payload: MailboxLimitsIn,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> MailboxLimitsOut:
    return MailboxLimitsService(db).set(context, mailbox_id, payload)


@router.get(
    "/mailboxes/{mailbox_id}/safety-holds", response_model=list[SafetyHoldOut]
)
def list_mailbox_safety_holds(
    mailbox_id: UUID,
    context: WorkspaceContext = Depends(require_permission("product.read")),
    db: Session = Depends(get_db),
) -> list[SafetyHoldOut]:
    """Active safety holds on the mailbox. While any is active it does not send."""
    return SafetyHoldService(db).list_active(context, mailbox_id)


@router.post(
    "/mailboxes/{mailbox_id}/safety-holds/{hold_id}/release",
    response_model=SafetyHoldOut,
)
def release_mailbox_safety_hold(
    mailbox_id: UUID,
    hold_id: UUID,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> SafetyHoldOut:
    """Let a held mailbox send again. Recorded in the audit log."""
    return SafetyHoldService(db).release(context, mailbox_id, hold_id)


@router.post(
    "/mailboxes/connect/gmail/start",
    response_model=GmailConnectStartResponse,
)
def start_gmail_connect(
    payload: GmailConnectStartRequest,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> GmailConnectStartResponse:
    service = MailboxService(MailboxRepository(db))
    return service.start_gmail_oauth(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        return_path=payload.return_path,
    )


@router.post(
    "/mailboxes/connect/gmail/complete",
    response_model=GmailConnectCompleteResponse,
)
def complete_gmail_connect(
    payload: GmailConnectCompleteRequest,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> GmailConnectCompleteResponse:
    service = MailboxService(MailboxRepository(db))
    return service.complete_gmail_oauth(
        user_id=context.user_id,
        code=payload.code,
        state_token=payload.state,
    )


@router.post(
    "/mailboxes/{mailbox_id}/reconnect/gmail",
    response_model=GmailConnectStartResponse,
)
def reconnect_gmail(
    mailbox_id: UUID,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> GmailConnectStartResponse:
    service = MailboxService(MailboxRepository(db))
    return service.reconnect_gmail(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        mailbox_id=mailbox_id,
    )


@router.post(
    "/mailboxes/connect/microsoft/start",
    response_model=MicrosoftConnectStartResponse,
)
def start_microsoft_connect(
    payload: MicrosoftConnectStartRequest,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> MicrosoftConnectStartResponse:
    service = MailboxService(MailboxRepository(db))
    return service.start_microsoft_oauth(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        return_path=payload.return_path,
    )


@router.post(
    "/mailboxes/connect/microsoft/complete",
    response_model=MicrosoftConnectCompleteResponse,
)
def complete_microsoft_connect(
    payload: MicrosoftConnectCompleteRequest,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> MicrosoftConnectCompleteResponse:
    service = MailboxService(MailboxRepository(db))
    return service.complete_microsoft_oauth(
        user_id=context.user_id,
        code=payload.code,
        state_token=payload.state,
    )


@router.post(
    "/mailboxes/{mailbox_id}/reconnect/microsoft",
    response_model=MicrosoftConnectStartResponse,
)
def reconnect_microsoft(
    mailbox_id: UUID,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> MicrosoftConnectStartResponse:
    service = MailboxService(MailboxRepository(db))
    return service.reconnect_microsoft(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        mailbox_id=mailbox_id,
    )


@router.post(
    "/mailboxes/connect/smtp",
    response_model=SmtpConnectResponse,
)
def connect_smtp_mailbox(
    payload: SmtpConnectRequest,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> SmtpConnectResponse:
    service = MailboxService(MailboxRepository(db))
    return service.connect_smtp_mailbox(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        payload=payload,
    )


@router.patch(
    "/mailboxes/{mailbox_id}/smtp",
    response_model=MailboxDetail,
)
def update_smtp_mailbox(
    mailbox_id: UUID,
    payload: SmtpUpdateRequest,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> MailboxDetail:
    service = MailboxService(MailboxRepository(db))
    return service.update_smtp_mailbox(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        mailbox_id=mailbox_id,
        payload=payload,
    )


@router.post(
    "/mailboxes/{mailbox_id}/disconnect",
    response_model=DisconnectResponse,
)
def disconnect_mailbox(
    mailbox_id: UUID,
    context: WorkspaceContext = Depends(require_permission("mailboxes.manage")),
    db: Session = Depends(get_db),
) -> DisconnectResponse:
    service = MailboxService(MailboxRepository(db))
    return service.disconnect_mailbox(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        mailbox_id=mailbox_id,
    )


@router.post(
    "/mailboxes/{mailbox_id}/test-send",
    response_model=MailboxTestSendResult,
)
def send_controlled_test_email(
    mailbox_id: UUID,
    payload: MailboxTestSendRequest,
    context: WorkspaceContext = Depends(require_permission("campaigns.execute")),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> MailboxTestSendResult:
    service = MailboxService(MailboxRepository(db))
    return service.send_controlled_test_email(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        mailbox_id=mailbox_id,
        recipient_email=payload.recipient_email,
        request_key=idempotency_key,
    )


@callback_router.get("/mailboxes/connect/gmail/callback")
def gmail_oauth_browser_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Authoritative browser redirect endpoint registered with Google OAuth.

    Safely forwards the browser back to the authenticated frontend page where
    the user's existing Supabase session completes the flow with bearer auth.
    """
    settings = Settings.current()
    frontend_base = settings.frontend_base_url.rstrip("/")

    if error:
        return RedirectResponse(
            url=f"{frontend_base}/app/mailboxes?error={error}",
            status_code=302,
        )

    if not code or not state:
        return RedirectResponse(
            url=f"{frontend_base}/app/mailboxes?error=missing_callback_params",
            status_code=302,
        )

    return RedirectResponse(
        url=f"{frontend_base}/app/mailboxes/connect/gmail?code={code}&state={state}",
        status_code=302,
    )


@callback_router.get("/mailboxes/connect/microsoft/callback")
def microsoft_oauth_browser_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Authoritative browser redirect endpoint registered with Microsoft OAuth.

    Mirrors gmail_oauth_browser_callback: forwards the browser back to the
    authenticated frontend page where the user's existing Supabase session
    completes the flow with bearer auth.
    """
    settings = Settings.current()
    frontend_base = settings.frontend_base_url.rstrip("/")

    if error:
        return RedirectResponse(
            url=f"{frontend_base}/app/mailboxes?error={error}",
            status_code=302,
        )

    if not code or not state:
        return RedirectResponse(
            url=f"{frontend_base}/app/mailboxes?error=missing_callback_params",
            status_code=302,
        )

    return RedirectResponse(
        url=f"{frontend_base}/app/mailboxes/connect/microsoft?code={code}&state={state}",
        status_code=302,
    )
