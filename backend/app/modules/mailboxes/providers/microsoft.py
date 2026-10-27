from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from app.core.config import Settings
from app.core.errors import PROVIDER_AUTH_REJECTED_STATUS, AppError
from app.modules.mailboxes.providers.base import (
    ClassifiedProviderError,
    ConnectionValidationResult,
    EmailProvider,
    EnvelopeAttachment,
    ErrorCategory,
    OutboundMessageEnvelope,
    ProviderAccountIdentity,
    ProviderCapability,
    ProviderInboundMessage,
    ProviderSendResult,
    SyncPageResult,
    TokenExchangeResult,
    TokenRefreshResult,
    UnsupportedCapabilityError,
)
from app.modules.mailboxes.providers.message_builder import validate_header_value

GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
GRAPH_INBOX_DELTA_URL = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages/delta"
)
GRAPH_MESSAGE_SELECT = (
    "id,conversationId,internetMessageId,subject,from,toRecipients,ccRecipients,"
    "bccRecipients,receivedDateTime,sentDateTime,body,hasAttachments"
)

logger = logging.getLogger(__name__)

MAIL_SEND_SCOPE = "https://graph.microsoft.com/Mail.Send"
# Mail.ReadWrite (not Mail.Read): sending creates a draft first so Graph
# reveals the Message-ID and conversation id that replies and bounces refer to,
# and the same scope covers reading the Inbox for reply synchronization.
MAIL_READWRITE_SCOPE = "https://graph.microsoft.com/Mail.ReadWrite"
# get_identity() calls Graph /me, which answers 403 to a token that carries
# only Mail.* scopes; User.Read is the minimal scope that allows it.
USER_READ_SCOPE = "https://graph.microsoft.com/User.Read"

MICROSOFT_DEFAULT_SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    USER_READ_SCOPE,
    MAIL_SEND_SCOPE,
    MAIL_READWRITE_SCOPE,
]


class _GraphRateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__("graph rate limited")
        self.retry_after = retry_after


_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=15.0)
# One request's worst case on a fresh connection (connect + write + read). /send
# is not started with less than this left before the authorization deadline.
_WORST_REQUEST_SECONDS = 5.0 + 5.0 + 10.0


def _attachment_json(attachment: EnvelopeAttachment) -> dict[str, Any]:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": attachment.filename,
        "contentType": attachment.content_type,
        "contentBytes": base64.b64encode(attachment.data).decode("ascii"),
        # Inline images are matched to the HTML by cid.
        "isInline": attachment.content_id is not None,
        **({"contentId": attachment.content_id} if attachment.content_id else {}),
    }


def _network_rejection(exc: Exception) -> ProviderSendResult:
    """No answer from Graph before /send: nothing was sent, safe to retry."""
    return ProviderSendResult(
        status="DEFINITIVELY_REJECTED",
        error_category=ErrorCategory.NETWORK_ERROR,
        error_code="network_error",
        raw_response={"detail": str(exc)},
    )


def _message_id_filter(rfc_message_id: str) -> str:
    """The OData filter matching one internetMessageId, percent-encoded by hand:
    httpx would send spaces as "+", and a Message-ID may itself contain "+"."""
    value = f"<{rfc_message_id.strip().strip('<>')}>".replace("'", "''")
    return quote(f"internetMessageId eq '{value}'", safe="")


