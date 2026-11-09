from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import httpx

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
    ProviderInboundMessage,
    ProviderSendResult,
    SyncPageResult,
    TokenExchangeResult,
    TokenRefreshResult,
)
from app.modules.mailboxes.providers.message_builder import (
    build_rfc5322_message,
    validate_header_value,
)
from app.modules.mailboxes.providers.mime_report import (
    extract_report_text,
    is_delivery_report,
    parse_mime_bytes,
)

GMAIL_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Google's OIDC userinfo endpoint, used for identity so connecting does not
# depend on a Gmail API round trip; the Gmail read scope below is needed only
# for reply/bounce synchronization.
GMAIL_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

GMAIL_DEFAULT_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    GMAIL_SEND_SCOPE,
    GMAIL_READ_SCOPE,
]

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class _GmailRateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__("gmail rate limited")
        self.retry_after = retry_after


def _looks_like_delivery_report(message: ProviderInboundMessage) -> bool:
    sender = (message.from_address or "").lower()
    content_type = message.headers.get("content-type", "").lower()
    return "multipart/report" in content_type or sender.startswith(
        ("mailer-daemon@", "postmaster@")
    )


def _thread_not_found(resp: httpx.Response) -> bool:
    """Gmail refused the send because of its threadId. [inferred] Google does not
    document this error: messages.send answers 404 "Requested entity was not
    found." for a thread that does not exist (nothing else in the request names
    an entity), and 400 "Invalid thread_id value" for a malformed one. Any other
    rejection is classified as usual and not retried."""
    if resp.status_code == 404:
        return True
    if resp.status_code != 400:
        return False
    try:
        error = resp.json().get("error", {})
    except ValueError:
        return False
    message = str(error.get("message", "")) if isinstance(error, dict) else ""
    return "thread" in message.lower()


