from __future__ import annotations

import httpx
import pytest

from app.core.errors import PROVIDER_AUTH_REJECTED_STATUS, AppError
from app.modules.mailboxes.providers.base import (
    ErrorCategory,
    OutboundMessageEnvelope,
)
from app.modules.mailboxes.providers.gmail import GmailProvider
from app.modules.mailboxes.providers.registry import ProviderRegistry


def test_get_authorization_url() -> None:
    provider = GmailProvider(client_id="test-client-id", client_secret="test-secret")
    url = provider.get_authorization_url(
        state="random-state-123",
        redirect_uri="http://localhost:8000/callback",
        code_challenge="test-challenge",
    )

    assert "https://accounts.google.com/o/oauth2/v2/auth" in url
    assert "client_id=test-client-id" in url
    assert "state=random-state-123" in url
    assert "code_challenge=test-challenge" in url
    assert "code_challenge_method=S256" in url
    assert "prompt=consent" in url
    assert "access_type=offline" in url
    assert "gmail.send" in url


def test_header_injection_protection() -> None:
    provider = GmailProvider(client_id="id", client_secret="secret")

    # Subject with CRLF injection attempt
    envelope_bad_subject = OutboundMessageEnvelope(
        to_address="victim@example.com",
        from_address="sender@example.com",
        subject="Hello\r\nBcc: evil@example.com",
        body_html="<p>test</p>",
    )
    with pytest.raises(AppError) as exc_info:
        provider.send_message({"access_token": "fake-token"}, envelope_bad_subject)
    assert exc_info.value.code == "header_injection"
    assert exc_info.value.status_code == 422

    # Recipient with newline
    envelope_bad_to = OutboundMessageEnvelope(
        to_address="victim@example.com\nBcc: evil@example.com",
        from_address="sender@example.com",
        subject="Clean Subject",
        body_html="<p>test</p>",
    )
    with pytest.raises(AppError) as exc_info:
        provider.send_message({"access_token": "fake-token"}, envelope_bad_to)
    assert exc_info.value.code == "header_injection"


