from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from app.core.errors import AppError


class ProviderCapability(StrEnum):
    """Capabilities a provider adapter may or may not support.

    Application/service code must check ``EmailProvider.capabilities``
    before relying on a capability instead of assuming every provider
    behaves like Gmail (e.g. SMTP has no OAuth flow or credential
    refresh). Calling an unsupported method raises
    ``UnsupportedCapabilityError`` rather than silently no-oping.
    """

    OAUTH_FLOW = "OAUTH_FLOW"
    IDENTITY_DISCOVERY = "IDENTITY_DISCOVERY"
    CONNECTION_VALIDATION = "CONNECTION_VALIDATION"
    SEND = "SEND"
    CREDENTIAL_REFRESH = "CREDENTIAL_REFRESH"
    TOKEN_REVOCATION = "TOKEN_REVOCATION"
    LOOKUP_MESSAGE = "LOOKUP_MESSAGE"
    REPLY_SYNC = "REPLY_SYNC"


class UnsupportedCapabilityError(AppError):
    def __init__(self, provider_name: str, capability: ProviderCapability) -> None:
        super().__init__(
            "unsupported_capability",
            f"{provider_name} does not support {capability.value}",
            status_code=400,
        )


class ErrorCategory(StrEnum):
    AUTH_FAILURE = "AUTH_FAILURE"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    RATE_LIMIT = "RATE_LIMIT"
    TEMPORARY_PROVIDER_ERROR = "TEMPORARY_PROVIDER_ERROR"
    PERMANENT_RECIPIENT_FAILURE = "PERMANENT_RECIPIENT_FAILURE"
    POLICY_REJECTION = "POLICY_REJECTION"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


@dataclass(frozen=True)
class TokenExchangeResult:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_in: int
    granted_scopes: list[str]
    id_token: str | None = None


@dataclass(frozen=True)
class TokenRefreshResult:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_in: int
    granted_scopes: list[str]


@dataclass(frozen=True)
class ProviderAccountIdentity:
    provider_account_id: str
    email_address: str
    display_name: str | None = None


@dataclass(frozen=True)
class ConnectionValidationResult:
    is_valid: bool
    # Providers with no identity/profile endpoint (e.g. SMTP) can only
    # confirm the credential authenticates successfully, not discover an
    # authoritative account identity -- those providers return None here.
    email_address: str | None
    provider_account_id: str | None
    scopes: list[str]


@dataclass(frozen=True)
class EnvelopeAttachment:
    """A file carried by an outbound message. `content_id` set means an inline
    image referenced from the HTML as cid:<content_id>; None is a regular
    attachment."""

    filename: str
    content_type: str
    data: bytes = field(repr=False)
    content_id: str | None = None


@dataclass(frozen=True)
class OutboundMessageEnvelope:
    to_address: str
    from_address: str
    from_name: str | None = None
    subject: str = ""
    body_html: str = ""
    body_text: str | None = None
    rfc_message_id: str | None = None
    attachments: tuple[EnvelopeAttachment, ...] = ()
    # Extra RFC 5322 headers (name, value), e.g. List-Unsubscribe. Written by the
    # raw-MIME providers (Gmail, SMTP). Microsoft Graph's JSON send only accepts
    # custom headers starting with "x-", so that adapter does not send them.
    extra_headers: tuple[tuple[str, str], ...] = ()
    # The provider thread to send into (a same-thread follow-up, ADR-0021). Only
    # Gmail uses it; the reply headers travel in extra_headers.
    provider_thread_id: str | None = None
    # The Message-ID being replied to (ADR-0021). Microsoft Graph, which ignores
    # extra_headers, finds that message and replies to it with createReply.
    in_reply_to: str | None = None