class MicrosoftGraphProvider(EmailProvider):
    """Microsoft Graph API and OAuth 2.0 provider adapter (v2.0 endpoint)."""

    capabilities = frozenset(
        {
            ProviderCapability.OAUTH_FLOW,
            ProviderCapability.IDENTITY_DISCOVERY,
            ProviderCapability.CONNECTION_VALIDATION,
            ProviderCapability.SEND,
            ProviderCapability.CREDENTIAL_REFRESH,
            ProviderCapability.LOOKUP_MESSAGE,
            ProviderCapability.REPLY_SYNC,
        }
    )

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        authority: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings = Settings.current()
        self.client_id = client_id or settings.microsoft_client_id
        self.client_secret = client_secret or settings.microsoft_client_secret
        self.authority = authority or settings.microsoft_authority
        self._http_client = http_client
        self._owned_http_client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        if self._owned_http_client is None:
            self._owned_http_client = httpx.Client(timeout=_TIMEOUT)
        return self._owned_http_client

    @property
    def _authorize_url(self) -> str:
        return f"{self.authority}/oauth2/v2.0/authorize"

    @property
    def _token_url(self) -> str:
        return f"{self.authority}/oauth2/v2.0/token"

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
                "Microsoft client ID is not configured",
                status_code=503,
            )

        params: dict[str, str] = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(MICROSOFT_DEFAULT_SCOPES),
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        if login_hint:
            params["login_hint"] = login_hint

        return f"{self._authorize_url}?{urlencode(params)}"

    def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> TokenExchangeResult:
        if not self.client_id or not self.client_secret:
            raise AppError(
                "configuration_error",
                "Microsoft OAuth credentials are not configured",
                status_code=503,
            )

        data: dict[str, str] = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "scope": " ".join(MICROSOFT_DEFAULT_SCOPES),
        }
        if code_verifier:
            data["code_verifier"] = code_verifier

        client = self._get_client()
        try:
            resp = client.post(self._token_url, data=data)
        except httpx.TransportError as exc:
            raise AppError(
                "provider_error",
                "Failed to reach Microsoft token endpoint",
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
                "Microsoft token endpoint did not return an access token",
                status_code=502,
            )
        scopes = (
            payload.get("scope", "").split()
            if payload.get("scope")
            else MICROSOFT_DEFAULT_SCOPES
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
                "Microsoft OAuth credentials are not configured",
                status_code=503,
            )

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(MICROSOFT_DEFAULT_SCOPES),
        }

        client = self._get_client()
        try:
            resp = client.post(self._token_url, data=data)
        except httpx.TransportError as exc:
            raise AppError(
                "provider_error",
                "Failed to reach Microsoft token endpoint during refresh",
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
                "Microsoft token endpoint did not return an access token "
                "during refresh",
                status_code=502,
            )
        scopes = (
            payload.get("scope", "").split()
            if payload.get("scope")
            else MICROSOFT_DEFAULT_SCOPES
        )
        return TokenRefreshResult(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type", "Bearer"),
            expires_in=int(payload.get("expires_in", 3600)),
            granted_scopes=scopes,
        )

    def get_identity(self, access_token: str) -> ProviderAccountIdentity:
        client = self._get_client()
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            resp = client.get(GRAPH_ME_URL, headers=headers)
        except httpx.TransportError as exc:
            raise AppError(
                "provider_error",
                "Failed to reach Microsoft Graph /me endpoint",
                status_code=502,
            ) from exc

        if resp.status_code != 200:
            classified = self.classify_error(
                resp.status_code, resp.json() if resp.content else {}
            )
            raise AppError(
                "provider_error",
                f"Failed to retrieve Microsoft identity: {classified.safe_message}",
                status_code=(
                    PROVIDER_AUTH_REJECTED_STATUS
                    if classified.requires_reconnect
                    else 502
                ),
            )

        payload = resp.json()
        # Some Microsoft account types (e.g. certain personal accounts)
        # return a null "mail" field; userPrincipalName is always present
        # and is the correct fallback identity.
        email_address = payload.get("mail") or payload.get("userPrincipalName")
        if not email_address:
            raise AppError(
                "provider_error",
                "Microsoft Graph did not return a valid account identity",
                status_code=502,
            )
        account_id = payload.get("id") or email_address

        return ProviderAccountIdentity(
            provider_account_id=str(account_id),
            email_address=str(email_address).lower(),
            display_name=payload.get("displayName"),
        )

    def validate_connection(
        self, credential: Mapping[str, Any]
    ) -> ConnectionValidationResult:
        identity = self.get_identity(credential["access_token"])
        return ConnectionValidationResult(
            is_valid=True,
            email_address=identity.email_address,
            provider_account_id=identity.provider_account_id,
            scopes=MICROSOFT_DEFAULT_SCOPES,
        )

    def send_message(
        self,
        credential: Mapping[str, Any],
        envelope: OutboundMessageEnvelope,
        *,
        authorization_deadline: datetime | None = None,
    ) -> ProviderSendResult:
        access_token = credential["access_token"]

        # Header-injection guard, applied as defense-in-depth even though
        # this transport is a JSON body, not raw MIME headers -- kept
        # consistent with Gmail/SMTP so the same guarantee holds everywhere.
        validate_header_value("To", envelope.to_address)
        validate_header_value("From", envelope.from_address)
        validate_header_value("Sender Name", envelope.from_name)
        validate_header_value("Subject", envelope.subject)
        for attachment in envelope.attachments:
            validate_header_value("Attachment filename", attachment.filename)

        message: dict[str, Any] = {
            "subject": envelope.subject,
            "body": {
                "contentType": "HTML",
                "content": envelope.body_html or envelope.body_text or "",
            },
            "toRecipients": [{"emailAddress": {"address": envelope.to_address}}],
            "from": {
                "emailAddress": {
                    "address": envelope.from_address,
                    "name": envelope.from_name or envelope.from_address,
                }
            },
        }
        if envelope.attachments:
            message["attachments"] = [_attachment_json(a) for a in envelope.attachments]
        client = self._get_client()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        # Two-step send (create draft, then send it). sendMail answers 202 with
        # no body, so it never reveals the Message-ID or conversation id that
        # replies and bounces refer to; a draft does. Creating the draft sends
        # nothing, so a failure or timeout at this step is a definitive
        # non-send and safe to retry.
        # A same-thread follow-up (ADR-0021) is a reply draft made from its parent;
        # with no parent to reply to it is sent as an ordinary draft.
        thread_dropped = False
        draft: dict[str, Any] | None = None
        if envelope.in_reply_to:
            reply = self._reply_draft(client, headers, envelope)
            if isinstance(reply, ProviderSendResult):
                return reply
            thread_dropped = reply is None
            draft = reply
        if draft is None:
            try:
                create = client.post(GRAPH_MESSAGES_URL, headers=headers, json=message)
            except httpx.TransportError as exc:
                return _network_rejection(exc)
            if create.status_code != 201:
                return self._rejected_result(create)
            draft = create.json()

        draft_id = draft.get("id")
        if not draft_id:
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                error_code="draft_without_id",
            )
        rfc_message_id = draft.get("internetMessageId") or envelope.rfc_message_id
        thread_id = draft.get("conversationId")

        # Send-time budget (issue I-01): a /send that could still be running when
        # the authorization deadline passes would race recovery, which then calls
        # the attempt UNKNOWN. Not starting it is a clean non-send, retried normally.
        if (
            authorization_deadline is not None
            and (authorization_deadline - datetime.now(UTC)).total_seconds()
            < _WORST_REQUEST_SECONDS
        ):
            self._discard_draft(client, headers, str(draft_id))
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                error_code="send_deadline_budget",
            )

        try:
            resp = client.post(f"{GRAPH_MESSAGES_URL}/{draft_id}/send", headers=headers)
        except httpx.TimeoutException:
            # Ambiguous: the send may have been accepted remotely before the
            # connection dropped. The draft is deliberately left in place.
            return ProviderSendResult(
                status="UNKNOWN",
                error_category=ErrorCategory.UNKNOWN_OUTCOME,
                error_code="request_timeout",
                provider_message_id=str(draft_id),
                provider_thread_id=thread_id,
                rfc_message_id=rfc_message_id,
                thread_dropped=thread_dropped,
            )
        except httpx.TransportError as exc:
            self._discard_draft(client, headers, str(draft_id))
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=ErrorCategory.NETWORK_ERROR,
                error_code="network_error",
                raw_response={"detail": str(exc)},
            )

        if resp.status_code == 202:
            # "Accepted for processing", not "delivered".
            return ProviderSendResult(
                status="ACCEPTED",
                provider_message_id=str(draft_id),
                provider_thread_id=thread_id,
                rfc_message_id=rfc_message_id,
                accepted_at=datetime.now(UTC),
                raw_response=None,
                thread_dropped=thread_dropped,
            )

        self._discard_draft(client, headers, str(draft_id))
        return self._rejected_result(resp)

    def _discard_draft(
        self, client: httpx.Client, headers: dict[str, str], draft_id: str
    ) -> None:
        """Best-effort cleanup of a draft that was definitively not sent. Never
        raises: a leftover draft must not change the send's outcome."""
        try:
            client.delete(f"{GRAPH_MESSAGES_URL}/{draft_id}", headers=headers)
        except Exception:  # noqa: BLE001 -- cleanup only; logged, outcome unchanged
            logger.warning("graph_draft_cleanup_failed", extra={"provider": "MICROSOFT"})

    def _reply_draft(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        envelope: OutboundMessageEnvelope,
    ) -> dict[str, Any] | ProviderSendResult | None:
        """A reply draft to envelope.in_reply_to that holds exactly our snapshot
        (ADR-0021 D4): the draft, None when there is no sent parent to reply to
        (send unthreaded), or the rejection to return. Every call here only reads
        mail or writes a draft, so any failure is a definitive non-send."""
        assert envelope.in_reply_to is not None
        # The whole mailbox (the lead's reply or the user may have moved it), but a
        # draft is not a sent parent.
        try:
            found = client.get(
                f"{GRAPH_MESSAGES_URL}?$filter={_message_id_filter(envelope.in_reply_to)}"
                "&$top=5&$select=id,conversationId,isDraft",
                headers=headers,
            )
        except httpx.TransportError as exc:
            return _network_rejection(exc)
        if found.status_code != 200:
            return self._rejected_result(found)
        parent = next(
            (
                m
                for m in found.json().get("value", [])
                if m.get("id") and not m.get("isDraft")
            ),
            None,
        )
        if parent is None:
            return None

        try:
            created = client.post(
                f"{GRAPH_MESSAGES_URL}/{quote(str(parent['id']), safe='')}/createReply",
                headers=headers,
                json={},
            )
        except httpx.TransportError as exc:
            return _network_rejection(exc)
        if created.status_code == 404:
            return None  # deleted since the lookup
        if created.status_code != 201:
            return self._rejected_result(created)
        draft: dict[str, Any] = created.json()
        draft_id = draft.get("id")
        if not draft_id:
            return ProviderSendResult(
                status="DEFINITIVELY_REJECTED",
                error_category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                error_code="draft_without_id",
            )

        # createReply addresses the parent's sender (ourselves) and quotes it:
        # replace all of that with the frozen snapshot.
        failure: ProviderSendResult | None = None
        try:
            patched = client.patch(
                f"{GRAPH_MESSAGES_URL}/{draft_id}",
                headers=headers,
                json={
                    "subject": envelope.subject,
                    "body": {
                        "contentType": "HTML",
                        "content": envelope.body_html or envelope.body_text or "",
                    },
                    "toRecipients": [
                        {"emailAddress": {"address": envelope.to_address}}
                    ],
                    "ccRecipients": [],
                    "bccRecipients": [],
                    "from": {
                        "emailAddress": {
                            "address": envelope.from_address,
                            "name": envelope.from_name or envelope.from_address,
                        }
                    },
                },
            )
            if patched.status_code != 200:
                failure = self._rejected_result(patched)
            for attachment in envelope.attachments:
                if failure is not None:
                    break
                uploaded = client.post(
                    f"{GRAPH_MESSAGES_URL}/{draft_id}/attachments",
                    headers=headers,
                    json=_attachment_json(attachment),
                )
                if uploaded.status_code != 201:
                    failure = self._rejected_result(uploaded)
        except httpx.TransportError as exc:
            failure = _network_rejection(exc)
        if failure is not None:
            self._discard_draft(client, headers, str(draft_id))
            return failure
        return draft

    def _rejected_result(self, resp: httpx.Response) -> ProviderSendResult:
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
        else:
            error_code = str(status_code or "")
            error_message = str(body)

        lowered_code = error_code.lower()
        lowered_message = error_message.lower()

        if status_code == 401 or "invalidauthenticationtoken" in lowered_code:
            return ClassifiedProviderError(
                category=ErrorCategory.AUTH_FAILURE,
                is_retryable=False,
                requires_reconnect=True,
                safe_message=(
                    "Microsoft authorization has expired or been revoked. "
                    "Reconnection required."
                ),
                provider_code=error_code or "invalid_token",
            )

        if status_code == 403 or "erroraccessdenied" in lowered_code:
            return ClassifiedProviderError(
                category=ErrorCategory.INSUFFICIENT_SCOPE,
                is_retryable=False,
                requires_reconnect=True,
                safe_message=(
                    "Microsoft did not grant the permissions this connection "
                    "needs. Please reconnect and approve all requested access."
                ),
                provider_code=error_code or "insufficient_scope",
            )

        if (
            status_code == 429
            or "throttl" in lowered_code
            or "throttl" in lowered_message
        ):
            return ClassifiedProviderError(
                category=ErrorCategory.RATE_LIMIT,
                is_retryable=True,
                requires_reconnect=False,
                safe_message=(
                    "Microsoft Graph rate limit exceeded. Please wait before retrying."
                ),
                provider_code="rate_limit_exceeded",
            )

        if status_code == 400 and (
            "recipient" in lowered_message or "invalidrecipients" in lowered_code
        ):
            return ClassifiedProviderError(
                category=ErrorCategory.PERMANENT_RECIPIENT_FAILURE,
                is_retryable=False,
                requires_reconnect=False,
                safe_message=(
                    "The recipient email address is invalid or was rejected "
                    "by Microsoft Graph."
                ),
                provider_code="invalid_recipient",
            )

        if status_code and status_code >= 500:
            return ClassifiedProviderError(
                category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                is_retryable=True,
                requires_reconnect=False,
                safe_message=(
                    "Microsoft Graph is temporarily unavailable. "
                    "Please try again later."
                ),
                provider_code=f"http_{status_code}",
            )

        return ClassifiedProviderError(
            category=ErrorCategory.POLICY_REJECTION,
            is_retryable=False,
            requires_reconnect=False,
            safe_message=(
                "Microsoft Graph rejected the request due to a policy restriction."
            ),
            provider_code=error_code or "unknown",
        )

    def revoke_token(self, token: str) -> bool:
        # Microsoft's v2.0 endpoint exposes no refresh-token revocation
        # API analogous to Google's /revoke -- there is nothing to call.
        # TOKEN_REVOCATION is deliberately excluded from `capabilities`;
        # raising here (rather than silently returning False) keeps that
        # fact explicit. The existing disconnect flow already wraps this
        # call in a broad `except Exception: pass` (remote revocation
        # failure must never block a local disconnect), so this is safe.
        raise UnsupportedCapabilityError(
            "MICROSOFT", ProviderCapability.TOKEN_REVOCATION
        )

    def lookup_message(
        self,
        credential: Mapping[str, Any],
        rfc_message_id: str,
    ) -> ProviderSendResult | None:
        """Query Microsoft Graph API to check if an email with the given
        rfc_message_id exists in the user's mailbox."""
        access_token = credential.get("access_token")
        if not access_token or not rfc_message_id:
            return None

        client = self._get_client()
        headers = {"Authorization": f"Bearer {access_token}"}
        # Graph supports filtering messages by internetMessageId
        clean_id = rfc_message_id.strip("<>")
        query = f"internetMessageId eq '<{clean_id}>'"
        try:
            # Sent Items only: /me/messages also searches Drafts, and a draft that
            # was created but never sent must not be mistaken for an accepted send.
            resp = client.get(
                "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages",
                headers=headers,
                params={"$filter": query, "$top": 1, "$select": "id,conversationId"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            messages = data.get("value", [])
            if not messages:
                return None
            found_msg = messages[0]
            return ProviderSendResult(
                status="ACCEPTED",
                provider_message_id=found_msg.get("id"),
                provider_thread_id=found_msg.get("conversationId"),
                accepted_at=datetime.now(UTC),
                raw_response=found_msg,
            )
        except Exception:
            return None

    def _validate_graph_url(self, url: str) -> None:
        """Validate opaque continuation/delta URL against SSRF."""
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise AppError("ssrf_rejected", f"Invalid URL scheme: {parsed.scheme}", status_code=400)
        if parsed.netloc.lower() != "graph.microsoft.com":
            raise AppError("ssrf_rejected", f"Untrusted host in continuation URL: {parsed.netloc}", status_code=400)
        if not parsed.path.startswith("/v1.0/"):
            raise AppError("ssrf_rejected", f"Unexpected path in continuation URL: {parsed.path}", status_code=400)

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
                "Missing access token for Microsoft sync",
                status_code=PROVIDER_AUTH_REJECTED_STATUS,
            )

        client = self._get_client()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": f"odata.maxpagesize={min(max(page_size, 1), 100)}",
        }

        try:
            return self._sync_page(client, headers, cursor)
        except httpx.TimeoutException:
            return SyncPageResult(messages=[], has_more=True, retry_after_seconds=30.0)
        except _GraphRateLimited as limited:
            return SyncPageResult(
                messages=[], has_more=True, retry_after_seconds=limited.retry_after
            )

    def _initial_delta_request(self) -> tuple[str, dict[str, str]]:
        horizon_days = Settings.current().reply_sync_initial_horizon_days
        since = (datetime.now(UTC) - timedelta(days=horizon_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return GRAPH_INBOX_DELTA_URL, {
            "$select": GRAPH_MESSAGE_SELECT,
            "$filter": f"receivedDateTime ge {since}",
            "$orderby": "receivedDateTime desc",
        }

    def _sync_page(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        cursor: str | None,
    ) -> SyncPageResult:
        params: dict[str, str] | None
        if cursor:
            self._validate_graph_url(cursor)
            target_url, params = cursor, None
        else:
            target_url, params = self._initial_delta_request()

        resp = client.get(target_url, headers=headers, params=params)
        if resp.status_code == 400 and not cursor:
            # The bounded-horizon filter is an optimisation; if Graph rejects
            # the filter/orderby combination fall back to the plain delta
            # (bounded per run by max_pages) rather than failing the mailbox.
            logger.warning("graph_delta_filter_rejected", extra={"provider": "MICROSOFT"})
            resp = client.get(
                GRAPH_INBOX_DELTA_URL,
                headers=headers,
                params={"$select": GRAPH_MESSAGE_SELECT},
            )

        if resp.status_code == 410:
            return SyncPageResult(messages=[], resync_required=True)
        if resp.status_code == 429:
            raise _GraphRateLimited(float(resp.headers.get("Retry-After", 30)))
        if resp.status_code != 200:
            try:
                err_data = resp.json().get("error", {})
                if err_data.get("code") in ("resyncRequired", "ResyncRequired"):
                    return SyncPageResult(messages=[], resync_required=True)
            except ValueError:
                pass
            classified = self.classify_error(resp.status_code)
            raise AppError(
                classified.category.value.lower(),
                classified.safe_message,
                status_code=resp.status_code,
            )

        data = resp.json()
        next_link = data.get("@odata.nextLink")
        delta_link = data.get("@odata.deltaLink")

        # Header lookups use their own headers: the paging Prefer must not
        # apply to single-message reads.
        read_headers = {"Authorization": headers["Authorization"]}
        parsed_messages: list[ProviderInboundMessage] = []
        for msg in data.get("value", []):
            if "@removed" in msg:
                continue
            self._hydrate_internet_headers(client, read_headers, msg)
            parsed = self._parse_graph_message(msg)
            if parsed:
                parsed_messages.append(parsed)

        return SyncPageResult(
            messages=parsed_messages,
            next_cursor=next_link or delta_link,
            has_more=bool(next_link),
            resync_required=False,
            synced_checkpoint=delta_link if delta_link else None,
        )

    def _hydrate_internet_headers(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        msg: dict[str, Any],
    ) -> None:
        """Attach In-Reply-To/References headers to a delta message.

        Graph's message resource has no In-Reply-To property and delta
        responses do not reliably include internetMessageHeaders, so they are
        read per message. A failure is not fatal: matching then falls back to
        the conversation id, which Graph always returns.
        """
        if msg.get("internetMessageHeaders") or not msg.get("id"):
            return
        resp = client.get(
            f"{GRAPH_MESSAGES_URL}/{msg['id']}",
            headers=headers,
            params={"$select": "internetMessageHeaders"},
        )
        if resp.status_code == 429:
            raise _GraphRateLimited(float(resp.headers.get("Retry-After", 30)))
        if resp.status_code == 200:
            msg["internetMessageHeaders"] = resp.json().get("internetMessageHeaders", [])
        elif resp.status_code != 404:
            logger.warning(
                "graph_header_lookup_failed",
                extra={"provider": "MICROSOFT", "status_code": resp.status_code},
            )

    def _parse_graph_message(self, msg: dict[str, Any]) -> ProviderInboundMessage | None:
        mid = msg.get("id")
        if not mid:
            return None

        thread_id = msg.get("conversationId")
        rfc_id = msg.get("internetMessageId")
        subject = msg.get("subject") or ""

        from_dict = msg.get("from", {}).get("emailAddress", {})
        from_addr = from_dict.get("address", "")
        from_name = from_dict.get("name")

        to_addrs = [
            r.get("emailAddress", {}).get("address", "")
            for r in msg.get("toRecipients", [])
            if r.get("emailAddress", {}).get("address")
        ]
        cc_addrs = [
            r.get("emailAddress", {}).get("address", "")
            for r in msg.get("ccRecipients", [])
            if r.get("emailAddress", {}).get("address")
        ]
        bcc_addrs = [
            r.get("emailAddress", {}).get("address", "")
            for r in msg.get("bccRecipients", [])
            if r.get("emailAddress", {}).get("address")
        ]

        received_at = datetime.now(UTC)
        received_str = msg.get("receivedDateTime")
        if received_str:
            try:
                if received_str.endswith("Z"):
                    received_str = received_str[:-1] + "+00:00"
                received_at = datetime.fromisoformat(received_str)
            except Exception:
                pass

        body_obj = msg.get("body", {})
        content_type = (body_obj.get("contentType") or "").lower()
        content_val = body_obj.get("content")
        body_text = None
        body_html = None
        if "html" in content_type:
            body_html = content_val
        else:
            body_text = content_val

        headers: dict[str, str] = {}
        for h in msg.get("internetMessageHeaders", []):
            name = (h.get("name") or "").strip()
            val = (h.get("value") or "").strip()
            if name:
                headers[name.lower()] = val
                headers[name] = val

        in_reply_to = headers.get("in-reply-to")
        references_str = headers.get("references", "")
        references = references_str.split() if references_str else []

        if not in_reply_to or not references:
            for prop in msg.get("singleValueExtendedProperties", []):
                prop_id = prop.get("id", "")
                if not in_reply_to and ("0x1042" in prop_id or "InReplyTo" in prop_id):
                    in_reply_to = prop.get("value")
                if not references and ("0x1039" in prop_id or "References" in prop_id):
                    ref_val = prop.get("value", "")
                    if ref_val:
                        references = ref_val.split()

        auto_sub = headers.get("auto-submitted", "").lower()
        precedence = headers.get("precedence", "").lower()
        x_autoreply = headers.get("x-autoreply", "").lower()
        is_automated = bool(
            (auto_sub and auto_sub != "no")
            or precedence in ("bulk", "junk", "auto_reply")
            or x_autoreply in ("yes", "true")
        )

        return ProviderInboundMessage(
            provider_message_id=str(mid),
            provider_thread_id=str(thread_id) if thread_id else None,
            rfc_message_id=str(rfc_id) if rfc_id else None,
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
