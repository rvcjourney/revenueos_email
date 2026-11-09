from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.core.crypto import encrypt_credentials, encrypt_verifier
from app.core.errors import PROVIDER_AUTH_REJECTED_STATUS, AppError
from app.modules.mailboxes.providers.base import (
    ConnectionValidationResult,
    EmailProvider,
    OutboundMessageEnvelope,
    ProviderAccountIdentity,
    ProviderCapability,
    ProviderSendResult,
    TokenExchangeResult,
    TokenRefreshResult,
    UnsupportedCapabilityError,
)
from app.modules.mailboxes.providers.registry import ProviderRegistry
from app.modules.mailboxes.repository import MailboxRepository
from app.modules.mailboxes.schemas import (
    SmtpConnectRequest,
    SmtpSecurityMode,
    SmtpUpdateRequest,
)
from app.modules.mailboxes.service import MailboxService


class FakeDomainTestProvider(EmailProvider):
    capabilities = frozenset(
        {
            ProviderCapability.OAUTH_FLOW,
            ProviderCapability.IDENTITY_DISCOVERY,
            ProviderCapability.CONNECTION_VALIDATION,
            ProviderCapability.SEND,
            ProviderCapability.CREDENTIAL_REFRESH,
            ProviderCapability.TOKEN_REVOCATION,
        }
    )

    def __init__(self, email: str = "sender@example.com") -> None:
        self.email = email
        self.sent_envelopes: list[OutboundMessageEnvelope] = []
        self.revoked_tokens: list[str] = []

    def get_authorization_url(
        self,
        state: str,
        redirect_uri: str,
        login_hint: str | None = None,
        code_challenge: str | None = None,
    ) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}&code_challenge={code_challenge}"

    def exchange_code(
        self, code: str, redirect_uri: str, code_verifier: str
    ) -> TokenExchangeResult:
        return TokenExchangeResult(
            access_token="fake-access-token",
            refresh_token="fake-refresh-token",
            expires_in=3600,
            token_type="Bearer",
            granted_scopes=["https://www.googleapis.com/auth/gmail.send"],
        )

    def refresh_token(self, refresh_token: str) -> TokenRefreshResult:
        return TokenRefreshResult(
            access_token="refreshed-access-token",
            expires_in=3600,
            token_type="Bearer",
            refresh_token="fake-refresh-token",
            granted_scopes=["https://www.googleapis.com/auth/gmail.send"],
        )

    def get_identity(self, access_token: str) -> ProviderAccountIdentity:
        return ProviderAccountIdentity(
            email_address=self.email,
            display_name="Sender Name",
            provider_account_id=f"google-{self.email}",
        )

    def validate_connection(self, credential: dict) -> ConnectionValidationResult:
        return ConnectionValidationResult(
            is_valid=True,
            email_address=self.email,
            provider_account_id=f"google-{self.email}",
            scopes=[],
        )

    def send_message(
        self, credential: dict, envelope: OutboundMessageEnvelope
    ) -> ProviderSendResult:
        self.sent_envelopes.append(envelope)
        return ProviderSendResult(
            status="ACCEPTED",
            provider_message_id=f"msg-{uuid.uuid4().hex[:8]}",
            provider_thread_id="thread-123",
            accepted_at=datetime.now(UTC),
        )

    def revoke_token(self, token: str) -> None:
        self.revoked_tokens.append(token)

    def classify_error(self, exc: Exception) -> None:
        return None


