from __future__ import annotations

import imaplib
import smtplib
import socket
import ssl
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.errors import PROVIDER_AUTH_REJECTED_STATUS, AppError
from app.modules.mailboxes.providers.base import (
    ClassifiedProviderError,
    ConnectionValidationResult,
    EmailProvider,
    ErrorCategory,
    OutboundMessageEnvelope,
    ProviderAccountIdentity,
    ProviderCapability,
    ProviderSendResult,
    SyncPageResult,
    TokenExchangeResult,
    TokenRefreshResult,
    UnsupportedCapabilityError,
)
from app.modules.mailboxes.providers.imap_sync import (
    ImapConnector,
    ImapLibSession,
    imap_configured,
    imap_credential,
    sync_imap_page,
)
from app.modules.mailboxes.providers.message_builder import (
    build_rfc5322_message,
    validate_header_value,
)
from app.modules.mailboxes.providers.ssrf import (
    ResolvedTarget,
    UnsafeDestinationError,
    resolve_and_validate,
)

ResolverFn = Callable[..., ResolvedTarget]


class _PinnedSMTP(smtplib.SMTP):
    """smtplib.SMTP that connects to a pre-validated, pinned IP address.

    The hostname passed to the constructor (``target.hostname``) is still
    used for STARTTLS SNI/certificate-hostname verification via smtplib's
    own ``self._host`` -- only the raw TCP connection step is redirected to
    the pinned address, closing the DNS-rebinding window between
    validation and connection.
    """

    def __init__(self, target: ResolvedTarget, timeout: float) -> None:
        self._pinned_ip = target.resolved_ip
        super().__init__(target.hostname, target.port, timeout=timeout)

    def _get_socket(
        self, host: str, port: int, timeout: float
    ) -> socket.socket:
        return socket.create_connection((self._pinned_ip, port), timeout)


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    """Implicit-TLS variant of ``_PinnedSMTP`` (port 465)."""

    def __init__(self, target: ResolvedTarget, timeout: float) -> None:
        self._pinned_ip = target.resolved_ip
        context = ssl.create_default_context()  # CERT_REQUIRED, check_hostname=True
        super().__init__(
            target.hostname, target.port, timeout=timeout, context=context
        )

    def _get_socket(
        self, host: str, port: int, timeout: float
    ) -> ssl.SSLSocket:
        # `host` here is the ORIGINAL hostname smtplib was constructed with
        # (see __init__ above) -- used only for SNI/certificate-hostname
        # verification. The actual TCP connection goes to the pinned,
        # pre-validated IP address.
        raw_socket = socket.create_connection((self._pinned_ip, port), timeout)
        return self.context.wrap_socket(raw_socket, server_hostname=host)


def _safe_quit(conn: smtplib.SMTP) -> None:
    try:
        conn.quit()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


