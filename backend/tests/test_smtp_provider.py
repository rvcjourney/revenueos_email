from __future__ import annotations

import socket
import ssl

import pytest

from app.core.errors import PROVIDER_AUTH_REJECTED_STATUS, AppError
from app.modules.mailboxes.providers import smtp as smtp_module
from app.modules.mailboxes.providers.base import (
    ErrorCategory,
    OutboundMessageEnvelope,
    ProviderCapability,
    UnsupportedCapabilityError,
)
from app.modules.mailboxes.providers.smtp import SmtpProvider
from app.modules.mailboxes.providers.ssrf import ResolvedTarget
from tests.support.fake_smtp_server import FakeSmtpServer

ENVELOPE = OutboundMessageEnvelope(
    to_address="lead@example.com",
    from_address="sales@company.com",
    from_name="Sales Team",
    subject="Introduction",
    body_html="<p>Hello world</p>",
)


def _resolver_for(server: FakeSmtpServer):
    def _resolve(host: str, port: int, *, dns_timeout: float) -> ResolvedTarget:
        return ResolvedTarget(
            hostname="localhost",
            port=server.port,
            resolved_ip="127.0.0.1",
            address_family=socket.AF_INET,
        )

    return _resolve


def _trust_server_cert(monkeypatch: pytest.MonkeyPatch, server: FakeSmtpServer) -> None:
    """Make the client trust the fake server's self-signed test cert.

    This is a test-only patch of ssl.create_default_context, reverted
    automatically by monkeypatch teardown. Production code never touches
    this -- it always calls the real ssl.create_default_context(), which
    correctly rejects untrusted/self-signed certificates.
    """
    trusted_ctx = ssl.create_default_context(cadata=server._cert_pem.decode("ascii"))
    monkeypatch.setattr(
        smtp_module.ssl, "create_default_context", lambda *a, **k: trusted_ctx
    )


def _credential(**overrides: object) -> dict:
    base = {
        "host": "smtp.example.com",
        "port": 587,
        "security_mode": "STARTTLS",
        "username": "user@example.com",
        "password": "s3cret",
    }
    base.update(overrides)
    return base


# -- Capability guards ------------------------------------------------------


def test_capabilities_are_validation_send_and_imap_backed_reply_sync() -> None:
    provider = SmtpProvider()
    # REPLY_SYNC is only usable for mailboxes with IMAP settings; without them
    # sync_inbound_messages refuses (see test_reply_provider_sync).
    assert provider.capabilities == frozenset(
        {
            ProviderCapability.CONNECTION_VALIDATION,
            ProviderCapability.SEND,
            ProviderCapability.REPLY_SYNC,
        }
    )


@pytest.mark.parametrize(
    "method_name,args",
    [
        ("get_authorization_url", ("state", "http://cb")),
        ("exchange_code", ("code", "http://cb")),
        ("refresh_token", ("rt",)),
        ("get_identity", ("at",)),
    ],
)
def test_oauth_only_methods_raise_unsupported_capability(method_name, args) -> None:
    provider = SmtpProvider()
    with pytest.raises(UnsupportedCapabilityError):
        getattr(provider, method_name)(*args)


def test_revoke_token_returns_false_without_raising() -> None:
    provider = SmtpProvider()
    assert provider.revoke_token("anything") is False


# -- Successful flows ---------------------------------------------------


def test_validate_connection_starttls_success(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeSmtpServer(mode="starttls", auth_ok=True, recipient_ok=True)
    _trust_server_cert(monkeypatch, server)
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=5.0)

    result = provider.validate_connection(_credential())
    assert result.is_valid is True
    assert result.email_address is None  # SMTP has no identity endpoint
    server.stop()


def test_send_message_starttls_success(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeSmtpServer(mode="starttls", auth_ok=True, recipient_ok=True)
    _trust_server_cert(monkeypatch, server)
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=5.0)

    result = provider.send_message(_credential(), ENVELOPE)
    assert result.status == "ACCEPTED"
    server.stop()


def test_send_message_implicit_tls_success(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeSmtpServer(mode="implicit_tls", auth_ok=True, recipient_ok=True)
    _trust_server_cert(monkeypatch, server)
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=5.0)

    result = provider.send_message(
        _credential(port=465, security_mode="IMPLICIT_TLS"), ENVELOPE
    )
    assert result.status == "ACCEPTED"
    server.stop()


# -- Failure classification --------------------------------------------


def test_send_message_auth_failure_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeSmtpServer(mode="starttls", auth_ok=False)
    _trust_server_cert(monkeypatch, server)
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=5.0)

    result = provider.send_message(_credential(), ENVELOPE)
    assert result.status == "DEFINITIVELY_REJECTED"
    assert result.error_category == ErrorCategory.AUTH_FAILURE
    server.stop()