class FakeSmtpTestProvider(EmailProvider):
    """SMTP fake: no OAuth capabilities, connection-validation + send only."""

    capabilities = frozenset(
        {ProviderCapability.CONNECTION_VALIDATION, ProviderCapability.SEND}
    )

    def __init__(
        self, *, validate_should_fail: bool = False, send_should_fail: bool = False
    ) -> None:
        self.validate_should_fail = validate_should_fail
        self.send_should_fail = send_should_fail
        self.validated_credentials: list[dict] = []
        self.sent_envelopes: list[OutboundMessageEnvelope] = []

    def get_authorization_url(self, *args, **kwargs) -> str:
        raise UnsupportedCapabilityError("SMTP", ProviderCapability.OAUTH_FLOW)

    def exchange_code(self, *args, **kwargs) -> TokenExchangeResult:
        raise UnsupportedCapabilityError("SMTP", ProviderCapability.OAUTH_FLOW)

    def refresh_token(self, refresh_token: str) -> TokenRefreshResult:
        raise UnsupportedCapabilityError("SMTP", ProviderCapability.CREDENTIAL_REFRESH)

    def get_identity(self, access_token: str) -> ProviderAccountIdentity:
        raise UnsupportedCapabilityError("SMTP", ProviderCapability.IDENTITY_DISCOVERY)

    def validate_connection(self, credential: dict) -> ConnectionValidationResult:
        self.validated_credentials.append(dict(credential))
        if self.validate_should_fail:
            raise AppError(
                "auth_failure",
                "SMTP authentication failed",
                status_code=PROVIDER_AUTH_REJECTED_STATUS,
            )
        return ConnectionValidationResult(
            is_valid=True, email_address=None, provider_account_id=None, scopes=[]
        )

    def send_message(
        self, credential: dict, envelope: OutboundMessageEnvelope
    ) -> ProviderSendResult:
        self.sent_envelopes.append(envelope)
        if self.send_should_fail:
            return ProviderSendResult(status="DEFINITIVELY_REJECTED")
        return ProviderSendResult(status="ACCEPTED", accepted_at=datetime.now(UTC))

    def revoke_token(self, token: str) -> bool:
        return False

    def classify_error(self, exc) -> None:
        return None


@pytest.fixture
def mock_repo() -> MagicMock:
    return MagicMock(spec=MailboxRepository)


@pytest.fixture
def mailbox_service(mock_repo: MagicMock) -> MailboxService:
    return MailboxService(repo=mock_repo)


@pytest.fixture(autouse=True)
def register_fake_provider() -> None:
    fake_gmail = FakeDomainTestProvider()
    fake_microsoft = FakeDomainTestProvider(email="ms-sender@example.com")
    ProviderRegistry.register("GMAIL", fake_gmail)
    ProviderRegistry.register("MICROSOFT", fake_microsoft)
    yield
    ProviderRegistry.reset()