class GmailProvider(EmailProvider):
    """Authoritative Gmail API and OAuth 2.0 provider adapter."""

    capabilities = frozenset(
        {
            ProviderCapability.OAUTH_FLOW,
            ProviderCapability.IDENTITY_DISCOVERY,
            ProviderCapability.CONNECTION_VALIDATION,
            ProviderCapability.SEND,
            ProviderCapability.CREDENTIAL_REFRESH,
            ProviderCapability.TOKEN_REVOCATION,
            ProviderCapability.LOOKUP_MESSAGE,
            ProviderCapability.REPLY_SYNC,
        }
    )

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings = Settings.current()
        self.client_id = client_id or settings.google_client_id
        self.client_secret = client_secret or settings.google_client_secret
        self._http_client = http_client
        self._owned_http_client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        if self._owned_http_client is None:
            self._owned_http_client = httpx.Client(
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=15.0)
            )
        return self._owned_http_client

    def get_authorization_url(
        self,
        state: str,
        redirect_uri: str,
        login_hint: str | None = None,
        code_challenge: str | None = None,
    ) -> str:
        if not self.client_id:
            raise AppError(
                "configuration_error",
                "Gmail client ID is not configured",
                status_code=503,
            )

        params: dict[str, str] = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GMAIL_DEFAULT_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        if login_hint:
            params["login_hint"] = login_hint

        return f"{GMAIL_AUTH_URL}?{urlencode(params)}"

    def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> TokenExchangeResult:
        if not self.client_id or not self.client_secret:
            raise AppError(
                "configuration_error",
                "Gmail OAuth credentials are not configured",
                status_code=503,
            )

        data: dict[str, str] = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            data["code_verifier"] = code_verifier

        client = self._get_client()
        try:
            resp = client.post(GMAIL_TOKEN_URL, data=data)
        except httpx.TransportError as exc:
            raise AppError(
                "provider_error",
                "Failed to reach Google token endpoint",
                status_code=502,
            ) from exc

        if resp.status_code != 200:
            classified = self.classify_error(
                resp.status_code, resp.json() if resp.content else {}
            )
            raise AppError(
                "provider_error",
                classified.safe_message,
                status_code=400 if resp.status_code < 500 else 502,
            )

        payload = resp.json()
        if not payload.get("access_token"):
            raise AppError(
                "provider_error",
                "Google token endpoint did not return an access token",
                status_code=502,
            )
        scopes = (
            payload.get("scope", "").split()
            if payload.get("scope")
            else GMAIL_DEFAULT_SCOPES
        )
        return TokenExchangeResult(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type", "Bearer"),
            expires_in=int(payload.get("expires_in", 3600)),
            granted_scopes=scopes,
            id_token=payload.get("id_token"),
        )

    def refresh_token(self, refresh_token: str) -> TokenRefreshResult:
        if not self.client_id or not self.client_secret:
            raise AppError(
                "configuration_error",
                "Gmail OAuth credentials are not configured",
                status_code=503,
            )

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        client = self._get_client()
        try:
            resp = client.post(GMAIL_TOKEN_URL, data=data)
        except httpx.TransportError as exc:
            raise AppError(
                "provider_error",
                "Failed to reach Google token endpoint during refresh",
                status_code=502,
            ) from exc

        if resp.status_code != 200:
            error_data = resp.json() if resp.content else {}
            classified = self.classify_error(resp.status_code, error_data)
            raise AppError(
                "auth_failure" if classified.requires_reconnect else "provider_error",
                classified.safe_message,
                status_code=(
                    PROVIDER_AUTH_REJECTED_STATUS
                    if classified.requires_reconnect
                    else 502
                ),
            )

        payload = resp.json()
        if not payload.get("access_token"):
            raise AppError(
                "provider_error",
                "Google token endpoint did not return an access token during refresh",
                status_code=502,
            )
        scopes = (
            payload.get("scope", "").split()
            if payload.get("scope")
            else GMAIL_DEFAULT_SCOPES
        )
        return TokenRefreshResult(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),  # Google may omit on refresh
            token_type=payload.get("token_type", "Bearer"),
            expires_in=int(payload.get("expires_in", 3600)),
            granted_scopes=scopes,
        )

    def get_identity(self, access_token: str) -> ProviderAccountIdentity:
        client = self._get_client()
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            resp = client.get(GMAIL_USERINFO_URL, headers=headers)
        except httpx.TransportError as exc:
            raise AppError(
                "provider_error",
                "Failed to reach Google userinfo endpoint",
                status_code=502,
            ) from exc

        if resp.status_code != 200:
            classified = self.classify_error(
                resp.status_code, resp.json() if resp.content else {}
            )
            raise AppError(
                "provider_error",
                f"Failed to retrieve Gmail identity: {classified.safe_message}",
                status_code=(
                    PROVIDER_AUTH_REJECTED_STATUS
                    if classified.requires_reconnect
                    else 502
                ),
            )

        payload = resp.json()
        email_address = payload.get("email")
        if not email_address:
            raise AppError(
                "provider_error",
                "Google userinfo did not return a valid email address",
                status_code=502,
            )

        return ProviderAccountIdentity(
            provider_account_id=email_address.lower(),
            email_address=email_address.lower(),
        )

    def validate_connection(
        self, credential: Mapping[str, Any]
    ) -> ConnectionValidationResult:
        identity = self.get_identity(credential["access_token"])
        return ConnectionValidationResult(
            is_valid=True,
            email_address=identity.email_address,
            provider_account_id=identity.provider_account_id,
            scopes=GMAIL_DEFAULT_SCOPES,
        )

    def send_message(
        self,
        credential: Mapping[str, Any],
        envelope: OutboundMessageEnvelope,
        *,
        # Only Graph uses it (a send-time budget); the others ignore it.
        authorization_deadline: datetime | None = None,
    ) -> ProviderSendResult:
        access_token = credential["access_token"]

        # 1. Header injection validation
        validate_header_value("To", envelope.to_address)
        validate_header_value("From", envelope.from_address)
        validate_header_value("Sender Name", envelope.from_name)
        validate_header_value("Subject", envelope.subject)

        # 2. Build RFC 5322 MIME message
        msg = build_rfc5322_message(envelope)

        raw_bytes = msg.as_bytes()
        encoded_raw = base64.urlsafe_b64encode(raw_bytes).decode("ascii")

        # 3. Submit to Gmail API
        client = self._get_client()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        body: dict[str, str] = {"raw": encoded_raw}
        if envelope.provider_thread_id:
            body["threadId"] = envelope.provider_thread_id

        thread_dropped = False
        try:
            resp = client.post(GMAIL_SEND_URL, headers=headers, json=body)
            if "threadId" in body and _thread_not_found(resp):
                # The reply's thread is gone (deleted by the user, or never ours).
                # This answer proves nothing was sent, so send it once outside the
                # thread; the reply headers still thread it for the recipient.
                thread_dropped = True
                resp = client.post(
                    GMAIL_SEND_URL, headers=headers, json={"raw": encoded_raw}
                )
        except httpx.TimeoutException:
            # Ambiguous: submission may have succeeded remotely before
            # the connection dropped
            return ProviderSendResult(
                status="UNKNOWN",
                error_category=ErrorCategory.UNKNOWN_OUTCOME,
                error_code="request_timeout",
            )
        except httpx.TransportError as exc:
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=ErrorCategory.NETWORK_ERROR,
                error_code="network_error",
                raw_response={"detail": str(exc)},
            )

        if resp.status_code == 200:
            data = resp.json()
            return ProviderSendResult(
                status="ACCEPTED",
                provider_message_id=data.get("id"),
                provider_thread_id=data.get("threadId"),
                rfc_message_id=self._read_sent_message_id(
                    client, headers, data.get("id")
                )
                or envelope.rfc_message_id,
                accepted_at=datetime.now(UTC),
                raw_response=data,
                thread_dropped=thread_dropped,
            )

        # Non-200 response
        error_payload = resp.json() if resp.content else {}
        classified = self.classify_error(resp.status_code, error_payload)

        retry_after_seconds: float | None = None
        raw_retry_after = resp.headers.get("Retry-After")
        if raw_retry_after:
            try:
                retry_after_seconds = float(raw_retry_after)
            except ValueError:
                retry_after_seconds = None

        return ProviderSendResult(
            status="DEFINITIVELY_REJECTED",
            error_category=classified.category,
            error_code=classified.provider_code or str(resp.status_code),
            raw_response=error_payload,
            retry_after_seconds=retry_after_seconds,
        )

    def classify_error(
        self,
        error: Exception | int | dict[str, Any],
        details: dict[str, Any] | None = None,
    ) -> ClassifiedProviderError:
        status_code = error if isinstance(error, int) else None
        body = details or (error if isinstance(error, dict) else {})

        error_val = body.get("error") if isinstance(body, dict) else None
        if isinstance(error_val, dict):
            error_code = str(error_val.get("code", status_code or ""))
            error_message = str(error_val.get("message", ""))
        elif isinstance(error_val, str):
            error_code = error_val
            error_message = str(body.get("error_description", error_val))
        else:
            error_code = str(status_code or "")
            error_message = str(body)

        # Invalid grant / authorization revoked
        if (
            status_code == 401
            or "invalid_grant" in str(body).lower()
            or "token has been expired or revoked" in error_message.lower()
        ):
            return ClassifiedProviderError(
                category=ErrorCategory.AUTH_FAILURE,
                is_retryable=False,
                requires_reconnect=True,
                safe_message=(
                    "Gmail authorization has expired or been revoked. "
                    "Reconnection required."
                ),
                provider_code="invalid_grant",
            )

        # Insufficient scope / permission denied for the requested API
        if status_code == 403:
            return ClassifiedProviderError(
                category=ErrorCategory.INSUFFICIENT_SCOPE,
                is_retryable=False,
                requires_reconnect=True,
                safe_message=(
                    "Google did not grant the permissions this connection needs. "
                    "Please reconnect and approve all requested access."
                ),
                provider_code=error_code or "insufficient_scope",
            )

        # Rate limit / Quota exceeded
        if (
            status_code == 429
            or "ratelimit" in error_message.lower()
            or "quota" in error_message.lower()
            or "userRateLimitExceeded" in str(body)
        ):
            return ClassifiedProviderError(
                category=ErrorCategory.RATE_LIMIT,
                is_retryable=True,
                requires_reconnect=False,
                safe_message=(
                    "Gmail API rate limit exceeded. Please wait before retrying."
                ),
                provider_code="rate_limit_exceeded",
            )

        # Permanent recipient / invalid argument
        if status_code == 400 and (
            "invalid argument" in error_message.lower()
            or "recipient" in error_message.lower()
        ):
            return ClassifiedProviderError(
                category=ErrorCategory.PERMANENT_RECIPIENT_FAILURE,
                is_retryable=False,
                requires_reconnect=False,
                safe_message=(
                    "The recipient email address is invalid or was rejected by Gmail."
                ),
                provider_code="invalid_recipient",
            )

        # Server-side temporary errors (5xx)
        if status_code and status_code >= 500:
            return ClassifiedProviderError(
                category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                is_retryable=True,
                requires_reconnect=False,
                safe_message=(
                    "Google service is temporarily unavailable. "
                    "Please try again later."
                ),
                provider_code=f"http_{status_code}",
            )

        return ClassifiedProviderError(
            category=ErrorCategory.POLICY_REJECTION,
            is_retryable=False,
            requires_reconnect=False,
            safe_message=("Google rejected the request due to a policy restriction."),
            provider_code=error_code or "unknown",
        )

    def revoke_token(self, token: str) -> bool:
        client = self._get_client()
        try:
            resp = client.post(GMAIL_REVOKE_URL, params={"token": token})
            return resp.status_code == 200
        except Exception:
            return False

    def lookup_message(
        self,
        credential: Mapping[str, Any],
        rfc_message_id: str,
    ) -> ProviderSendResult | None:
        """Query Gmail messages API to check if an email with the given
        rfc_message_id exists in the user's account."""
        access_token = credential.get("access_token")
        if not access_token or not rfc_message_id:
            return None

        client = self._get_client()
        headers = {"Authorization": f"Bearer {access_token}"}
        query = f"rfc822msgid:{rfc_message_id.strip('<>')}"
        try:
            resp = client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers=headers,
                params={"q": query, "maxResults": 1},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            messages = data.get("messages", [])
            if not messages:
                return None
            found_msg = messages[0]
            return ProviderSendResult(
                status="ACCEPTED",
                provider_message_id=found_msg.get("id"),
                provider_thread_id=found_msg.get("threadId"),
                accepted_at=datetime.now(UTC),
                raw_response=found_msg,
            )
        except Exception:
            return None

    def _read_sent_message_id(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        gmail_message_id: str | None,
    ) -> str | None:
        """Read the Message-ID Gmail stored for a message we just sent.

        Best effort: the send already succeeded, so a failure here (for
        example a mailbox connected before the read scope existed) falls back
        to the Message-ID we generated instead of failing the send.
        """
        if not gmail_message_id:
            return None
        try:
            resp = client.get(
                f"{GMAIL_API_BASE}/messages/{gmail_message_id}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": "Message-ID"},
            )
            if resp.status_code != 200:
                return None
            for header in resp.json().get("payload", {}).get("headers", []):
                if str(header.get("name", "")).lower() == "message-id":
                    value = str(header.get("value", "")).strip()
                    return value or None
        except (httpx.HTTPError, ValueError):
            return None
        return None

    @staticmethod
    def _parse_cursor(cursor: str | None) -> tuple[str, str | None, str | None]:
        """Return (mode, history_id, page_token).

        mode "init": no cursor yet -> start a bounded full scan.
        mode "full": mid full scan; history_id is the mailbox position taken
                     when the scan began, page_token continues messages.list.
        mode "hist": history_id is the last processed position, page_token
                     (if set) continues the same history.list traversal.
        """
        if not cursor:
            return "init", None, None
        if cursor.startswith("{"):
            try:
                data = json.loads(cursor)
            except ValueError:
                return "init", None, None
            mode = data.get("m")
            history_id = data.get("h") or data.get("history_id")
            page_token = data.get("t") or data.get("page_token")
            if mode == "full" and history_id:
                return "full", str(history_id), page_token
            if history_id:
                return "hist", str(history_id), page_token
            return "init", None, None
        return "hist", cursor, None

    def _raise_for_sync_status(self, resp: httpx.Response) -> None:
        classified = self.classify_error(resp.status_code)
        raise AppError(
            classified.category.value.lower(),
            classified.safe_message,
            status_code=resp.status_code,
        )

    def sync_inbound_messages(
        self,
        credential: Mapping[str, Any],
        cursor: str | None = None,
        page_size: int = 50,
    ) -> SyncPageResult:
        access_token = credential.get("access_token")
        if not access_token:
            raise AppError(
                "auth_failure",
                "Missing access token for Gmail sync",
                status_code=PROVIDER_AUTH_REJECTED_STATUS,
            )

        client = self._get_client()
        headers = {"Authorization": f"Bearer {access_token}"}
        limit = min(max(page_size, 1), 100)
        mode, history_id, page_token = self._parse_cursor(cursor)

        try:
            if mode == "hist":
                return self._sync_history_page(
                    client, headers, str(history_id), page_token, limit
                )
            return self._sync_full_page(
                client, headers, history_id if mode == "full" else None, page_token, limit
            )
        except httpx.TimeoutException:
            return SyncPageResult(messages=[], has_more=True, retry_after_seconds=30.0)
        except _GmailRateLimited as limited:
            return SyncPageResult(
                messages=[], has_more=True, retry_after_seconds=limited.retry_after
            )

    def _sync_history_page(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        start_history_id: str,
        page_token: str | None,
        limit: int,
    ) -> SyncPageResult:
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "maxResults": limit,
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
        }
        if page_token:
            params["pageToken"] = page_token
        resp = client.get(f"{GMAIL_API_BASE}/history", headers=headers, params=params)

        if resp.status_code == 404:
            # startHistoryId is older than Gmail retains -> full resync
            return SyncPageResult(messages=[], resync_required=True)
        if resp.status_code == 429:
            raise _GmailRateLimited(float(resp.headers.get("Retry-After", 30)))
        if resp.status_code != 200:
            self._raise_for_sync_status(resp)

        data = resp.json()
        current_history_id = str(data.get("historyId", start_history_id))
        next_page_token = data.get("nextPageToken")

        msg_ids: list[tuple[str, str | None]] = []
        for record in data.get("history", []):
            for added in record.get("messagesAdded", []):
                message = added.get("message", {})
                if message.get("id"):
                    msg_ids.append((message["id"], message.get("threadId")))

        inbound = self._fetch_gmail_messages(client, headers, msg_ids)

        if next_page_token:
            # Keep the ORIGINAL startHistoryId while a traversal is in flight;
            # only the last page advances the position.
            next_cursor = json.dumps(
                {"m": "hist", "h": start_history_id, "t": next_page_token}
            )
        else:
            next_cursor = current_history_id
        return SyncPageResult(
            messages=inbound,
            next_cursor=next_cursor,
            next_page_token=next_page_token,
            has_more=bool(next_page_token),
            synced_checkpoint=current_history_id,
        )

    def _sync_full_page(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        scan_history_id: str | None,
        page_token: str | None,
        limit: int,
    ) -> SyncPageResult:
        if scan_history_id is None:
            # Capture the mailbox position BEFORE listing so anything arriving
            # while the scan runs is picked up by the history pass afterwards.
            profile = client.get(f"{GMAIL_API_BASE}/profile", headers=headers)
            if profile.status_code == 429:
                raise _GmailRateLimited(float(profile.headers.get("Retry-After", 30)))
            if profile.status_code != 200:
                self._raise_for_sync_status(profile)
            scan_history_id = str(profile.json().get("historyId", ""))
            if not scan_history_id:
                raise AppError(
                    "provider_error",
                    "Gmail did not return a history position",
                    status_code=502,
                )

        horizon_days = Settings.current().reply_sync_initial_horizon_days
        list_params: dict[str, Any] = {
            "maxResults": limit,
            "labelIds": "INBOX",
            "q": f"newer_than:{horizon_days}d",
        }
        if page_token:
            list_params["pageToken"] = page_token
        resp = client.get(f"{GMAIL_API_BASE}/messages", headers=headers, params=list_params)
        if resp.status_code == 429:
            raise _GmailRateLimited(float(resp.headers.get("Retry-After", 30)))
        if resp.status_code != 200:
            self._raise_for_sync_status(resp)

        data = resp.json()
        next_page_token = data.get("nextPageToken")
        msg_ids = [(m["id"], m.get("threadId")) for m in data.get("messages", []) if "id" in m]
        inbound = self._fetch_gmail_messages(client, headers, msg_ids)

        if next_page_token:
            next_cursor = json.dumps(
                {"m": "full", "h": scan_history_id, "t": next_page_token}
            )
        else:
            next_cursor = scan_history_id
        return SyncPageResult(
            messages=inbound,
            next_cursor=next_cursor,
            next_page_token=next_page_token,
            has_more=bool(next_page_token),
            synced_checkpoint=scan_history_id,
        )

    def _fetch_gmail_messages(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        msg_ids: list[tuple[str, str | None]],
    ) -> list[ProviderInboundMessage]:
        """Fetch full messages. A failure other than "message no longer exists"
        aborts the page so the checkpoint does not advance past a reply that
        was never read (it is retried on the next run)."""
        inbound_msgs: list[ProviderInboundMessage] = []
        for mid, thread_id in msg_ids:
            resp = client.get(
                f"{GMAIL_API_BASE}/messages/{mid}",
                headers=headers,
                params={"format": "full"},
            )
            if resp.status_code == 404:
                continue  # deleted between history and fetch
            if resp.status_code == 429:
                raise _GmailRateLimited(float(resp.headers.get("Retry-After", 30)))
            if resp.status_code != 200:
                self._raise_for_sync_status(resp)
            m_data = resp.json()
            labels = set(m_data.get("labelIds") or [])
            if labels & {"SENT", "DRAFT"}:
                continue  # our own copy, never an inbound reply
            parsed = self._parse_gmail_message(m_data, thread_id)
            if _looks_like_delivery_report(parsed):
                parsed = self._attach_report_text(client, headers, mid, parsed)
            inbound_msgs.append(parsed)
        return inbound_msgs

    def _attach_report_text(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        gmail_message_id: str,
        parsed: ProviderInboundMessage,
    ) -> ProviderInboundMessage:
        """Delivery reports carry the bounced recipient and original Message-ID
        in MIME parts the "full" representation does not reliably expose, so the
        raw message is parsed for those."""
        resp = client.get(
            f"{GMAIL_API_BASE}/messages/{gmail_message_id}",
            headers=headers,
            params={"format": "raw"},
        )
        if resp.status_code != 200:
            return parsed
        raw = resp.json().get("raw")
        if not raw:
            return parsed
        try:
            msg = parse_mime_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except (ValueError, TypeError):
            return parsed
        if not is_delivery_report(msg):
            return parsed
        return replace(parsed, report_text=extract_report_text(msg))

    def _parse_gmail_message(
        self,
        m_data: dict[str, Any],
        fallback_thread_id: str | None = None,
    ) -> ProviderInboundMessage:
        mid = str(m_data.get("id"))
        thread_id = m_data.get("threadId") or fallback_thread_id
        payload = m_data.get("payload", {})
        headers_list = payload.get("headers", [])

        headers: dict[str, str] = {}
        for h in headers_list:
            name = h.get("name", "").strip()
            val = h.get("value", "").strip()
            if name:
                headers[name.lower()] = val
                headers[name] = val

        rfc_id = headers.get("message-id")
        in_reply_to = headers.get("in-reply-to")
        references_str = headers.get("references", "")
        references = references_str.split() if references_str else []

        from_raw = headers.get("from", "")
        from_name, from_addr = parseaddr(from_raw)

        to_raw = headers.get("to", "")
        to_addrs = [addr for _, addr in getaddresses([to_raw]) if addr]

        cc_raw = headers.get("cc", "")
        cc_addrs = [addr for _, addr in getaddresses([cc_raw]) if addr]

        bcc_raw = headers.get("bcc", "")
        bcc_addrs = [addr for _, addr in getaddresses([bcc_raw]) if addr]

        subject = headers.get("subject", "")

        received_at = datetime.now(UTC)
        internal_date_str = m_data.get("internalDate")
        if internal_date_str:
            try:
                received_at = datetime.fromtimestamp(int(internal_date_str) / 1000.0, tz=UTC)
            except Exception:
                pass
        elif "date" in headers:
            try:
                received_at = parsedate_to_datetime(headers["date"])
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=UTC)
            except Exception:
                pass

        body_text, body_html = self._extract_gmail_payload(payload)

        auto_sub = headers.get("auto-submitted", "").lower()
        precedence = headers.get("precedence", "").lower()
        x_autoreply = headers.get("x-autoreply", "").lower()
        is_automated = bool(
            (auto_sub and auto_sub != "no")
            or precedence in ("bulk", "junk", "auto_reply")
            or x_autoreply in ("yes", "true")
        )

        return ProviderInboundMessage(
            provider_message_id=mid,
            provider_thread_id=thread_id,
            rfc_message_id=rfc_id,
            in_reply_to=in_reply_to,
            references=references,
            from_address=from_addr,
            from_name=from_name or None,
            to_addresses=to_addrs,
            cc_addresses=cc_addrs,
            bcc_addresses=bcc_addrs,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            received_at=received_at,
            headers=headers,
            is_automated=is_automated,
        )

    def _extract_gmail_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data")
        body_text: str | None = None
        body_html: str | None = None

        if body_data:
            try:
                decoded = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
                if "text/html" in mime_type:
                    body_html = decoded
                else:
                    body_text = decoded
            except Exception:
                pass

        for part in payload.get("parts", []):
            sub_text, sub_html = self._extract_gmail_payload(part)
            if sub_text and not body_text:
                body_text = sub_text
            if sub_html and not body_html:
                body_html = sub_html

        return body_text, body_html