def test_validate_connection_auth_failure_raises_app_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeSmtpServer(mode="starttls", auth_ok=False)
    _trust_server_cert(monkeypatch, server)
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=5.0)

    with pytest.raises(AppError) as exc_info:
        provider.validate_connection(_credential())
    assert exc_info.value.code == "auth_failure"
    # Must never be 401: the browser signs the user out on any 401, and a wrong
    # SMTP password is not an invalid platform session.
    assert exc_info.value.status_code == PROVIDER_AUTH_REJECTED_STATUS
    assert exc_info.value.status_code != 401
    server.stop()


def test_send_message_recipient_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeSmtpServer(mode="starttls", auth_ok=True, recipient_ok=False)
    _trust_server_cert(monkeypatch, server)
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=5.0)

    result = provider.send_message(_credential(), ENVELOPE)
    assert result.status == "DEFINITIVELY_REJECTED"
    assert result.error_category == ErrorCategory.PERMANENT_RECIPIENT_FAILURE
    server.stop()


def test_send_message_no_starttls_support_hard_fails_never_downgrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeSmtpServer(mode="no_starttls")
    # Deliberately NOT trusting the cert here -- a no-STARTTLS server never
    # reaches a TLS handshake at all, so this proves the failure is due to
    # the missing STARTTLS extension, not an incidental cert issue.
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=5.0)

    result = provider.send_message(_credential(), ENVELOPE)
    assert result.status == "DEFINITIVELY_REJECTED"

    # The client must never have sent AUTH (i.e. credentials) over the
    # plaintext connection -- it must fail closed before authenticating.
    plaintext_lines = [line for tls_active, line in server.transcript if not tls_active]
    assert not any("AUTH" in line.upper() for line in plaintext_lines)
    server.stop()


def test_send_message_untrusted_certificate_rejected() -> None:
    # No _trust_server_cert() call: the client uses the real default
    # context, which must reject this self-signed certificate.
    server = FakeSmtpServer(mode="starttls", auth_ok=True, recipient_ok=True)
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=5.0)

    result = provider.send_message(_credential(), ENVELOPE)
    assert result.status == "DEFINITIVELY_REJECTED"
    assert result.error_category == ErrorCategory.NETWORK_ERROR
    server.stop()


def test_send_message_connect_timeout_is_definitive_not_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Resolver points at a reserved TEST-NET address (RFC 5737): nothing
    # listens there, so the connection attempt itself blocks until the
    # provider's own connect_timeout fires. This exercises the "connect
    # never completes" path distinctly from FakeSmtpServer's protocol
    # scenarios, entirely offline. A timeout here happens BEFORE anything
    # was submitted, so it is safe to classify as a definite non-send
    # rather than an ambiguous UNKNOWN outcome (UNKNOWN is reserved for a
    # connection lost mid-submission, tested separately).
    def _resolve(host: str, port: int, *, dns_timeout: float) -> ResolvedTarget:
        return ResolvedTarget(
            hostname="localhost",
            port=587,
            resolved_ip="192.0.2.1",
            address_family=socket.AF_INET,
        )

    provider = SmtpProvider(resolver=_resolve, connect_timeout=0.5)
    result = provider.send_message(_credential(), ENVELOPE)
    assert result.status == "DEFINITIVELY_REJECTED"
    assert result.error_category in (ErrorCategory.NETWORK_ERROR,)


def test_send_message_connection_lost_mid_submission_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeSmtpServer(
        mode="starttls", auth_ok=True, recipient_ok=True, hang_after_data_prompt=True
    )
    _trust_server_cert(monkeypatch, server)
    # Short command timeout so the hung final acknowledgement is observed
    # quickly rather than waiting out the server's 30s sleep.
    provider = SmtpProvider(resolver=_resolver_for(server), connect_timeout=1.0)

    result = provider.send_message(_credential(), ENVELOPE)
    # The message body WAS fully submitted before the connection went
    # silent -- this must never be guessed as a safe rejection or a
    # confirmed success.
    assert result.status == "UNKNOWN"
    assert result.error_category == ErrorCategory.UNKNOWN_OUTCOME
    server.stop()


# -- Header injection guard shared with other providers -----------------


def test_send_message_header_injection_rejected() -> None:
    provider = SmtpProvider()
    bad_envelope = OutboundMessageEnvelope(
        to_address="victim@example.com",
        from_address="sender@example.com",
        subject="Hi\r\nBcc: evil@example.com",
        body_html="<p>test</p>",
    )
    with pytest.raises(AppError) as exc_info:
        provider.send_message(_credential(), bad_envelope)
    assert exc_info.value.code == "header_injection"