def test_start_gmail_oauth_creates_flow_and_url(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()

    resp = mailbox_service.start_gmail_oauth(
        workspace_id=workspace_id,
        user_id=actor_id,
        return_path="/app/mailboxes",
    )

    assert "https://accounts.google.com/o/oauth2/v2/auth" in resp.authorization_url
    assert "code_challenge=" in resp.authorization_url
    assert mock_repo.insert_oauth_flow.called


def test_complete_gmail_oauth_invalid_or_consumed_state_rejected(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    actor_id = uuid.uuid4()

    # When state is not found, expired, or already claimed
    mock_repo.get_and_claim_oauth_flow.return_value = None

    with pytest.raises(AppError) as exc_info:
        mailbox_service.complete_gmail_oauth(
            user_id=actor_id,
            code="auth-code",
            state_token="invalid-or-consumed-state",
        )
    assert exc_info.value.code == "invalid_oauth_state"
    assert exc_info.value.status_code == 400


def test_complete_gmail_oauth_actor_mismatch_rejected(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    mock_repo.get_and_claim_oauth_flow.return_value = {
        "id": flow_id,
        "workspace_id": workspace_id,
        "actor_id": other_user_id,  # Started by someone else
        "return_path": "/app/mailboxes",
    }

    with pytest.raises(AppError) as exc_info:
        mailbox_service.complete_gmail_oauth(
            user_id=actor_id,
            code="auth-code",
            state_token="valid-state",
        )
    assert exc_info.value.code == "forbidden"
    assert exc_info.value.status_code == 403
    assert mock_repo.fail_oauth_flow.called


def test_complete_gmail_oauth_reconnect_email_mismatch_rejected(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    flow_id = uuid.uuid4()
    target_mailbox_id = uuid.uuid4()

    raw_verifier = "a" * 50
    enc_verifier, key_id, nonce = encrypt_verifier(
        raw_verifier, workspace_id, actor_id, "GMAIL"
    )

    mock_repo.get_and_claim_oauth_flow.return_value = {
        "id": flow_id,
        "workspace_id": workspace_id,
        "actor_id": actor_id,
        "return_path": f"/app/mailboxes/{target_mailbox_id}",
        "encrypted_verifier": enc_verifier,
        "verifier_nonce": nonce,
        "verifier_key_id": key_id,
    }
    mock_repo.get_active_mailbox_by_provider_account_global.return_value = None

    # Target mailbox has a different address
    mock_repo.get_mailbox.return_value = {
        "id": target_mailbox_id,
        "original_address": "other-address@example.com",
        "provider_account_id": "google-other-address@example.com",
    }

    with pytest.raises(AppError) as exc_info:
        mailbox_service.complete_gmail_oauth(
            user_id=actor_id,
            code="auth-code",
            state_token="valid-state",
        )
    assert exc_info.value.code == "account_mismatch"
    assert exc_info.value.status_code == 400
    assert mock_repo.fail_oauth_flow.called


def test_complete_gmail_oauth_success_new_mailbox(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    raw_verifier = "b" * 50
    enc_verifier, key_id, nonce = encrypt_verifier(
        raw_verifier, workspace_id, actor_id, "GMAIL"
    )

    mock_repo.get_and_claim_oauth_flow.return_value = {
        "id": flow_id,
        "workspace_id": workspace_id,
        "actor_id": actor_id,
        "return_path": "/app/mailboxes",
        "encrypted_verifier": enc_verifier,
        "verifier_nonce": nonce,
        "verifier_key_id": key_id,
    }
    mock_repo.get_active_mailbox_by_provider_account_global.return_value = None
    mock_repo.get_mailbox_by_provider_account.return_value = None  # New mailbox

    result = mailbox_service.complete_gmail_oauth(
        user_id=actor_id,
        code="auth-code",
        state_token="valid-state-2",
    )

    assert result.email_address == "sender@example.com"
    assert result.connection_state == "CONNECTED"
    assert result.health_state == "HEALTHY"
    assert mock_repo.insert_mailbox_connection.called
    assert mock_repo.insert_mailbox.called
    assert mock_repo.complete_oauth_flow.called


def test_complete_gmail_oauth_identity_failure_fails_flow(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    raw_verifier = "c" * 50
    enc_verifier, key_id, nonce = encrypt_verifier(
        raw_verifier, workspace_id, actor_id, "GMAIL"
    )

    mock_repo.get_and_claim_oauth_flow.return_value = {
        "id": flow_id,
        "workspace_id": workspace_id,
        "actor_id": actor_id,
        "return_path": "/app/mailboxes",
        "encrypted_verifier": enc_verifier,
        "verifier_nonce": nonce,
        "verifier_key_id": key_id,
    }

    failing_provider = FakeDomainTestProvider()

    def _raise_identity_error(access_token: str) -> ProviderAccountIdentity:
        raise AppError(
            "provider_error",
            "Failed to retrieve Gmail identity: Google did not grant the "
            "permissions this connection needs. Please reconnect and approve "
            "all requested access.",
            status_code=PROVIDER_AUTH_REJECTED_STATUS,
        )

    failing_provider.get_identity = _raise_identity_error  # type: ignore[method-assign]
    ProviderRegistry.register("GMAIL", failing_provider)

    with pytest.raises(AppError) as exc_info:
        mailbox_service.complete_gmail_oauth(
            user_id=actor_id,
            code="auth-code",
            state_token="valid-state-3",
        )
    assert exc_info.value.status_code == PROVIDER_AUTH_REJECTED_STATUS
    assert mock_repo.fail_oauth_flow.called
    assert mock_repo.fail_oauth_flow.call_args[0][0] == flow_id
    assert not mock_repo.insert_mailbox.called


def test_disconnect_mailbox_clears_connection_and_revokes(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()

    mock_repo.get_mailbox.return_value = {
        "id": mailbox_id,
        "original_address": "sender@example.com",
        "provider": "GMAIL",
        "connection_state": "CONNECTED",
        "current_connection_generation": 1,
    }
    mock_repo.get_mailbox_connection.return_value = {
        "credential_ciphertext": None,
        "nonce": None,
        "encryption_key_id": "k1",
    }

    resp = mailbox_service.disconnect_mailbox(workspace_id, actor_id, mailbox_id)

    assert resp.mailbox_id == mailbox_id
    assert resp.connection_state == "DISCONNECTED"
    assert mock_repo.destroy_mailbox_connection.called
    assert mock_repo.update_mailbox_connection_state.called


def test_send_controlled_test_email_suppression_blocked(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()
    address_id = uuid.uuid4()

    mock_repo.get_mailbox.return_value = {
        "id": mailbox_id,
        "original_address": "sender@example.com",
        "sender_display_name": "Sender Name",
        "signature_html": None,
        "connection_state": "CONNECTED",
        "health_state": "HEALTHY",
        "policy_state": "ENABLED",
        "policy_reason": None,
    }
    mock_repo.ensure_recipient_address.return_value = address_id
    # Recipient is suppressed!
    mock_repo.is_address_suppressed.return_value = True

    with pytest.raises(AppError) as exc_info:
        mailbox_service.send_controlled_test_email(
            workspace_id=workspace_id,
            user_id=actor_id,
            mailbox_id=mailbox_id,
            recipient_email="blocked@example.com",
        )
    assert exc_info.value.code == "suppressed_recipient"
    assert exc_info.value.status_code == 400


def test_send_controlled_test_email_unhealthy_mailbox_rejected(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()

    # Mailbox is disconnected
    mock_repo.get_mailbox.return_value = {
        "id": mailbox_id,
        "original_address": "sender@example.com",
        "sender_display_name": "Sender Name",
        "signature_html": None,
        "connection_state": "DISCONNECTED",
        "health_state": "UNHEALTHY",
        "policy_state": "ENABLED",
        "policy_reason": None,
    }

    with pytest.raises(AppError) as exc_info:
        mailbox_service.send_controlled_test_email(
            workspace_id=workspace_id,
            user_id=actor_id,
            mailbox_id=mailbox_id,
            recipient_email="ok@example.com",
        )
    assert exc_info.value.code == "conflict"
    assert exc_info.value.status_code == 409


def test_send_controlled_test_email_success(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()
    address_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    receipt_id = uuid.uuid4()

    mock_repo.get_mailbox.return_value = {
        "id": mailbox_id,
        "original_address": "sender@example.com",
        "sender_display_name": "Sender Name",
        "signature_html": "<p>Best regards</p>",
        "connection_state": "CONNECTED",
        "health_state": "HEALTHY",
        "policy_state": "ENABLED",
        "policy_reason": None,
        "current_connection_generation": 1,
        "provider": "GMAIL",
    }
    mock_repo.ensure_recipient_address.return_value = address_id
    mock_repo.is_address_suppressed.return_value = False
    mock_repo.get_user_membership_id.return_value = membership_id
    mock_repo.ensure_command_receipt.return_value = receipt_id

    creds_payload = {
        "access_token": "valid-token",
        "refresh_token": "valid-refresh",
        "token_type": "Bearer",
    }
    enc_creds, key_id, nonce = encrypt_credentials(
        creds_payload, workspace_id, mailbox_id, "GMAIL"
    )

    mock_repo.get_mailbox_connection.return_value = {
        "credential_ciphertext": enc_creds,
        "nonce": nonce,
        "encryption_key_id": key_id,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }

    result = mailbox_service.send_controlled_test_email(
        workspace_id=workspace_id,
        user_id=actor_id,
        mailbox_id=mailbox_id,
        recipient_email="recipient@example.com",
    )

    assert result.status == "SENT"
    assert result.provider_message_id is not None
    assert mock_repo.insert_test_message.called
    assert mock_repo.insert_message_attempt.called
    assert mock_repo.update_message_and_attempt_result.called


def test_send_controlled_test_email_merges_protected_config_for_smtp(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    """The one production call site that was silently Gmail-shaped: the
    credential passed to provider.send_message must be a merged mapping
    (decrypted secret + protected_config), not a bare access token."""
    fake_smtp = FakeSmtpTestProvider()
    ProviderRegistry.register("SMTP", fake_smtp)

    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()
    address_id = uuid.uuid4()

    mock_repo.get_mailbox.return_value = {
        "id": mailbox_id,
        "original_address": "sender@example.com",
        "sender_display_name": "Sender Name",
        "signature_html": None,
        "connection_state": "CONNECTED",
        "health_state": "HEALTHY",
        "policy_state": "ENABLED",
        "policy_reason": None,
        "current_connection_generation": 1,
        "provider": "SMTP",
    }
    mock_repo.ensure_recipient_address.return_value = address_id
    mock_repo.is_address_suppressed.return_value = False
    mock_repo.get_user_membership_id.return_value = uuid.uuid4()
    mock_repo.ensure_command_receipt.return_value = uuid.uuid4()

    creds_payload = {"password": "s3cret"}
    enc_creds, key_id, nonce = encrypt_credentials(
        creds_payload, workspace_id, mailbox_id, "SMTP"
    )
    mock_repo.get_mailbox_connection.return_value = {
        "credential_ciphertext": enc_creds,
        "nonce": nonce,
        "encryption_key_id": key_id,
        "expires_at": None,
        "protected_config": {
            "host": "smtp.example.com",
            "port": 587,
            "security_mode": "STARTTLS",
            "username": "user@example.com",
        },
    }

    result = mailbox_service.send_controlled_test_email(
        workspace_id=workspace_id,
        user_id=actor_id,
        mailbox_id=mailbox_id,
        recipient_email="recipient@example.com",
    )

    assert result.status == "SENT"
    assert len(fake_smtp.sent_envelopes) == 1


# -------------------------------------------------------------------------
# Microsoft OAuth (mirrors the Gmail flow tests above)
# -------------------------------------------------------------------------


def test_start_microsoft_oauth_creates_flow_and_url(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()

    resp = mailbox_service.start_microsoft_oauth(
        workspace_id=workspace_id,
        user_id=actor_id,
        return_path="/app/mailboxes",
    )

    assert "code_challenge=" in resp.authorization_url
    assert mock_repo.insert_oauth_flow.called
    assert mock_repo.insert_oauth_flow.call_args.kwargs["provider"] == "MICROSOFT"


def test_complete_microsoft_oauth_success_new_mailbox(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    raw_verifier = "d" * 50
    enc_verifier, key_id, nonce = encrypt_verifier(
        raw_verifier, workspace_id, actor_id, "MICROSOFT"
    )

    mock_repo.get_and_claim_oauth_flow.return_value = {
        "id": flow_id,
        "workspace_id": workspace_id,
        "actor_id": actor_id,
        "return_path": "/app/mailboxes",
        "encrypted_verifier": enc_verifier,
        "verifier_nonce": nonce,
        "verifier_key_id": key_id,
    }
    mock_repo.get_active_mailbox_by_provider_account_global.return_value = None
    mock_repo.get_mailbox_by_provider_account.return_value = None

    result = mailbox_service.complete_microsoft_oauth(
        user_id=actor_id,
        code="auth-code",
        state_token="valid-state",
    )

    assert result.provider == "MICROSOFT"
    assert result.email_address == "ms-sender@example.com"
    assert result.connection_state == "CONNECTED"
    assert mock_repo.insert_mailbox.called
    assert mock_repo.insert_mailbox.call_args.kwargs["provider"] == "MICROSOFT"
    assert mock_repo.complete_oauth_flow.called


def test_complete_microsoft_oauth_identity_failure_fails_flow(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    raw_verifier = "e" * 50
    enc_verifier, key_id, nonce = encrypt_verifier(
        raw_verifier, workspace_id, actor_id, "MICROSOFT"
    )
    mock_repo.get_and_claim_oauth_flow.return_value = {
        "id": flow_id,
        "workspace_id": workspace_id,
        "actor_id": actor_id,
        "return_path": "/app/mailboxes",
        "encrypted_verifier": enc_verifier,
        "verifier_nonce": nonce,
        "verifier_key_id": key_id,
    }

    failing_provider = FakeDomainTestProvider()

    def _raise_identity_error(access_token: str) -> ProviderAccountIdentity:
        raise AppError(
            "provider_error",
            "Microsoft identity lookup failed",
            status_code=PROVIDER_AUTH_REJECTED_STATUS,
        )

    failing_provider.get_identity = _raise_identity_error  # type: ignore[method-assign]
    ProviderRegistry.register("MICROSOFT", failing_provider)

    with pytest.raises(AppError) as exc_info:
        mailbox_service.complete_microsoft_oauth(
            user_id=actor_id,
            code="auth-code",
            state_token="valid-state",
        )
    assert exc_info.value.status_code == PROVIDER_AUTH_REJECTED_STATUS
    assert mock_repo.fail_oauth_flow.called
    assert not mock_repo.insert_mailbox.called


def test_complete_microsoft_oauth_global_account_conflict_rejected(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    other_workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    flow_id = uuid.uuid4()

    raw_verifier = "f" * 50
    enc_verifier, key_id, nonce = encrypt_verifier(
        raw_verifier, workspace_id, actor_id, "MICROSOFT"
    )
    mock_repo.get_and_claim_oauth_flow.return_value = {
        "id": flow_id,
        "workspace_id": workspace_id,
        "actor_id": actor_id,
        "return_path": "/app/mailboxes",
        "encrypted_verifier": enc_verifier,
        "verifier_nonce": nonce,
        "verifier_key_id": key_id,
    }
    mock_repo.get_active_mailbox_by_provider_account_global.return_value = {
        "id": uuid.uuid4(),
        "workspace_id": other_workspace_id,
    }

    with pytest.raises(AppError) as exc_info:
        mailbox_service.complete_microsoft_oauth(
            user_id=actor_id,
            code="auth-code",
            state_token="valid-state",
        )
    assert exc_info.value.code == "conflict"
    assert mock_repo.fail_oauth_flow.called


def test_reconnect_microsoft_wrong_provider_rejected(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()

    mock_repo.get_mailbox.return_value = {
        "id": mailbox_id,
        "provider": "GMAIL",
        "original_address": "sender@example.com",
    }

    with pytest.raises(AppError) as exc_info:
        mailbox_service.reconnect_microsoft(workspace_id, actor_id, mailbox_id)
    assert exc_info.value.code == "bad_request"


# -------------------------------------------------------------------------
# Custom SMTP
# -------------------------------------------------------------------------


def _smtp_connect_payload(**overrides) -> SmtpConnectRequest:
    base = dict(
        host="smtp.example.com",
        port=587,
        security_mode=SmtpSecurityMode.STARTTLS,
        username="user@example.com",
        password="s3cret-pw",
        email_address="sender@example.com",
        sender_display_name="Sender Name",
    )
    base.update(overrides)
    return SmtpConnectRequest(**base)


def test_connect_smtp_mailbox_success(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    fake_smtp = FakeSmtpTestProvider()
    ProviderRegistry.register("SMTP", fake_smtp)

    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mock_repo.get_active_mailbox_by_provider_account_global.return_value = None

    result = mailbox_service.connect_smtp_mailbox(
        workspace_id=workspace_id,
        user_id=actor_id,
        payload=_smtp_connect_payload(),
    )

    assert result.provider == "SMTP"
    assert result.connection_state == "CONNECTED"
    assert len(fake_smtp.validated_credentials) == 1
    assert mock_repo.insert_mailbox.called
    assert mock_repo.insert_mailbox_connection.called
    call_kwargs = mock_repo.insert_mailbox_connection.call_args.kwargs
    assert call_kwargs["auth_mechanism"] == "SMTP_PASSWORD"
    assert call_kwargs["protected_config"]["host"] == "smtp.example.com"
    assert "password" not in call_kwargs["protected_config"]


def test_connect_smtp_mailbox_validation_failure_creates_no_mailbox(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    fake_smtp = FakeSmtpTestProvider(validate_should_fail=True)
    ProviderRegistry.register("SMTP", fake_smtp)

    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mock_repo.get_active_mailbox_by_provider_account_global.return_value = None

    with pytest.raises(AppError) as exc_info:
        mailbox_service.connect_smtp_mailbox(
            workspace_id=workspace_id,
            user_id=actor_id,
            payload=_smtp_connect_payload(),
        )
    assert exc_info.value.code == "auth_failure"
    assert not mock_repo.insert_mailbox.called
    assert not mock_repo.insert_mailbox_connection.called


def test_connect_smtp_mailbox_global_conflict_rejected(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    fake_smtp = FakeSmtpTestProvider()
    ProviderRegistry.register("SMTP", fake_smtp)

    workspace_id = uuid.uuid4()
    other_workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mock_repo.get_active_mailbox_by_provider_account_global.return_value = {
        "id": uuid.uuid4(),
        "workspace_id": other_workspace_id,
    }

    with pytest.raises(AppError) as exc_info:
        mailbox_service.connect_smtp_mailbox(
            workspace_id=workspace_id,
            user_id=actor_id,
            payload=_smtp_connect_payload(),
        )
    assert exc_info.value.code == "conflict"
    assert not mock_repo.insert_mailbox.called


def test_update_smtp_mailbox_password_omitted_reuses_existing(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    fake_smtp = FakeSmtpTestProvider()
    ProviderRegistry.register("SMTP", fake_smtp)

    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()

    mock_repo.get_mailbox.return_value = {
        "id": mailbox_id,
        "provider": "SMTP",
        "original_address": "sender@example.com",
        "sender_display_name": "Sender Name",
        "signature_html": None,
        "current_connection_generation": 1,
    }

    existing_creds = {"password": "original-secret-pw"}
    enc_creds, key_id, nonce = encrypt_credentials(
        existing_creds, workspace_id, mailbox_id, "SMTP"
    )
    mock_repo.get_mailbox_connection.return_value = {
        "credential_ciphertext": enc_creds,
        "nonce": nonce,
        "encryption_key_id": key_id,
        "protected_config": {
            "host": "smtp.example.com",
            "port": 587,
            "security_mode": "STARTTLS",
            "username": "user@example.com",
        },
    }
    # get_mailbox is called again at the end via self.get_mailbox(...)
    mock_repo.get_mailbox.side_effect = [
        mock_repo.get_mailbox.return_value,
        {
            "id": mailbox_id,
            "provider": "SMTP",
            "original_address": "sender@example.com",
            "sender_display_name": "Sender Name",
            "signature_html": None,
            "connection_state": "CONNECTED",
            "health_state": "HEALTHY",
            "policy_state": "ENABLED",
            "policy_reason": None,
            "circuit_state": "CLOSED",
            "sync_state": "CURRENT",
            "blocked_until": None,
            "pending_safety_count": 0,
            "current_connection_generation": 2,
            "connected_generation": 2,
            "config_version": 1,
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        },
    ]

    # Only updating the display name; host/port/username/password all omitted.
    mailbox_service.update_smtp_mailbox(
        workspace_id=workspace_id,
        user_id=actor_id,
        mailbox_id=mailbox_id,
        payload=SmtpUpdateRequest(sender_display_name="New Display Name"),
    )

    # The re-validation must have used the EXISTING password, not a blank one.
    assert fake_smtp.validated_credentials[-1]["password"] == "original-secret-pw"
    assert fake_smtp.validated_credentials[-1]["host"] == "smtp.example.com"


def test_update_smtp_mailbox_revalidation_failure_leaves_existing_untouched(
    mailbox_service: MailboxService, mock_repo: MagicMock
) -> None:
    fake_smtp = FakeSmtpTestProvider(validate_should_fail=True)
    ProviderRegistry.register("SMTP", fake_smtp)

    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    mailbox_id = uuid.uuid4()

    mock_repo.get_mailbox.return_value = {
        "id": mailbox_id,
        "provider": "SMTP",
        "original_address": "sender@example.com",
        "sender_display_name": "Sender Name",
        "signature_html": None,
        "current_connection_generation": 1,
    }
    existing_creds = {"password": "original-secret-pw"}
    enc_creds, key_id, nonce = encrypt_credentials(
        existing_creds, workspace_id, mailbox_id, "SMTP"
    )
    mock_repo.get_mailbox_connection.return_value = {
        "credential_ciphertext": enc_creds,
        "nonce": nonce,
        "encryption_key_id": key_id,
        "protected_config": {
            "host": "smtp.example.com",
            "port": 587,
            "security_mode": "STARTTLS",
            "username": "user@example.com",
        },
    }

    with pytest.raises(AppError):
        mailbox_service.update_smtp_mailbox(
            workspace_id=workspace_id,
            user_id=actor_id,
            mailbox_id=mailbox_id,
            payload=SmtpUpdateRequest(password="new-bad-password"),
        )

    # No new connection generation was activated -- fail closed.
    assert not mock_repo.insert_mailbox_connection.called
    assert not mock_repo.update_mailbox_connection_state.called