@dataclass(frozen=True)
class ProviderSendResult:
    status: Literal["ACCEPTED", "DEFINITIVELY_REJECTED", "UNKNOWN"]
    provider_message_id: str | None = None
    provider_thread_id: str | None = None
    # The Message-ID header the recipient will see and quote in In-Reply-To.
    # Reply/bounce correlation depends on it, so each adapter reports the
    # value the provider actually used rather than assuming ours was kept.
    rfc_message_id: str | None = None
    accepted_at: datetime | None = None
    error_category: str | None = None
    error_code: str | None = None
    raw_response: dict[str, Any] | None = None
    retry_after_seconds: float | None = None
    # The provider refused the requested thread and the message was sent outside
    # it (it still carries the reply headers).
    thread_dropped: bool = False


@dataclass(frozen=True)
class ClassifiedProviderError:
    category: ErrorCategory
    is_retryable: bool
    requires_reconnect: bool
    safe_message: str
    provider_code: str | None = None


@dataclass(frozen=True)
class ProviderInboundMessage:
    provider_message_id: str
    provider_thread_id: str | None = None
    rfc_message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    from_address: str = ""
    from_name: str | None = None
    to_addresses: list[str] = field(default_factory=list)
    cc_addresses: list[str] = field(default_factory=list)
    bcc_addresses: list[str] = field(default_factory=list)
    subject: str = ""
    body_text: str | None = None
    body_html: str | None = None
    received_at: datetime | None = None
    headers: dict[str, str] = field(default_factory=dict)
    is_automated: bool = False
    classification: str | None = None
    # Machine-readable parts of a delivery-status report (message/delivery-status
    # and the returned original headers), when the provider exposes them.
    report_text: str | None = None


@dataclass(frozen=True)
class SyncPageResult:
    messages: list[ProviderInboundMessage]
    next_cursor: str | None = None
    next_page_token: str | None = None
    has_more: bool = False
    resync_required: bool = False
    synced_checkpoint: str | None = None
    retry_after_seconds: float | None = None


class EmailProvider(Protocol):
    """Generic abstraction for email provider integrations.

    Not every provider supports every method: OAuth-only methods
    (``get_authorization_url``/``exchange_code``/``refresh_token``/
    ``get_identity``) are meaningless for SMTP, which has no
    authorization-code flow or identity endpoint. Providers that lack a
    capability raise ``UnsupportedCapabilityError`` for the corresponding
    method rather than a bare ``NotImplementedError`` -- callers should
    check ``capabilities`` first when a capability is optional.

    ``validate_connection`` and ``send_message`` are supported by every
    provider and take a provider-neutral ``credential`` mapping (the
    decrypted credential payload merged with any non-secret
    ``protected_config``) instead of a bare OAuth access token, so a
    Gmail/Microsoft credential (``{"access_token": ...}``) and an SMTP
    credential (``{"host", "port", "security_mode", "username",
    "password"}``) both fit through the same signature.
    """

    capabilities: frozenset[ProviderCapability]

    def get_authorization_url(
        self,
        state: str,
        redirect_uri: str,
        login_hint: str | None = None,
        code_challenge: str | None = None,
    ) -> str: ...

    def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> TokenExchangeResult: ...

    def refresh_token(self, refresh_token: str) -> TokenRefreshResult: ...

    def get_identity(self, access_token: str) -> ProviderAccountIdentity: ...

    def validate_connection(
        self, credential: Mapping[str, Any]
    ) -> ConnectionValidationResult: ...

    def send_message(
        self,
        credential: Mapping[str, Any],
        envelope: OutboundMessageEnvelope,
        *,
        authorization_deadline: datetime | None = None,
    ) -> ProviderSendResult: ...

    def classify_error(
        self,
        error: Exception | int | dict[str, Any],
    ) -> ClassifiedProviderError: ...

    def revoke_token(self, token: str) -> bool: ...

    def lookup_message(
        self,
        credential: Mapping[str, Any],
        rfc_message_id: str,
    ) -> ProviderSendResult | None: ...

    def sync_inbound_messages(
        self,
        credential: Mapping[str, Any],
        cursor: str | None = None,
        page_size: int = 50,
    ) -> SyncPageResult: ...