class SmtpProvider(EmailProvider):
    """Custom SMTP compatibility provider.

    Not an OAuth provider: authentication is a per-mailbox username/password
    validated against a user-supplied host/port. All network access goes
    through the SSRF-safe resolver/connector in ``ssrf.py`` -- this class
    never opens a socket directly.
    """

    # REPLY_SYNC is available only for mailboxes that also carry IMAP settings
    # (see imap_configured); SMTP alone can never receive mail.
    capabilities = frozenset(
        {
            ProviderCapability.CONNECTION_VALIDATION,
            ProviderCapability.SEND,
            ProviderCapability.REPLY_SYNC,
        }
    )

    def __init__(
        self,
        resolver: ResolverFn = resolve_and_validate,
        dns_timeout: float | None = None,
        connect_timeout: float | None = None,
        imap_connector: ImapConnector | None = None,
    ) -> None:
        # `resolver` is a test-only dependency-injection seam (see
        # tests/test_smtp_provider.py and the integration SMTP fixture).
        # Production code (ProviderRegistry.get("SMTP")) always constructs
        # SmtpProvider() with zero arguments, so the real SSRF-safe
        # resolve_and_validate is the only resolver ever used outside tests.
        settings = Settings.current()
        self._resolver = resolver
        # Test-only seam, like ``resolver``: production uses the real,
        # SSRF-pinned imaplib session.
        self._imap_connector: ImapConnector = imap_connector or (
            lambda cred: ImapLibSession(cred, resolver=resolver)
        )
        self._dns_timeout = dns_timeout or settings.smtp_dns_timeout_seconds
        self._connect_timeout = connect_timeout or settings.smtp_connect_timeout_seconds

    # -- OAuth-only capabilities: not supported by SMTP -------------------

    def get_authorization_url(
        self,
        state: str,
        redirect_uri: str,
        login_hint: str | None = None,
        code_challenge: str | None = None,
    ) -> str:
        raise UnsupportedCapabilityError("SMTP", ProviderCapability.OAUTH_FLOW)

    def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> TokenExchangeResult:
        raise UnsupportedCapabilityError("SMTP", ProviderCapability.OAUTH_FLOW)

    def refresh_token(self, refresh_token: str) -> TokenRefreshResult:
        raise UnsupportedCapabilityError("SMTP", ProviderCapability.CREDENTIAL_REFRESH)

    def get_identity(self, access_token: str) -> ProviderAccountIdentity:
        raise UnsupportedCapabilityError("SMTP", ProviderCapability.IDENTITY_DISCOVERY)

    def revoke_token(self, token: str) -> bool:
        # SMTP has no token to revoke; nothing to do. Not a capability, but
        # returning False (rather than raising) keeps this safe to call
        # from disconnect's best-effort revocation path even though that
        # path already skips SMTP in practice (see MailboxService.
        # disconnect_mailbox: an SMTP credential payload has neither
        # "refresh_token" nor "access_token", so revoke_token is never
        # actually invoked for SMTP mailboxes).
        return False

    # -- Supported capabilities --------------------------------------------

    def _connect_and_auth(
        self,
        credential: Mapping[str, Any],
    ) -> smtplib.SMTP:
        host = str(credential["host"])
        port = int(credential["port"])
        security_mode = str(credential["security_mode"])
        username = str(credential["username"])
        password = str(credential["password"])

        target = self._resolver(host, port, dns_timeout=self._dns_timeout)

        if security_mode == "IMPLICIT_TLS":
            conn: smtplib.SMTP = _PinnedSMTPSSL(target, timeout=self._connect_timeout)
        elif security_mode == "STARTTLS":
            conn = _PinnedSMTP(target, timeout=self._connect_timeout)
            # Raises SMTPNotSupportedError if the server doesn't advertise
            # STARTTLS -- this must fail closed, never fall back to
            # plaintext submission.
            conn.starttls(context=ssl.create_default_context())
        else:
            raise AppError(
                "bad_request",
                f"Unsupported SMTP security mode: {security_mode}",
                status_code=422,
            )

        conn.login(username, password)
        return conn

    def validate_connection(
        self, credential: Mapping[str, Any]
    ) -> ConnectionValidationResult:
        try:
            conn = self._connect_and_auth(credential)
        except UnsafeDestinationError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            classified = self.classify_error(exc)
            raise AppError(
                "auth_failure",
                classified.safe_message,
                status_code=PROVIDER_AUTH_REJECTED_STATUS,
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            # OSError already covers TimeoutError/socket.timeout (an alias)
            # and ssl.SSLError (an OSError subclass).
            classified = self.classify_error(exc)
            raise AppError(
                "provider_error", classified.safe_message, status_code=502
            ) from exc

        _safe_quit(conn)
        # SMTP has no identity/profile endpoint: this only confirms the
        # credential authenticates successfully, it cannot authoritatively
        # discover an account identity the way Gmail/Microsoft can.
        return ConnectionValidationResult(
            is_valid=True,
            email_address=None,
            provider_account_id=None,
            scopes=[],
        )

    def send_message(
        self,
        credential: Mapping[str, Any],
        envelope: OutboundMessageEnvelope,
        *,
        # Only Graph uses it (a send-time budget); the others ignore it.
        authorization_deadline: datetime | None = None,
    ) -> ProviderSendResult:
        validate_header_value("To", envelope.to_address)
        validate_header_value("From", envelope.from_address)
        validate_header_value("Sender Name", envelope.from_name)
        validate_header_value("Subject", envelope.subject)

        msg = build_rfc5322_message(envelope)

        # Connect + authenticate. Nothing has been submitted to the remote
        # server yet at this point, so any failure here is a safe,
        # definitive "message was not sent" -- never ambiguous.
        try:
            conn = self._connect_and_auth(credential)
        except UnsafeDestinationError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            classified = self.classify_error(exc)
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=classified.category,
                error_code=classified.provider_code,
            )
        except (OSError, smtplib.SMTPException) as exc:
            classified = self.classify_error(exc)
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=classified.category,
                error_code=classified.provider_code,
                raw_response={"detail": exc.__class__.__name__},
            )

        # Submission phase: a definite SMTP response (even a rejection) is
        # NOT ambiguous -- the server told us what happened. Only a
        # connection loss/timeout with no final response is genuinely
        # ambiguous (message may have been accepted before the drop).
        try:
            conn.send_message(
                msg, from_addr=envelope.from_address, to_addrs=[envelope.to_address]
            )
        except smtplib.SMTPRecipientsRefused:
            _safe_quit(conn)
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=ErrorCategory.PERMANENT_RECIPIENT_FAILURE,
                error_code="recipient_refused",
            )
        except smtplib.SMTPResponseException as exc:
            _safe_quit(conn)
            classified = self.classify_error(exc)
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=classified.category,
                error_code=classified.provider_code,
            )
        except (smtplib.SMTPServerDisconnected, OSError):
            # Connection lost mid-submission: acceptance is unknown, not
            # rejected. Must not be guessed as safe-to-retry.
            return ProviderSendResult(
                status="UNKNOWN",
                error_category=ErrorCategory.UNKNOWN_OUTCOME,
                error_code="connection_lost_during_submission",
            )

        _safe_quit(conn)
        # SMTP returns no provider id, so the Message-ID we set is the only
        # handle for correlating replies/bounces (and the acceptance evidence).
        return ProviderSendResult(
            status="ACCEPTED",
            provider_message_id=(
                envelope.rfc_message_id.strip("<>") if envelope.rfc_message_id else None
            ),
            rfc_message_id=envelope.rfc_message_id,
            accepted_at=datetime.now(UTC),
        )

    def classify_error(
        self,
        error: Exception | int | dict[str, Any],
        details: dict[str, Any] | None = None,
    ) -> ClassifiedProviderError:
        if isinstance(error, smtplib.SMTPAuthenticationError):
            return ClassifiedProviderError(
                category=ErrorCategory.AUTH_FAILURE,
                is_retryable=False,
                requires_reconnect=True,
                safe_message=(
                    "SMTP authentication failed. Check the configured "
                    "username and password."
                ),
                provider_code=str(error.smtp_code),
            )
        if isinstance(error, smtplib.SMTPResponseException):
            code = error.smtp_code
            return self._classify_smtp_code(code)
        if isinstance(error, int):
            return self._classify_smtp_code(error)
        if isinstance(error, ssl.SSLCertVerificationError):
            return ClassifiedProviderError(
                category=ErrorCategory.NETWORK_ERROR,
                is_retryable=False,
                requires_reconnect=False,
                safe_message="TLS certificate validation failed for the SMTP server.",
                provider_code="tls_certificate_error",
            )
        if isinstance(error, ssl.SSLError):
            return ClassifiedProviderError(
                category=ErrorCategory.NETWORK_ERROR,
                is_retryable=False,
                requires_reconnect=False,
                safe_message="TLS handshake with the SMTP server failed.",
                provider_code="tls_error",
            )
        if isinstance(error, smtplib.SMTPNotSupportedError):
            return ClassifiedProviderError(
                category=ErrorCategory.POLICY_REJECTION,
                is_retryable=False,
                requires_reconnect=False,
                safe_message=(
                    "The SMTP server does not support the required "
                    "STARTTLS extension."
                ),
                provider_code="starttls_unsupported",
            )
        if isinstance(error, TimeoutError):  # covers socket.timeout (an alias)
            return ClassifiedProviderError(
                category=ErrorCategory.NETWORK_ERROR,
                is_retryable=True,
                requires_reconnect=False,
                safe_message="Connecting to the SMTP server timed out.",
                provider_code="connect_timeout",
            )
        if isinstance(error, OSError):
            return ClassifiedProviderError(
                category=ErrorCategory.NETWORK_ERROR,
                is_retryable=True,
                requires_reconnect=False,
                safe_message="Could not reach the SMTP server.",
                provider_code="network_error",
            )
        return ClassifiedProviderError(
            category=ErrorCategory.POLICY_REJECTION,
            is_retryable=False,
            requires_reconnect=False,
            safe_message="The SMTP server rejected the request.",
            provider_code="unknown",
        )

    @staticmethod
    def _classify_smtp_code(code: int) -> ClassifiedProviderError:
        # SMTP semantics: 4xx is temporary, 5xx is permanent (opposite of
        # HTTP's convention where 5xx is often retried).
        if code in (534, 535, 530):
            return ClassifiedProviderError(
                category=ErrorCategory.AUTH_FAILURE,
                is_retryable=False,
                requires_reconnect=True,
                safe_message="SMTP authentication was rejected by the server.",
                provider_code=str(code),
            )
        if code == 421:
            return ClassifiedProviderError(
                category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                is_retryable=True,
                requires_reconnect=False,
                safe_message="The SMTP server is temporarily unavailable.",
                provider_code=str(code),
            )
        if code in (450, 451, 452):
            return ClassifiedProviderError(
                category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                is_retryable=True,
                requires_reconnect=False,
                safe_message=(
                    "The SMTP server temporarily could not process the request."
                ),
                provider_code=str(code),
            )
        if code in (550, 551, 553):
            return ClassifiedProviderError(
                category=ErrorCategory.PERMANENT_RECIPIENT_FAILURE,
                is_retryable=False,
                requires_reconnect=False,
                safe_message="The recipient address was rejected by the SMTP server.",
                provider_code=str(code),
            )
        if code in (552, 554):
            return ClassifiedProviderError(
                category=ErrorCategory.POLICY_REJECTION,
                is_retryable=False,
                requires_reconnect=False,
                safe_message="The SMTP server rejected the message.",
                provider_code=str(code),
            )
        if 400 <= code < 500:
            return ClassifiedProviderError(
                category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                is_retryable=True,
                requires_reconnect=False,
                safe_message="The SMTP server temporarily rejected the request.",
                provider_code=str(code),
            )
        return ClassifiedProviderError(
            category=ErrorCategory.POLICY_REJECTION,
            is_retryable=False,
            requires_reconnect=False,
            safe_message="The SMTP server rejected the request.",
            provider_code=str(code),
        )

    def lookup_message(
        self,
        credential: Mapping[str, Any],
        rfc_message_id: str,
    ) -> ProviderSendResult | None:
        """SMTP protocol does not provide a remote message search capability."""
        return None

    def validate_imap(self, credential: Mapping[str, Any]) -> None:
        """Log in to the IMAP server and open INBOX. Raises AppError on failure
        so a mailbox is never saved with reply sync that cannot work."""
        imap_cred = imap_credential(credential)
        try:
            session = self._imap_connector(imap_cred)
        except UnsafeDestinationError:
            raise
        except imaplib.IMAP4.error as exc:
            raise AppError(
                "auth_failure",
                "IMAP login failed. Check the IMAP username and password.",
                status_code=PROVIDER_AUTH_REJECTED_STATUS,
            ) from exc
        except (OSError, ssl.SSLError) as exc:
            raise AppError(
                "provider_error",
                "Could not connect to the IMAP server.",
                status_code=502,
            ) from exc
        try:
            session.select_inbox()
        except imaplib.IMAP4.error as exc:
            raise AppError(
                "provider_error", "The IMAP INBOX could not be opened.", status_code=502
            ) from exc
        finally:
            session.close()

    def sync_inbound_messages(
        self,
        credential: Mapping[str, Any],
        cursor: str | None = None,
        page_size: int = 50,
    ) -> SyncPageResult:
        """Read new INBOX messages over IMAP (requires IMAP settings)."""
        if not imap_configured(credential):
            raise UnsupportedCapabilityError("SMTP", ProviderCapability.REPLY_SYNC)
        session = self._imap_connector(imap_credential(credential))
        try:
            return sync_imap_page(
                session,
                cursor,
                page_size=min(max(page_size, 1), 100),
                horizon_days=Settings.current().reply_sync_initial_horizon_days,
            )
        finally:
            session.close()
