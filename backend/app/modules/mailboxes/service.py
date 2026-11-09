from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from app.core.config import Settings
from app.core.crypto import (
    decrypt_credentials,
    decrypt_verifier,
    encrypt_credentials,
    encrypt_verifier,
)
from app.core.errors import PROVIDER_AUTH_REJECTED_STATUS, AppError
from app.modules.mailboxes.providers.base import (
    EnvelopeAttachment,
    OutboundMessageEnvelope,
    ProviderCapability,
)
from app.modules.mailboxes.providers.registry import ProviderRegistry
from app.modules.mailboxes.providers.smtp import SmtpProvider
from app.modules.mailboxes.reply_capability import (
    ReplySyncStatus,
    reply_sync_status,
)
from app.modules.mailboxes.repository import MailboxRepository
from app.modules.mailboxes.schemas import (
    DisconnectResponse,
    GmailConnectCompleteResponse,
    GmailConnectStartResponse,
    MailboxDetail,
    MailboxListItem,
    MailboxTestSendResult,
    MailboxUpdate,
    MicrosoftConnectCompleteResponse,
    MicrosoftConnectStartResponse,
    SmtpConfigView,
    SmtpConnectRequest,
    SmtpConnectResponse,
    SmtpUpdateRequest,
)

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MailboxService:
    def __init__(self, repo: MailboxRepository) -> None:
        self.repo = repo

    def list_mailboxes(self, workspace_id: UUID) -> list[MailboxListItem]:
        rows = self.repo.list_mailboxes(workspace_id)
        return [
            MailboxListItem(
                id=UUID(str(r["id"])),
                provider=r["provider"],
                email_address=r["original_address"],
                sender_display_name=r["sender_display_name"],
                connection_state=r["connection_state"],
                health_state=r["health_state"],
                policy_state=r["policy_state"],
                policy_reason=r["policy_reason"],
                circuit_state=r["circuit_state"],
                sync_state=r["sync_state"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get_mailbox(self, workspace_id: UUID, mailbox_id: UUID) -> MailboxDetail:
        r = self.repo.get_mailbox(workspace_id, mailbox_id)
        if not r:
            raise AppError("not_found", "Mailbox not found", status_code=404)

        smtp_config = None
        reply_status = ReplySyncStatus.UNSUPPORTED
        if r["connected_generation"]:
            conn = self.repo.get_mailbox_connection(
                workspace_id, mailbox_id, r["connected_generation"]
            )
            if conn:
                cfg = conn.get("protected_config") or {}
                reply_status = reply_sync_status(
                    r["provider"], conn.get("granted_scopes"), cfg
                )
                if r["provider"] == "SMTP" and cfg:
                    smtp_config = SmtpConfigView(
                        host=cfg["host"],
                        port=cfg["port"],
                        security_mode=cfg["security_mode"],
                        username=cfg["username"],
                        imap_host=cfg.get("imap_host"),
                        imap_port=cfg.get("imap_port"),
                        imap_security_mode=cfg.get("imap_security_mode"),
                        imap_username=cfg.get("imap_username"),
                    )

        return MailboxDetail(
            id=UUID(str(r["id"])),
            provider=r["provider"],
            email_address=r["original_address"],
            sender_display_name=r["sender_display_name"],
            signature_html=r["signature_html"],
            connection_state=r["connection_state"],
            health_state=r["health_state"],
            policy_state=r["policy_state"],
            policy_reason=r["policy_reason"],
            circuit_state=r["circuit_state"],
            sync_state=r["sync_state"],
            blocked_until=r["blocked_until"],
            pending_safety_count=r["pending_safety_count"],
            current_connection_generation=r["current_connection_generation"],
            config_version=r["config_version"],
            version=r["version"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            smtp_config=smtp_config,
            reply_sync_status=reply_status.value,
        )

    def update_mailbox(
        self,
        workspace_id: UUID,
        mailbox_id: UUID,
        payload: MailboxUpdate,
    ) -> MailboxDetail:
        # Check existence
        existing = self.repo.get_mailbox(workspace_id, mailbox_id)
        if not existing:
            raise AppError("not_found", "Mailbox not found", status_code=404)

        self.repo.update_mailbox_metadata(
            workspace_id,
            mailbox_id,
            payload.sender_display_name,
            payload.signature_html,
        )
        return self.get_mailbox(workspace_id, mailbox_id)

    # -------------------------------------------------------------------------
    # Gmail OAuth
    # -------------------------------------------------------------------------

    def start_gmail_oauth(
        self,
        workspace_id: UUID,
        user_id: UUID,
        return_path: str = "/app/mailboxes",
        login_hint: str | None = None,
    ) -> GmailConnectStartResponse:
        # Validate return_path shape
        if (
            not return_path.startswith("/app/")
            or "\\" in return_path
            or any(ord(c) < 32 for c in return_path)
        ):
            return_path = "/app/mailboxes"

        settings = Settings.current()
        provider = ProviderRegistry.get("GMAIL")

        # 1. Generate state and PKCE verifier
        state_token = secrets.token_urlsafe(32)
        state_digest = hashlib.sha256(state_token.encode("utf-8")).hexdigest()

        code_verifier = secrets.token_urlsafe(64)
        verifier_hash = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = (
            base64.urlsafe_b64encode(verifier_hash).rstrip(b"=").decode("ascii")
        )

        # 2. Encrypt PKCE verifier
        enc_verifier, key_id, nonce = encrypt_verifier(
            code_verifier, workspace_id, user_id, "GMAIL"
        )

        # 3. Store flow record with 10-minute TTL
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=10)
        flow_id = uuid4()

        self.repo.insert_oauth_flow(
            flow_id=flow_id,
            workspace_id=workspace_id,
            actor_id=user_id,
            provider="GMAIL",
            state_digest=state_digest,
            encrypted_verifier=enc_verifier,
            verifier_key_id=key_id,
            verifier_nonce=nonce,
            return_path=return_path,
            expires_at=expires_at,
        )

        auth_url = provider.get_authorization_url(
            state=state_token,
            redirect_uri=settings.google_redirect_uri,
            login_hint=login_hint,
            code_challenge=code_challenge,
        )

        return GmailConnectStartResponse(
            authorization_url=auth_url,
            expires_at=expires_at,
        )

    def complete_gmail_oauth(
        self,
        user_id: UUID,
        code: str,
        state_token: str,
    ) -> GmailConnectCompleteResponse:
        settings = Settings.current()
        state_digest = hashlib.sha256(state_token.encode("utf-8")).hexdigest()
        claim_owner = f"user:{user_id}:{uuid4()}"

        # 1. Claim flow atomically (prevents replay). RLS on oauth_flows
        # (see oauth_flows_connection_update in supabase/migrations) already
        # requires workspace_id = app_current_workspace_id(), which is bound
        # from the URL's workspace_id by get_workspace_context/require_permission
        # before this runs — so a flow started under a different workspace than
        # the URL's simply cannot be claimed here.
        flow = self.repo.get_and_claim_oauth_flow(state_digest, claim_owner)
        if not flow:
            raise AppError(
                "invalid_oauth_state",
                "OAuth session is invalid, expired, or has already been used. "
                "Please try connecting again.",
                status_code=400,
            )

        # 2. Re-validate initiating user identity
        flow_actor_id = UUID(str(flow["actor_id"]))
        if flow_actor_id != user_id:
            self.repo.fail_oauth_flow(UUID(str(flow["id"])))
            raise AppError(
                "forbidden",
                "OAuth transaction was initiated by a different user session",
                status_code=403,
            )

        flow_workspace_id = UUID(str(flow["workspace_id"]))
        provider = ProviderRegistry.get("GMAIL")

        # 3. Decrypt verifier
        code_verifier = decrypt_verifier(
            ciphertext=flow["encrypted_verifier"],
            nonce=flow["verifier_nonce"],
            workspace_id=flow_workspace_id,
            actor_id=user_id,
            provider="GMAIL",
            key_id=flow["verifier_key_id"],
        )

        # 4. Exchange code for tokens
        try:
            tokens = provider.exchange_code(
                code=code,
                redirect_uri=settings.google_redirect_uri,
                code_verifier=code_verifier,
            )
        except AppError:
            self.repo.fail_oauth_flow(UUID(str(flow["id"])))
            raise

        # 5. Authoritatively discover identity from Google API
        try:
            identity = provider.get_identity(tokens.access_token)
        except AppError:
            self.repo.fail_oauth_flow(UUID(str(flow["id"])))
            raise

        # 6. Check global single-active-account uniqueness rule
        global_active = self.repo.get_active_mailbox_by_provider_account_global(
            "GMAIL", identity.provider_account_id
        )
        if (
            global_active
            and UUID(str(global_active["workspace_id"])) != flow_workspace_id
        ):
            self.repo.fail_oauth_flow(UUID(str(flow["id"])))
            raise AppError(
                "conflict",
                f"The Gmail account '{identity.email_address}' is already "
                "connected to another workspace.",
                status_code=409,
            )

        # 7. Check if this flow was initiated to reconnect a specific mailbox
        target_mailbox_id = None
        if flow.get("return_path", "").startswith("/app/mailboxes/"):
            path_suffix = flow["return_path"].removeprefix("/app/mailboxes/").strip()
            if path_suffix:
                try:
                    target_mailbox_id = UUID(path_suffix)
                except ValueError:
                    pass

        if target_mailbox_id:
            target_mb = self.repo.get_mailbox(flow_workspace_id, target_mailbox_id)
            if target_mb:
                if (
                    target_mb.get("provider_account_id")
                    and target_mb["provider_account_id"] != identity.provider_account_id
                ) or (
                    target_mb["original_address"].lower()
                    != identity.email_address.lower()
                ):
                    self.repo.fail_oauth_flow(UUID(str(flow["id"])))
                    raise AppError(
                        "account_mismatch",
                        f"Authenticated Google account '{identity.email_address}' "
                        f"does not match mailbox '{target_mb['original_address']}'.",
                        status_code=400,
                    )

        # 8. Check if this mailbox already exists in this workspace (Reconnect)
        existing = self.repo.get_mailbox_by_provider_account(
            flow_workspace_id, "GMAIL", identity.provider_account_id
        )

        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=tokens.expires_in)

        if existing:
            mailbox_id = UUID(str(existing["id"]))
            new_generation = existing["current_connection_generation"] + 1

            # Retain old refresh token if Google didn't return a new one
            refresh_token = tokens.refresh_token
            if not refresh_token:
                old_conn = self.repo.get_mailbox_connection(
                    flow_workspace_id,
                    mailbox_id,
                    existing["current_connection_generation"],
                )
                if old_conn and old_conn["credential_ciphertext"]:
                    old_creds = decrypt_credentials(
                        old_conn["credential_ciphertext"],
                        old_conn["nonce"],
                        flow_workspace_id,
                        mailbox_id,
                        "GMAIL",
                        old_conn["encryption_key_id"],
                    )
                    refresh_token = old_creds.get("refresh_token")

            cred_payload = {
                "access_token": tokens.access_token,
                "refresh_token": refresh_token,
                "token_type": tokens.token_type,
            }
            ciphertext, key_id, nonce = encrypt_credentials(
                cred_payload, flow_workspace_id, mailbox_id, "GMAIL"
            )

            # Insert new connection generation
            self.repo.insert_mailbox_connection(
                connection_id=uuid4(),
                workspace_id=flow_workspace_id,
                mailbox_id=mailbox_id,
                generation=new_generation,
                credential_ciphertext=ciphertext,
                encryption_key_id=key_id,
                nonce=nonce,
                auth_mechanism="OAUTH",
                granted_scopes=tokens.granted_scopes,
                expires_at=expires_at,
            )

            # Update mailbox to CONNECTED & HEALTHY
            self.repo.update_mailbox_connection_state(
                workspace_id=flow_workspace_id,
                mailbox_id=mailbox_id,
                connection_state="CONNECTED",
                health_state="HEALTHY",
                connected_generation=new_generation,
                current_connection_generation=new_generation,
                provider_account_id=identity.provider_account_id,
                original_address=identity.email_address,
            )
        else:
            # Create new mailbox
            mailbox_id = uuid4()
            generation = 1

            cred_payload = {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "token_type": tokens.token_type,
            }
            ciphertext, key_id, nonce = encrypt_credentials(
                cred_payload, flow_workspace_id, mailbox_id, "GMAIL"
            )

            # mailbox_connections_mailbox_fkey requires the mailbox row to
            # already exist, so it must be inserted first. It starts in
            # CONNECTING with connected_generation NULL (allowed by
            # mailboxes_connected_generation_check), which trivially
            # satisfies mailboxes_current_connection_fkey until the
            # connection row exists below.
            self.repo.insert_mailbox(
                mailbox_id=mailbox_id,
                workspace_id=flow_workspace_id,
                provider="GMAIL",
                provider_account_id=identity.provider_account_id,
                original_address=identity.email_address,
                connection_state="CONNECTING",
                health_state="UNKNOWN",
                generation=generation,
            )
            self.repo.ensure_default_sending_limit(
                workspace_id=flow_workspace_id, mailbox_id=mailbox_id
            )

            self.repo.insert_mailbox_connection(
                connection_id=uuid4(),
                workspace_id=flow_workspace_id,
                mailbox_id=mailbox_id,
                generation=generation,
                credential_ciphertext=ciphertext,
                encryption_key_id=key_id,
                nonce=nonce,
                auth_mechanism="OAUTH",
                granted_scopes=tokens.granted_scopes,
                expires_at=expires_at,
            )

            # Now that the connection row exists, promote the mailbox to
            # CONNECTED and point connected_generation at it.
            self.repo.update_mailbox_connection_state(
                workspace_id=flow_workspace_id,
                mailbox_id=mailbox_id,
                connection_state="CONNECTED",
                health_state="HEALTHY",
                connected_generation=generation,
                current_connection_generation=generation,
            )

        self.repo.complete_oauth_flow(UUID(str(flow["id"])), mailbox_id)

        return GmailConnectCompleteResponse(
            mailbox_id=mailbox_id,
            provider="GMAIL",
            email_address=identity.email_address,
            connection_state="CONNECTED",
            health_state="HEALTHY",
        )

    def reconnect_gmail(
        self,
        workspace_id: UUID,
        user_id: UUID,
        mailbox_id: UUID,
    ) -> GmailConnectStartResponse:
        mailbox = self.repo.get_mailbox(workspace_id, mailbox_id)
        if not mailbox:
            raise AppError("not_found", "Mailbox not found", status_code=404)

        if mailbox["provider"] != "GMAIL":
            raise AppError(
                "bad_request", "Mailbox provider is not Gmail", status_code=400
            )

        return self.start_gmail_oauth(
            workspace_id=workspace_id,
            user_id=user_id,
            return_path=f"/app/mailboxes/{mailbox_id}",
            login_hint=mailbox["original_address"],
        )

    # -------------------------------------------------------------------------
    # Microsoft OAuth
    # -------------------------------------------------------------------------
    #
    # Structurally identical to the Gmail flow above (same 8-step claim /
    # actor-check / verifier-decrypt / exchange / identity / global-
    # uniqueness / account-mismatch / insert-or-update sequence), just
    # swapping the provider name and settings field. Deliberately
    # copy-and-rename rather than factored into one shared
    # complete_oauth(provider) helper -- that would be a larger refactor
    # than this phase calls for and raises Gmail-regression risk.

    def start_microsoft_oauth(
        self,
        workspace_id: UUID,
        user_id: UUID,
        return_path: str = "/app/mailboxes",
        login_hint: str | None = None,
    ) -> MicrosoftConnectStartResponse:
        if (
            not return_path.startswith("/app/")
            or "\\" in return_path
            or any(ord(c) < 32 for c in return_path)
        ):
            return_path = "/app/mailboxes"

        settings = Settings.current()
        provider = ProviderRegistry.get("MICROSOFT")

        state_token = secrets.token_urlsafe(32)
        state_digest = hashlib.sha256(state_token.encode("utf-8")).hexdigest()

        code_verifier = secrets.token_urlsafe(64)
        verifier_hash = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = (
            base64.urlsafe_b64encode(verifier_hash).rstrip(b"=").decode("ascii")
        )

        enc_verifier, key_id, nonce = encrypt_verifier(
            code_verifier, workspace_id, user_id, "MICROSOFT"
        )

        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=10)
        flow_id = uuid4()

        self.repo.insert_oauth_flow(
            flow_id=flow_id,
            workspace_id=workspace_id,
            actor_id=user_id,
            provider="MICROSOFT",
            state_digest=state_digest,
            encrypted_verifier=enc_verifier,
            verifier_key_id=key_id,
            verifier_nonce=nonce,
            return_path=return_path,
            expires_at=expires_at,
        )

        auth_url = provider.get_authorization_url(
            state=state_token,
            redirect_uri=settings.microsoft_redirect_uri,
            login_hint=login_hint,
            code_challenge=code_challenge,
        )

        return MicrosoftConnectStartResponse(
            authorization_url=auth_url,
            expires_at=expires_at,
        )

    def complete_microsoft_oauth(
        self,
        user_id: UUID,
        code: str,
        state_token: str,
    ) -> MicrosoftConnectCompleteResponse:
        settings = Settings.current()
        state_digest = hashlib.sha256(state_token.encode("utf-8")).hexdigest()
        claim_owner = f"user:{user_id}:{uuid4()}"

        flow = self.repo.get_and_claim_oauth_flow(state_digest, claim_owner)
        if not flow:
            raise AppError(
                "invalid_oauth_state",
                "OAuth session is invalid, expired, or has already been used. "
                "Please try connecting again.",
                status_code=400,
            )

        flow_actor_id = UUID(str(flow["actor_id"]))
        if flow_actor_id != user_id:
            self.repo.fail_oauth_flow(UUID(str(flow["id"])))
            raise AppError(
                "forbidden",
                "OAuth transaction was initiated by a different user session",
                status_code=403,
            )

        flow_workspace_id = UUID(str(flow["workspace_id"]))
        provider = ProviderRegistry.get("MICROSOFT")

        code_verifier = decrypt_verifier(
            ciphertext=flow["encrypted_verifier"],
            nonce=flow["verifier_nonce"],
            workspace_id=flow_workspace_id,
            actor_id=user_id,
            provider="MICROSOFT",
            key_id=flow["verifier_key_id"],
        )

        try:
            tokens = provider.exchange_code(
                code=code,
                redirect_uri=settings.microsoft_redirect_uri,
                code_verifier=code_verifier,
            )
        except AppError:
            self.repo.fail_oauth_flow(UUID(str(flow["id"])))
            raise

        try:
            identity = provider.get_identity(tokens.access_token)
        except AppError:
            self.repo.fail_oauth_flow(UUID(str(flow["id"])))
            raise

        global_active = self.repo.get_active_mailbox_by_provider_account_global(
            "MICROSOFT", identity.provider_account_id
        )
        if (
            global_active
            and UUID(str(global_active["workspace_id"])) != flow_workspace_id
        ):
            self.repo.fail_oauth_flow(UUID(str(flow["id"])))
            raise AppError(
                "conflict",
                f"The Microsoft account '{identity.email_address}' is already "
                "connected to another workspace.",
                status_code=409,
            )

        target_mailbox_id = None
        if flow.get("return_path", "").startswith("/app/mailboxes/"):
            path_suffix = flow["return_path"].removeprefix("/app/mailboxes/").strip()
            if path_suffix:
                try:
                    target_mailbox_id = UUID(path_suffix)
                except ValueError:
                    pass

        if target_mailbox_id:
            target_mb = self.repo.get_mailbox(flow_workspace_id, target_mailbox_id)
            if target_mb:
                if (
                    target_mb.get("provider_account_id")
                    and target_mb["provider_account_id"] != identity.provider_account_id
                ) or (
                    target_mb["original_address"].lower()
                    != identity.email_address.lower()
                ):
                    self.repo.fail_oauth_flow(UUID(str(flow["id"])))
                    raise AppError(
                        "account_mismatch",
                        f"Authenticated Microsoft account '{identity.email_address}' "
                        f"does not match mailbox '{target_mb['original_address']}'.",
                        status_code=400,
                    )

        existing = self.repo.get_mailbox_by_provider_account(
            flow_workspace_id, "MICROSOFT", identity.provider_account_id
        )

        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=tokens.expires_in)

        if existing:
            mailbox_id = UUID(str(existing["id"]))
            new_generation = existing["current_connection_generation"] + 1

            refresh_token = tokens.refresh_token
            if not refresh_token:
                old_conn = self.repo.get_mailbox_connection(
                    flow_workspace_id,
                    mailbox_id,
                    existing["current_connection_generation"],
                )
                if old_conn and old_conn["credential_ciphertext"]:
                    old_creds = decrypt_credentials(
                        old_conn["credential_ciphertext"],
                        old_conn["nonce"],
                        flow_workspace_id,
                        mailbox_id,
                        "MICROSOFT",
                        old_conn["encryption_key_id"],
                    )
                    refresh_token = old_creds.get("refresh_token")

            cred_payload = {
                "access_token": tokens.access_token,
                "refresh_token": refresh_token,
                "token_type": tokens.token_type,
            }
            ciphertext, key_id, nonce = encrypt_credentials(
                cred_payload, flow_workspace_id, mailbox_id, "MICROSOFT"
            )

            self.repo.insert_mailbox_connection(
                connection_id=uuid4(),
                workspace_id=flow_workspace_id,
                mailbox_id=mailbox_id,
                generation=new_generation,
                credential_ciphertext=ciphertext,
                encryption_key_id=key_id,
                nonce=nonce,
                auth_mechanism="OAUTH",
                granted_scopes=tokens.granted_scopes,
                expires_at=expires_at,
            )

            self.repo.update_mailbox_connection_state(
                workspace_id=flow_workspace_id,
                mailbox_id=mailbox_id,
                connection_state="CONNECTED",
                health_state="HEALTHY",
                connected_generation=new_generation,
                current_connection_generation=new_generation,
                provider_account_id=identity.provider_account_id,
                original_address=identity.email_address,
            )
        else:
            mailbox_id = uuid4()
            generation = 1

            cred_payload = {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "token_type": tokens.token_type,
            }
            ciphertext, key_id, nonce = encrypt_credentials(
                cred_payload, flow_workspace_id, mailbox_id, "MICROSOFT"
            )

            self.repo.insert_mailbox(
                mailbox_id=mailbox_id,
                workspace_id=flow_workspace_id,
                provider="MICROSOFT",
                provider_account_id=identity.provider_account_id,
                original_address=identity.email_address,
                connection_state="CONNECTING",
                health_state="UNKNOWN",
                generation=generation,
            )
            self.repo.ensure_default_sending_limit(
                workspace_id=flow_workspace_id, mailbox_id=mailbox_id
            )

            self.repo.insert_mailbox_connection(
                connection_id=uuid4(),
                workspace_id=flow_workspace_id,
                mailbox_id=mailbox_id,
                generation=generation,
                credential_ciphertext=ciphertext,
                encryption_key_id=key_id,
                nonce=nonce,
                auth_mechanism="OAUTH",
                granted_scopes=tokens.granted_scopes,
                expires_at=expires_at,
            )

            self.repo.update_mailbox_connection_state(
                workspace_id=flow_workspace_id,
                mailbox_id=mailbox_id,
                connection_state="CONNECTED",
                health_state="HEALTHY",
                connected_generation=generation,
                current_connection_generation=generation,
            )

        self.repo.complete_oauth_flow(UUID(str(flow["id"])), mailbox_id)

        return MicrosoftConnectCompleteResponse(
            mailbox_id=mailbox_id,
            provider="MICROSOFT",
            email_address=identity.email_address,
            connection_state="CONNECTED",
            health_state="HEALTHY",
        )

    def reconnect_microsoft(
        self,
        workspace_id: UUID,
        user_id: UUID,
        mailbox_id: UUID,
    ) -> MicrosoftConnectStartResponse:
        mailbox = self.repo.get_mailbox(workspace_id, mailbox_id)
        if not mailbox:
            raise AppError("not_found", "Mailbox not found", status_code=404)

        if mailbox["provider"] != "MICROSOFT":
            raise AppError(
                "bad_request", "Mailbox provider is not Microsoft", status_code=400
            )

        return self.start_microsoft_oauth(
            workspace_id=workspace_id,
            user_id=user_id,
            return_path=f"/app/mailboxes/{mailbox_id}",
            login_hint=mailbox["original_address"],
        )

    # -------------------------------------------------------------------------
    # Custom SMTP
    # -------------------------------------------------------------------------

    @staticmethod
    def _resolve_imap_settings(
        payload: SmtpConnectRequest | SmtpUpdateRequest,
        existing_config: dict[str, Any],
        existing_secret: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Merge submitted IMAP settings over the stored ones.

        Returns (non-secret config, secret payload). Both are empty when IMAP
        is not configured, in which case the mailbox is send-only.
        """
        host = (
            payload.imap_host
            if payload.imap_host is not None
            else existing_config.get("imap_host")
        )
        if not host:
            return {}, {}
        port = payload.imap_port or existing_config.get("imap_port") or 993
        expected_mode = "IMPLICIT_TLS" if port == 993 else "STARTTLS"
        mode = (
            payload.imap_security_mode.value
            if payload.imap_security_mode is not None
            else existing_config.get("imap_security_mode") or expected_mode
        )
        if mode != expected_mode:
            raise AppError(
                "bad_request",
                f"IMAP port {port} requires security mode {expected_mode}",
                status_code=422,
            )
        config: dict[str, Any] = {
            "imap_host": host,
            "imap_port": port,
            "imap_security_mode": mode,
        }
        username = payload.imap_username or existing_config.get("imap_username")
        if username:
            config["imap_username"] = username
        password = payload.imap_password or existing_secret.get("imap_password")
        secret = {"imap_password": password} if password else {}
        return config, secret

    def connect_smtp_mailbox(
        self,
        workspace_id: UUID,
        user_id: UUID,
        payload: SmtpConnectRequest,
    ) -> SmtpConnectResponse:
        target_email = payload.email_address.strip().lower()
        if not _EMAIL_PATTERN.match(target_email) or any(
            c in target_email for c in ["\r", "\n"]
        ):
            raise AppError(
                "bad_request", "Invalid mailbox email address format", status_code=422
            )

        # SMTP has no external "account id" concept the way OAuth providers
        # do (no /me identity endpoint) -- synthesize a stable, deterministic
        # key from the connection coordinates so the same global
        # one-workspace-per-account uniqueness rule still applies.
        provider_account_id = (
            f"{payload.host.strip().lower()}:{payload.port}:"
            f"{payload.username.strip().lower()}"
        )

        global_active = self.repo.get_active_mailbox_by_provider_account_global(
            "SMTP", provider_account_id
        )
        if global_active and UUID(str(global_active["workspace_id"])) != workspace_id:
            raise AppError(
                "conflict",
                "This SMTP account is already connected to another workspace.",
                status_code=409,
            )

        provider = ProviderRegistry.get("SMTP")
        credential = {
            "host": payload.host,
            "port": payload.port,
            "security_mode": payload.security_mode.value,
            "username": payload.username,
            "password": payload.password,
        }
        imap_config, imap_secret = self._resolve_imap_settings(payload, {}, {})
        credential.update(imap_config)
        credential.update(imap_secret)

        # SSRF-safe connect + authenticate. Any failure here propagates as
        # an AppError and no mailbox row is ever created -- invalid
        # configuration must never produce a "healthy" mailbox.
        provider.validate_connection(credential)
        if imap_config:
            cast(SmtpProvider, provider).validate_imap(credential)

        mailbox_id = uuid4()
        generation = 1

        secret_payload = {"password": payload.password, **imap_secret}
        ciphertext, key_id, nonce = encrypt_credentials(
            secret_payload, workspace_id, mailbox_id, "SMTP"
        )
        protected_config = {
            "host": payload.host,
            "port": payload.port,
            "security_mode": payload.security_mode.value,
            "username": payload.username,
            **imap_config,
        }

        self.repo.insert_mailbox(
            mailbox_id=mailbox_id,
            workspace_id=workspace_id,
            provider="SMTP",
            provider_account_id=provider_account_id,
            original_address=target_email,
            sender_display_name=payload.sender_display_name,
            connection_state="CONNECTING",
            health_state="UNKNOWN",
            generation=generation,
        )
        self.repo.ensure_default_sending_limit(
            workspace_id=workspace_id, mailbox_id=mailbox_id
        )

        self.repo.insert_mailbox_connection(
            connection_id=uuid4(),
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            generation=generation,
            credential_ciphertext=ciphertext,
            encryption_key_id=key_id,
            nonce=nonce,
            auth_mechanism="SMTP_PASSWORD",
            protected_config=protected_config,
            expires_at=None,
        )

        self.repo.update_mailbox_connection_state(
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            connection_state="CONNECTED",
            health_state="HEALTHY",
            connected_generation=generation,
            current_connection_generation=generation,
        )

        return SmtpConnectResponse(
            mailbox_id=mailbox_id,
            provider="SMTP",
            email_address=target_email,
            connection_state="CONNECTED",
            health_state="HEALTHY",
        )

    def update_smtp_mailbox(
        self,
        workspace_id: UUID,
        user_id: UUID,
        mailbox_id: UUID,
        payload: SmtpUpdateRequest,
    ) -> MailboxDetail:
        mailbox = self.repo.get_mailbox(workspace_id, mailbox_id, for_update=True)
        if not mailbox:
            raise AppError("not_found", "Mailbox not found", status_code=404)
        if mailbox["provider"] != "SMTP":
            raise AppError(
                "bad_request", "Mailbox provider is not SMTP", status_code=400
            )

        current_gen = mailbox["current_connection_generation"]
        conn = self.repo.get_mailbox_connection(workspace_id, mailbox_id, current_gen)
        if not conn or not conn["credential_ciphertext"]:
            raise AppError(
                "conflict", "Mailbox credentials not found or revoked", status_code=409
            )

        existing_config: dict[str, Any] = dict(conn.get("protected_config") or {})
        existing_creds = decrypt_credentials(
            conn["credential_ciphertext"],
            conn["nonce"],
            workspace_id,
            mailbox_id,
            "SMTP",
            conn["encryption_key_id"],
        )

        new_host = (
            payload.host if payload.host is not None else existing_config.get("host")
        )
        new_port = (
            payload.port if payload.port is not None else existing_config.get("port")
        )
        new_security_mode = (
            payload.security_mode.value
            if payload.security_mode is not None
            else existing_config.get("security_mode")
        )
        new_username = (
            payload.username
            if payload.username is not None
            else existing_config.get("username")
        )
        # Password omitted -> keep the existing credential. Never a literal
        # placeholder string.
        new_password = (
            payload.password
            if payload.password is not None
            else existing_creds.get("password")
        )

        provider = ProviderRegistry.get("SMTP")
        candidate_credential = {
            "host": new_host,
            "port": new_port,
            "security_mode": new_security_mode,
            "username": new_username,
            "password": new_password,
        }
        imap_config, imap_secret = self._resolve_imap_settings(
            payload, existing_config, existing_creds
        )
        candidate_credential.update(imap_config)
        candidate_credential.update(imap_secret)

        # Re-validate the merged configuration BEFORE activating anything.
        # On failure, the existing working generation is left untouched
        # (fail closed, no partial update).
        provider.validate_connection(candidate_credential)
        if imap_config:
            cast(SmtpProvider, provider).validate_imap(candidate_credential)

        new_gen = current_gen + 1
        secret_payload = {"password": new_password, **imap_secret}
        ciphertext, key_id, nonce = encrypt_credentials(
            secret_payload, workspace_id, mailbox_id, "SMTP"
        )
        protected_config = {
            "host": new_host,
            "port": new_port,
            "security_mode": new_security_mode,
            "username": new_username,
            **imap_config,
        }

        self.repo.insert_mailbox_connection(
            connection_id=uuid4(),
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            generation=new_gen,
            credential_ciphertext=ciphertext,
            encryption_key_id=key_id,
            nonce=nonce,
            auth_mechanism="SMTP_PASSWORD",
            protected_config=protected_config,
            expires_at=None,
        )
        self.repo.update_mailbox_connection_state(
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            connection_state="CONNECTED",
            health_state="HEALTHY",
            connected_generation=new_gen,
            current_connection_generation=new_gen,
        )

        if payload.sender_display_name is not None:
            self.repo.update_mailbox_metadata(
                workspace_id,
                mailbox_id,
                payload.sender_display_name,
                mailbox["signature_html"],
            )

        return self.get_mailbox(workspace_id, mailbox_id)

    def disconnect_mailbox(
        self,
        workspace_id: UUID,
        user_id: UUID,
        mailbox_id: UUID,
    ) -> DisconnectResponse:
        mailbox = self.repo.get_mailbox(workspace_id, mailbox_id, for_update=True)
        if not mailbox:
            raise AppError("not_found", "Mailbox not found", status_code=404)

        if mailbox["connection_state"] == "DISCONNECTED":
            return DisconnectResponse(
                mailbox_id=mailbox_id, connection_state="DISCONNECTED"
            )

        current_gen = mailbox["current_connection_generation"]
        next_gen = current_gen + 1

        # Attempt remote token revocation if credentials exist
        conn = self.repo.get_mailbox_connection(workspace_id, mailbox_id, current_gen)
        if conn and conn["credential_ciphertext"]:
            try:
                creds = decrypt_credentials(
                    conn["credential_ciphertext"],
                    conn["nonce"],
                    workspace_id,
                    mailbox_id,
                    mailbox["provider"],
                    conn["encryption_key_id"],
                )
                refresh_token = creds.get("refresh_token") or creds.get("access_token")
                if refresh_token:
                    provider = ProviderRegistry.get(mailbox["provider"])
                    provider.revoke_token(refresh_token)
            except Exception:
                pass  # Remote revocation failure must not prevent local disconnect

        # Zero local credentials
        self.repo.destroy_mailbox_connection(workspace_id, mailbox_id, current_gen)

        # Transition mailbox to DISCONNECTED
        self.repo.update_mailbox_connection_state(
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            connection_state="DISCONNECTED",
            health_state="UNKNOWN",
            connected_generation=None,
            current_connection_generation=next_gen,
        )

        return DisconnectResponse(
            mailbox_id=mailbox_id, connection_state="DISCONNECTED"
        )

    # -------------------------------------------------------------------------
    # Controlled Test Send
    # -------------------------------------------------------------------------

    def send_controlled_test_email(
        self,
        workspace_id: UUID,
        user_id: UUID,
        mailbox_id: UUID,
        recipient_email: str | None = None,
        request_key: str | None = None,
        *,
        subject: str | None = None,
        body_html: str | None = None,
        operation: str = "mailbox.test_send",
        payload_fingerprint: str | None = None,
        attachments: tuple[EnvelopeAttachment, ...] = (),
    ) -> MailboxTestSendResult:
        """Send one controlled test email through the mailbox.

        With no `subject`/`body_html` this sends the fixed mailbox connectivity
        test. Callers that render real content (a campaign step's test send)
        pass it in and reuse every safety step below (mailbox state, recipient
        validation, suppression, authorization, attempt bookkeeping) instead of
        forking a second send path. `operation` scopes the idempotency receipt
        and `payload_fingerprint` binds it to the content being sent.
        """
        # 1. Validate Mailbox State
        mailbox = self.repo.get_mailbox(workspace_id, mailbox_id)
        if not mailbox:
            raise AppError("not_found", "Mailbox not found", status_code=404)

        if mailbox["connection_state"] != "CONNECTED":
            raise AppError(
                "conflict",
                f"Mailbox is not connected "
                f"(current state: {mailbox['connection_state']})",
                status_code=409,
            )

        if mailbox["policy_state"] != "ENABLED":
            reason = mailbox["policy_reason"] or "Policy restricted"
            raise AppError(
                "conflict",
                f"Mailbox sending is restricted: {reason}",
                status_code=409,
            )

        if mailbox["health_state"] == "DEGRADED":
            raise AppError(
                "conflict",
                "Mailbox health is degraded. Reconnection may be required.",
                status_code=409,
            )

        # 2. Validate Recipient
        target_email = (recipient_email or mailbox["original_address"]).strip()
        if not _EMAIL_PATTERN.match(target_email) or any(
            c in target_email for c in ["\r", "\n"]
        ):
            raise AppError(
                "bad_request", "Invalid recipient email address format", status_code=422
            )

        canonical_email = target_email.lower()

        # 3. Check Suppression
        address_id = self.repo.ensure_recipient_address(workspace_id, canonical_email)
        if self.repo.is_address_suppressed(workspace_id, address_id):
            raise AppError(
                "suppressed_recipient",
                "Recipient email address is suppressed and cannot receive email",
                status_code=400,
            )

        # 4. Resolve Membership & Idempotency
        membership_id = self.repo.get_user_membership_id(workspace_id, user_id)
        req_key = request_key or str(uuid4())
        payload_hash = hashlib.sha256(
            f"{target_email}\x00{payload_fingerprint}".encode()
            if payload_fingerprint
            else f"{target_email}".encode()
        ).hexdigest()
        receipt_id = self.repo.ensure_command_receipt(
            workspace_id, user_id, operation, req_key, payload_hash
        )

        # 5. Create Authorization
        now = datetime.now(UTC)
        auth_id = uuid4()
        self.repo.insert_controlled_send_authorization(
            auth_id=auth_id,
            workspace_id=workspace_id,
            requester_user_id=user_id,
            requester_membership_id=membership_id,
            mailbox_id=mailbox_id,
            address_id=address_id,
            command_receipt_id=receipt_id,
            expires_at=now + timedelta(minutes=15),
            evidence={"ip": "internal", "target": target_email},
        )

        # 6. Retrieve and Refresh Credentials if needed
        current_gen = mailbox["current_connection_generation"]
        conn = self.repo.get_mailbox_connection(
            workspace_id, mailbox_id, current_gen, for_update=True
        )
        if not conn or not conn["credential_ciphertext"]:
            raise AppError(
                "conflict", "Mailbox credentials not found or revoked", status_code=409
            )

        creds = decrypt_credentials(
            conn["credential_ciphertext"],
            conn["nonce"],
            workspace_id,
            mailbox_id,
            mailbox["provider"],
            conn["encryption_key_id"],
        )

        # Provider-neutral credential mapping: merges the decrypted secret
        # payload (e.g. Gmail/Microsoft {"access_token", ...}, or SMTP
        # {"password"}) with the non-secret protected_config (e.g. SMTP's
        # {"host", "port", "security_mode", "username"}) so every provider
        # adapter receives one consistent mapping shape.
        credential: dict[str, Any] = dict(creds)
        if conn.get("protected_config"):
            credential.update(conn["protected_config"])

        expires_at = conn["expires_at"]
        provider = ProviderRegistry.get(mailbox["provider"])

        # Refresh if within 5-minute safety margin (naturally never true for
        # SMTP, which has no expires_at; guarded by capability too for
        # clarity/defense-in-depth).
        if (
            ProviderCapability.CREDENTIAL_REFRESH in provider.capabilities
            and expires_at
            and (now + timedelta(minutes=5) >= expires_at)
        ):
            refresh_token = creds.get("refresh_token")
            if not refresh_token:
                self.repo.update_mailbox_connection_state(
                    workspace_id,
                    mailbox_id,
                    "RECONNECT_REQUIRED",
                    "DEGRADED",
                    None,
                    current_gen,
                )
                raise AppError(
                    "auth_failure",
                    "Mailbox credentials expired and no refresh token available. "
                    "Reconnect required.",
                    status_code=PROVIDER_AUTH_REJECTED_STATUS,
                )

            try:
                refresh_res = provider.refresh_token(refresh_token)
                new_refresh_token = refresh_res.refresh_token or refresh_token
                new_creds = {
                    "access_token": refresh_res.access_token,
                    "refresh_token": new_refresh_token,
                    "token_type": refresh_res.token_type,
                }
                new_ct, new_key_id, new_nonce = encrypt_credentials(
                    new_creds, workspace_id, mailbox_id, mailbox["provider"]
                )
                new_expiry = now + timedelta(seconds=refresh_res.expires_in)
                # A refreshed token rotates into a NEW connection generation
                # rather than mutating the current row in place: retained
                # credential history is immutable by design (see
                # mailbox_connections_guard_connection_history in
                # supabase/migrations/0003 -- an in-place UPDATE of
                # credential_ciphertext/nonce/expires_at is rejected by that
                # trigger, and app_connection isn't even granted UPDATE on
                # expires_at/granted_scopes). This mirrors the reconnect path
                # in complete_gmail_oauth above.
                new_gen = current_gen + 1
                self.repo.insert_mailbox_connection(
                    connection_id=uuid4(),
                    workspace_id=workspace_id,
                    mailbox_id=mailbox_id,
                    generation=new_gen,
                    credential_ciphertext=new_ct,
                    encryption_key_id=new_key_id,
                    nonce=new_nonce,
                    auth_mechanism="OAUTH",
                    granted_scopes=refresh_res.granted_scopes,
                    expires_at=new_expiry,
                )
                self.repo.update_mailbox_connection_state(
                    workspace_id,
                    mailbox_id,
                    "CONNECTED",
                    "HEALTHY",
                    new_gen,
                    new_gen,
                )
                current_gen = new_gen
                credential["access_token"] = refresh_res.access_token
            except AppError as exc:
                if exc.code == "auth_failure":
                    self.repo.update_mailbox_connection_state(
                        workspace_id,
                        mailbox_id,
                        "RECONNECT_REQUIRED",
                        "DEGRADED",
                        None,
                        current_gen,
                    )
                raise

        # 7. Create Message and Attempt
        message_id = uuid4()
        attempt_id = uuid4()
        rfc_message_id = (
            f"test-{message_id}@{mailbox['original_address'].split('@')[-1]}"
        )
        sender_label = (
            mailbox["sender_display_name"] or mailbox["original_address"]
        )
        if subject is None or body_html is None:
            subject = f"Test Email from {sender_label}"
            body_html = (
                f"<p>This is a controlled test email from the Email Outreach "
                f"Platform.</p>"
                f"<p>Mailbox: <strong>{mailbox['original_address']}</strong></p>"
                f"<p>Timestamp (UTC): {now.isoformat()}</p>"
            )
        content_digest = hashlib.sha256(
            f"{subject}\n{body_html}".encode()
        ).hexdigest()

        self.repo.insert_test_message(
            message_id=message_id,
            workspace_id=workspace_id,
            controlled_send_authorization_id=auth_id,
            mailbox_id=mailbox_id,
            address_id=address_id,
            rfc_message_id=rfc_message_id,
            content_subject=subject,
            content_body_html=body_html,
            content_digest=content_digest,
            frozen_destination=target_email,
            frozen_sender_address=mailbox["original_address"],
            frozen_sender_name=mailbox["sender_display_name"],
            status="SENDING",
        )

        self.repo.insert_message_attempt(
            attempt_id=attempt_id,
            workspace_id=workspace_id,
            message_id=message_id,
            mailbox_id=mailbox_id,
            generation=current_gen,
            authorization_deadline=now + timedelta(seconds=30),
            invocation_owner="api_test_send",
            evidence_state="PREPARED",
        )

        # 8. Dispatch through Provider
        envelope = OutboundMessageEnvelope(
            to_address=target_email,
            from_address=mailbox["original_address"],
            from_name=mailbox["sender_display_name"],
            subject=subject,
            body_html=body_html,
            rfc_message_id=rfc_message_id,
            attachments=attachments,
        )

        send_result = provider.send_message(credential, envelope)

        # 9. Commit durable result
        if send_result.status == "ACCEPTED":
            self.repo.update_message_and_attempt_result(
                workspace_id=workspace_id,
                message_id=message_id,
                attempt_id=attempt_id,
                status="SENT",
                evidence_state="ACCEPTED",
                provider_message_id=send_result.provider_message_id,
                provider_thread_id=send_result.provider_thread_id,
                # Locally-generated attempt identity, persisted as
                # reconciliation evidence regardless of whether the
                # provider itself returned a message id (Graph/SMTP often
                # don't) -- required by
                # message_attempts_acceptance_evidence_check.
                provider_request_id=rfc_message_id,
            )
            # Reaffirm healthy state
            self.repo.update_mailbox_connection_state(
                workspace_id,
                mailbox_id,
                "CONNECTED",
                "HEALTHY",
                current_gen,
                current_gen,
            )
            return MailboxTestSendResult(
                message_id=message_id,
                status="SENT",
                recipient_email=target_email,
                provider_message_id=send_result.provider_message_id,
                accepted_at=send_result.accepted_at or now,
            )
        elif send_result.status == "UNKNOWN":
            self.repo.update_message_and_attempt_result(
                workspace_id=workspace_id,
                message_id=message_id,
                attempt_id=attempt_id,
                status="UNKNOWN_OUTCOME",
                evidence_state="UNKNOWN",
                error_category=send_result.error_category,
                error_code=send_result.error_code,
            )
            return MailboxTestSendResult(
                message_id=message_id,
                status="UNKNOWN_OUTCOME",
                recipient_email=target_email,
                error_message="Send result is ambiguous due to a provider timeout.",
            )
        else:
            self.repo.update_message_and_attempt_result(
                workspace_id=workspace_id,
                message_id=message_id,
                attempt_id=attempt_id,
                status="FAILED",
                evidence_state="REJECTED",
                error_category=send_result.error_category,
                error_code=send_result.error_code,
            )
            err_desc = send_result.error_category or "Send failed"
            return MailboxTestSendResult(
                message_id=message_id,
                status="FAILED",
                recipient_email=target_email,
                error_message=f"Provider rejected message: {err_desc}",
            )