def test_send_message_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        if request.method == "GET":
            # Read back the Message-ID Gmail actually stored for the sent copy.
            assert request.url.path.endswith("/messages/msg-12345")
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "headers": [{"name": "Message-ID", "value": "<gmail-actual@mail.gmail.com>"}]
                    }
                },
            )
        assert b"raw" in request.content
        return httpx.Response(
            200,
            json={"id": "msg-12345", "threadId": "thread-67890"},
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GmailProvider(
        client_id="id", client_secret="secret", http_client=mock_client
    )

    envelope = OutboundMessageEnvelope(
        to_address="lead@example.com",
        from_address="sales@company.com",
        from_name="Sales Team",
        subject="Introduction",
        body_html="<p>Hello world</p>",
        rfc_message_id="unique-rfc-id-123",
    )

    result = provider.send_message({"access_token": "fake-token"}, envelope)
    assert result.status == "ACCEPTED"
    assert result.rfc_message_id == "<gmail-actual@mail.gmail.com>"
    assert result.provider_message_id == "msg-12345"
    assert result.provider_thread_id == "thread-67890"
    assert result.accepted_at is not None


def test_send_message_timeout_becomes_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Connection timed out waiting for Google response")

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GmailProvider(
        client_id="id", client_secret="secret", http_client=mock_client
    )

    envelope = OutboundMessageEnvelope(
        to_address="lead@example.com",
        from_address="sales@company.com",
        subject="Test",
        body_html="<p>Test</p>",
    )

    result = provider.send_message({"access_token": "fake-token"}, envelope)
    assert result.status == "UNKNOWN"
    assert result.error_category == ErrorCategory.UNKNOWN_OUTCOME
    assert result.error_code == "request_timeout"


def test_error_classification() -> None:
    provider = GmailProvider()

    # 401 Auth Failure
    c401 = provider.classify_error(401, {"error": {"message": "Invalid Credentials"}})
    assert c401.category == ErrorCategory.AUTH_FAILURE
    assert c401.requires_reconnect is True
    assert c401.is_retryable is False

    # invalid_grant
    c_grant = provider.classify_error(
        400, {"error": "invalid_grant", "error_description": "Token has been revoked."}
    )
    assert c_grant.category == ErrorCategory.AUTH_FAILURE
    assert c_grant.requires_reconnect is True

    # 429 Rate limit
    c429 = provider.classify_error(
        429, {"error": {"message": "User Rate Limit Exceeded"}}
    )
    assert c429.category == ErrorCategory.RATE_LIMIT
    assert c429.is_retryable is True
    assert c429.requires_reconnect is False

    # 503 Service Unavailable
    c503 = provider.classify_error(503, {"error": {"message": "Backend Error"}})
    assert c503.category == ErrorCategory.TEMPORARY_PROVIDER_ERROR
    assert c503.is_retryable is True

    # 400 Invalid Recipient
    c_recip = provider.classify_error(
        400, {"error": {"message": "Invalid recipient address"}}
    )
    assert c_recip.category == ErrorCategory.PERMANENT_RECIPIENT_FAILURE
    assert c_recip.is_retryable is False

    # 403 Insufficient scope / permission denied
    c403 = provider.classify_error(
        403, {"error": {"message": "Request had insufficient authentication scopes"}}
    )
    assert c403.category == ErrorCategory.INSUFFICIENT_SCOPE
    assert c403.requires_reconnect is True
    assert c403.is_retryable is False
    assert "message could not be sent" not in c403.safe_message.lower()

    # Unclassified error falls back to a provider-neutral message, not one
    # hardcoded for the send-message path
    c_unknown = provider.classify_error(418, {"error": {"message": "I'm a teapot"}})
    assert c_unknown.category == ErrorCategory.POLICY_REJECTION
    assert "message could not be sent" not in c_unknown.safe_message.lower()


def test_get_identity_success_via_userinfo_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openidconnect.googleapis.com"
        assert request.headers["Authorization"] == "Bearer fake-token"
        return httpx.Response(
            200,
            json={"sub": "1234567890", "email": "User@Example.com", "email_verified": True},
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GmailProvider(
        client_id="id", client_secret="secret", http_client=mock_client
    )

    identity = provider.get_identity("fake-token")
    assert identity.email_address == "user@example.com"
    assert identity.provider_account_id == "user@example.com"


def test_get_identity_insufficient_scope_403() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "Request had insufficient authentication scopes.",
                }
            },
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GmailProvider(
        client_id="id", client_secret="secret", http_client=mock_client
    )

    with pytest.raises(AppError) as exc_info:
        provider.get_identity("fake-token")
    assert exc_info.value.status_code == PROVIDER_AUTH_REJECTED_STATUS
    assert "message could not be sent" not in exc_info.value.message.lower()
    assert "permissions" in exc_info.value.message.lower()


def test_get_identity_malformed_response_raises_clean_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sub": "1234567890"})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GmailProvider(
        client_id="id", client_secret="secret", http_client=mock_client
    )

    with pytest.raises(AppError) as exc_info:
        provider.get_identity("fake-token")
    assert exc_info.value.status_code == 502


def test_exchange_code_malformed_response_raises_clean_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "Bearer"})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GmailProvider(
        client_id="id", client_secret="secret", http_client=mock_client
    )

    with pytest.raises(AppError) as exc_info:
        provider.exchange_code(code="auth-code", redirect_uri="http://localhost/cb")
    assert exc_info.value.status_code == 502


def test_provider_registry() -> None:
    ProviderRegistry.reset()
    provider = ProviderRegistry.get("GMAIL")
    assert isinstance(provider, GmailProvider)

    with pytest.raises(AppError) as exc_info:
        ProviderRegistry.get("YAHOO")
    assert exc_info.value.code == "unsupported_provider"


def test_send_message_falls_back_to_our_message_id_when_readback_is_forbidden() -> None:
    """A mailbox connected before the read scope existed cannot read the sent
    copy back; the send still succeeded, so our Message-ID is recorded."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(403, json={"error": {"code": 403}})
        return httpx.Response(200, json={"id": "m-1", "threadId": "t-1"})

    provider = GmailProvider(
        client_id="id",
        client_secret="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.send_message(
        {"access_token": "fake-token"},
        OutboundMessageEnvelope(
            to_address="lead@example.com",
            from_address="sender@example.com",
            subject="Hi",
            body_html="<p>x</p>",
            rfc_message_id="<ours@example.com>",
        ),
    )
    assert result.status == "ACCEPTED"
    assert result.rfc_message_id == "<ours@example.com>"
    assert result.provider_thread_id == "t-1"
