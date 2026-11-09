from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.modules.rate_limit.policy_service import insert_default_mailbox_limit
from app.modules.suppression.checks import is_address_suppressed


def _safe_set_role(session: Session, role_name: str) -> None:
    """Attempt SET LOCAL ROLE if running against PostgreSQL; ignore in SQLite."""
    bind = session.get_bind()
    if bind and getattr(bind.dialect, "name", "") != "sqlite":
        session.execute(text(f"RESET ROLE; SET LOCAL ROLE {role_name}"))


class MailboxRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -------------------------------------------------------------------------
    # Mailbox queries
    # -------------------------------------------------------------------------

    def list_mailboxes(self, workspace_id: UUID) -> list[dict[str, Any]]:
        query = text(
            """
            SELECT id, provider, original_address, sender_display_name, signature_html,
                   connection_state, health_state, policy_state, policy_reason,
                   circuit_state, sync_state, blocked_until, pending_safety_count,
                   current_connection_generation, config_version, version,
                   created_at, updated_at
            FROM public.mailboxes
            WHERE workspace_id = :workspace_id
            ORDER BY created_at ASC
            """
        )
        rows = (
            self.session.execute(query, {"workspace_id": str(workspace_id)})
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    def get_mailbox(
        self,
        workspace_id: UUID,
        mailbox_id: UUID,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        clause = "FOR UPDATE" if for_update else ""
        query = text(
            f"""
            SELECT id, workspace_id, provider, provider_account_id,
                   original_address, sender_display_name, signature_html,
                   connection_state, health_state, policy_state, policy_reason,
                   circuit_state, sync_state, blocked_until,
                   pending_safety_count, current_connection_generation,
                   connected_generation, config_version, version,
                   created_at, updated_at
            FROM public.mailboxes
            WHERE workspace_id = :workspace_id AND id = :mailbox_id
            {clause}
            """
        )
        row = (
            self.session.execute(
                query,
                {"workspace_id": str(workspace_id), "mailbox_id": str(mailbox_id)},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def get_mailbox_by_provider_account(
        self,
        workspace_id: UUID,
        provider: str,
        provider_account_id: str,
    ) -> dict[str, Any] | None:
        query = text(
            """
            SELECT *
            FROM public.mailboxes
            WHERE workspace_id = :workspace_id
              AND provider = :provider
              AND provider_account_id = :provider_account_id
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "workspace_id": str(workspace_id),
                    "provider": provider,
                    "provider_account_id": provider_account_id,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def get_active_mailbox_by_provider_account_global(
        self,
        provider: str,
        provider_account_id: str,
    ) -> dict[str, Any] | None:
        query = text(
            """
            SELECT id, workspace_id, provider, provider_account_id, connection_state
            FROM public.mailboxes
            WHERE provider = :provider
              AND provider_account_id = :provider_account_id
              AND connection_state <> 'DISCONNECTED'
            """
        )
        row = (
            self.session.execute(
                query,
                {"provider": provider, "provider_account_id": provider_account_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def insert_mailbox(
        self,
        mailbox_id: UUID,
        workspace_id: UUID,
        provider: str,
        provider_account_id: str,
        original_address: str,
        sender_display_name: str | None = None,
        connection_state: str = "CONNECTED",
        health_state: str = "HEALTHY",
        generation: int = 1,
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_connection")
        connected_gen = generation if connection_state == "CONNECTED" else None
        query = text(
            """
            INSERT INTO public.mailboxes (
                id, workspace_id, provider, provider_account_id, original_address,
                sender_display_name, connection_state, health_state,
                current_connection_generation, connected_generation
            ) VALUES (
                :id, :workspace_id, :provider, :provider_account_id, :original_address,
                :sender_display_name, :connection_state, :health_state,
                :generation, :connected_gen
            )
            RETURNING *
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "id": str(mailbox_id),
                    "workspace_id": str(workspace_id),
                    "provider": provider,
                    "provider_account_id": provider_account_id,
                    "original_address": original_address,
                    "sender_display_name": sender_display_name,
                    "connection_state": connection_state,
                    "health_state": health_state,
                    "generation": generation,
                    "connected_gen": connected_gen,
                },
            )
            .mappings()
            .one()
        )
        return dict(row)

    def ensure_default_sending_limit(
        self, *, workspace_id: UUID, mailbox_id: UUID
    ) -> None:
        """Give a newly connected mailbox its default sending limit (idempotent).

        Runs as the role that just created the mailbox, in the same transaction,
        so a mailbox never exists without a limit. The send path refuses to send
        for a mailbox that has none.
        """
        _safe_set_role(self.session, "app_connection")
        insert_default_mailbox_limit(
            self.session, workspace_id=workspace_id, mailbox_id=mailbox_id
        )

    def update_mailbox_metadata(
        self,
        workspace_id: UUID,
        mailbox_id: UUID,
        sender_display_name: str | None,
        signature_html: str | None,
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_api")
        query = text(
            """
            UPDATE public.mailboxes
            SET sender_display_name = :sender_display_name,
                signature_html = :signature_html
            WHERE workspace_id = :workspace_id AND id = :mailbox_id
            RETURNING *
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "workspace_id": str(workspace_id),
                    "mailbox_id": str(mailbox_id),
                    "sender_display_name": sender_display_name,
                    "signature_html": signature_html,
                },
            )
            .mappings()
            .one()
        )
        return dict(row)

    def update_mailbox_connection_state(
        self,
        workspace_id: UUID,
        mailbox_id: UUID,
        connection_state: str,
        health_state: str,
        connected_generation: int | None,
        current_connection_generation: int,
        provider_account_id: str | None = None,
        original_address: str | None = None,
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_connection")
        query = text(
            """
            UPDATE public.mailboxes
            SET connection_state = :connection_state,
                health_state = :health_state,
                connected_generation = :connected_generation,
                current_connection_generation = :current_connection_generation,
                provider_account_id = COALESCE(
                    :provider_account_id, provider_account_id
                ),
                original_address = COALESCE(:original_address, original_address)
            WHERE workspace_id = :workspace_id AND id = :mailbox_id
            RETURNING *
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "workspace_id": str(workspace_id),
                    "mailbox_id": str(mailbox_id),
                    "connection_state": connection_state,
                    "health_state": health_state,
                    "connected_generation": connected_generation,
                    "current_connection_generation": current_connection_generation,
                    "provider_account_id": provider_account_id,
                    "original_address": original_address,
                },
            )
            .mappings()
            .one()
        )
        return dict(row)

    # -------------------------------------------------------------------------
    # OAuth flows
    # -------------------------------------------------------------------------

    def insert_oauth_flow(
        self,
        flow_id: UUID,
        workspace_id: UUID,
        actor_id: UUID,
        provider: str,
        state_digest: str,
        encrypted_verifier: bytes,
        verifier_key_id: str,
        verifier_nonce: bytes,
        return_path: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_connection")
        query = text(
            """
            INSERT INTO public.oauth_flows (
                id, workspace_id, actor_id, provider, state_digest,
                encrypted_verifier, verifier_key_id, verifier_nonce,
                return_path, expires_at, status
            ) VALUES (
                :id, :workspace_id, :actor_id, :provider, :state_digest,
                :encrypted_verifier, :verifier_key_id, :verifier_nonce,
                :return_path, :expires_at, 'PENDING'
            )
            RETURNING *
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "id": str(flow_id),
                    "workspace_id": str(workspace_id),
                    "actor_id": str(actor_id),
                    "provider": provider,
                    "state_digest": state_digest,
                    "encrypted_verifier": encrypted_verifier,
                    "verifier_key_id": verifier_key_id,
                    "verifier_nonce": verifier_nonce,
                    "return_path": return_path,
                    "expires_at": expires_at,
                },
            )
            .mappings()
            .one()
        )
        return dict(row)

    def get_and_claim_oauth_flow(
        self,
        state_digest: str,
        claim_owner: str,
    ) -> dict[str, Any] | None:
        _safe_set_role(self.session, "app_connection")
        # Atomically check PENDING + unexpired and transition to CLAIMED
        query = text(
            """
            UPDATE public.oauth_flows
            SET status = 'CLAIMED',
                claim_owner = :claim_owner,
                claim_expires_at = (
                    pg_catalog.transaction_timestamp() + interval '5 minutes'
                )
            WHERE state_digest = :state_digest
              AND status = 'PENDING'
              AND expires_at > pg_catalog.transaction_timestamp()
            RETURNING *
            """
        )
        row = (
            self.session.execute(
                query,
                {"state_digest": state_digest, "claim_owner": claim_owner},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def complete_oauth_flow(self, flow_id: UUID, resulting_mailbox_id: UUID) -> None:
        _safe_set_role(self.session, "app_connection")
        query = text(
            """
            UPDATE public.oauth_flows
            SET status = 'COMPLETED',
                resulting_mailbox_id = :resulting_mailbox_id,
                claim_owner = NULL,
                claim_expires_at = NULL
            WHERE id = :flow_id
            """
        )
        self.session.execute(
            query,
            {
                "flow_id": str(flow_id),
                "resulting_mailbox_id": str(resulting_mailbox_id),
            },
        )

    def fail_oauth_flow(self, flow_id: UUID) -> None:
        _safe_set_role(self.session, "app_connection")
        query = text(
            """
            UPDATE public.oauth_flows
            SET status = 'FAILED',
                claim_owner = NULL,
                claim_expires_at = NULL
            WHERE id = :flow_id
            """
        )
        self.session.execute(query, {"flow_id": str(flow_id)})

    # -------------------------------------------------------------------------
    # Mailbox connections (Encrypted credentials)
    # -------------------------------------------------------------------------

    def insert_mailbox_connection(
        self,
        connection_id: UUID,
        workspace_id: UUID,
        mailbox_id: UUID,
        generation: int,
        credential_ciphertext: bytes,
        encryption_key_id: str,
        nonce: bytes,
        auth_mechanism: str = "OAUTH",
        granted_scopes: list[str] | None = None,
        expires_at: datetime | None = None,
        protected_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_connection")
        query = text(
            """
            INSERT INTO public.mailbox_connections (
                id, workspace_id, mailbox_id, generation, credential_ciphertext,
                encryption_key_id, nonce, auth_mechanism, granted_scopes, expires_at,
                protected_config
            ) VALUES (
                :id, :workspace_id, :mailbox_id, :generation, :credential_ciphertext,
                :encryption_key_id, :nonce, :auth_mechanism,
                :granted_scopes, :expires_at, :protected_config
            )
            RETURNING *
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "id": str(connection_id),
                    "workspace_id": str(workspace_id),
                    "mailbox_id": str(mailbox_id),
                    "generation": generation,
                    "credential_ciphertext": credential_ciphertext,
                    "encryption_key_id": encryption_key_id,
                    "nonce": nonce,
                    "auth_mechanism": auth_mechanism,
                    "granted_scopes": json.dumps(granted_scopes or []),
                    "expires_at": expires_at,
                    "protected_config": (
                        json.dumps(protected_config)
                        if protected_config is not None
                        else None
                    ),
                },
            )
            .mappings()
            .one()
        )
        return dict(row)

    def get_mailbox_connection(
        self,
        workspace_id: UUID,
        mailbox_id: UUID,
        generation: int,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        _safe_set_role(self.session, "app_connection")
        clause = "FOR UPDATE" if for_update else ""
        query = text(
            f"""
            SELECT *
            FROM public.mailbox_connections
            WHERE workspace_id = :workspace_id
              AND mailbox_id = :mailbox_id
              AND generation = :generation
              AND destroyed_at IS NULL
            {clause}
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "workspace_id": str(workspace_id),
                    "mailbox_id": str(mailbox_id),
                    "generation": generation,
                },
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def destroy_mailbox_connection(
        self,
        workspace_id: UUID,
        mailbox_id: UUID,
        generation: int,
    ) -> None:
        _safe_set_role(self.session, "app_connection")
        query = text(
            """
            UPDATE public.mailbox_connections
            SET destroyed_at = pg_catalog.transaction_timestamp(),
                revoked_at = pg_catalog.transaction_timestamp(),
                credential_ciphertext = NULL,
                encryption_key_id = NULL,
                nonce = NULL,
                protected_config = NULL
            WHERE workspace_id = :workspace_id
              AND mailbox_id = :mailbox_id
              AND generation = :generation
            """
        )
        self.session.execute(
            query,
            {
                "workspace_id": str(workspace_id),
                "mailbox_id": str(mailbox_id),
                "generation": generation,
            },
        )

    # -------------------------------------------------------------------------
    # Recipient Addresses & Suppression checks
    # -------------------------------------------------------------------------

    def ensure_recipient_address(
        self,
        workspace_id: UUID,
        canonical_email: str,
    ) -> UUID:
        # DO NOTHING (not DO UPDATE) deliberately: app_api is only granted
        # INSERT on recipient_addresses (see recipient_addresses_api_insert
        # in supabase/migrations/0002_contacts_content.sql), not UPDATE.
        # canonical_address is itself the conflict key, so a DO UPDATE would
        # only ever rewrite it to the same value -- a pure no-op that still
        # requires Postgres to check UPDATE privilege at parse time. DO
        # NOTHING avoids needing that grant at all; the fallback SELECT below
        # (app_api already has SELECT) fetches the id on conflict.
        params = {
            "workspace_id": str(workspace_id),
            "canonical_address": canonical_email,
        }
        insert_query = text(
            """
            INSERT INTO public.recipient_addresses (
                workspace_id, canonical_address, normalization_version
            ) VALUES (
                :workspace_id, :canonical_address, 1
            )
            ON CONFLICT (workspace_id, canonical_address, normalization_version)
            DO NOTHING
            RETURNING id
            """
        )
        addr_id = self.session.execute(insert_query, params).scalar_one_or_none()
        if addr_id is None:
            select_query = text(
                """
                SELECT id FROM public.recipient_addresses
                WHERE workspace_id = :workspace_id
                  AND canonical_address = :canonical_address
                  AND normalization_version = 1
                """
            )
            addr_id = self.session.execute(select_query, params).scalar_one()
        return UUID(str(addr_id))

    def is_address_suppressed(self, workspace_id: UUID, address_id: UUID) -> bool:
        return is_address_suppressed(self.session, workspace_id, address_id)

    # -------------------------------------------------------------------------
    # Command Receipts & Controlled Test Send
    # -------------------------------------------------------------------------

    def ensure_command_receipt(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        operation: str,
        request_key: str,
        payload_hash: str,
    ) -> UUID:
        query = text(
            """
            INSERT INTO public.command_receipts (
                workspace_id, actor_id, operation, request_key, payload_hash,
                status, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :operation, :request_key, :payload_hash,
                'COMPLETED', (pg_catalog.transaction_timestamp() + interval '7 days')
            )
            ON CONFLICT (workspace_id, actor_id, operation, request_key)
            DO UPDATE SET status = EXCLUDED.status
            RETURNING id
            """
        )
        receipt_id = self.session.execute(
            query,
            {
                "workspace_id": str(workspace_id),
                "actor_id": str(actor_id),
                "operation": operation,
                "request_key": request_key,
                "payload_hash": payload_hash,
            },
        ).scalar_one()
        return UUID(str(receipt_id))

    def get_user_membership_id(self, workspace_id: UUID, user_id: UUID) -> UUID:
        query = text(
            """
            SELECT id
            FROM public.workspace_memberships
            WHERE workspace_id = :workspace_id
              AND user_id = :user_id
              AND status = 'ACTIVE'
            """
        )
        res = self.session.execute(
            query,
            {"workspace_id": str(workspace_id), "user_id": str(user_id)},
        ).scalar()
        if not res:
            raise AppError(
                "not_found", "Active workspace membership not found", status_code=404
            )
        return UUID(str(res))

    def insert_controlled_send_authorization(
        self,
        auth_id: UUID,
        workspace_id: UUID,
        requester_user_id: UUID,
        requester_membership_id: UUID,
        mailbox_id: UUID,
        address_id: UUID,
        command_receipt_id: UUID,
        expires_at: datetime,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_api")
        # RETURNING lists columns explicitly rather than `*`: app_api has
        # column-scoped SELECT on this table (controlled_send_authorizations_
        # api_select in supabase/migrations/0003) that deliberately excludes
        # recipient_approval_evidence -- that column is write-only for this
        # role, so `RETURNING *` fails with "permission denied for table"
        # even though every column actually being inserted is granted.
        query = text(
            """
            INSERT INTO public.controlled_send_authorizations (
                id, workspace_id, requester_user_id, requester_membership_id,
                mailbox_id, address_id, command_receipt_id, purpose, expires_at,
                recipient_approval_evidence
            ) VALUES (
                :id, :workspace_id, :requester_user_id, :requester_membership_id,
                :mailbox_id, :address_id, :command_receipt_id, 'CONTROLLED_TEST',
                :expires_at, :evidence
            )
            RETURNING id, workspace_id, requester_user_id, requester_membership_id,
                      mailbox_id, address_id, command_receipt_id, purpose,
                      expires_at, revoked_at, version, created_at, updated_at
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "id": str(auth_id),
                    "workspace_id": str(workspace_id),
                    "requester_user_id": str(requester_user_id),
                    "requester_membership_id": str(requester_membership_id),
                    "mailbox_id": str(mailbox_id),
                    "address_id": str(address_id),
                    "command_receipt_id": str(command_receipt_id),
                    "expires_at": expires_at,
                    "evidence": json.dumps(evidence or {}),
                },
            )
            .mappings()
            .one()
        )
        return dict(row)

    def insert_test_message(
        self,
        message_id: UUID,
        workspace_id: UUID,
        controlled_send_authorization_id: UUID,
        mailbox_id: UUID,
        address_id: UUID,
        rfc_message_id: str,
        content_subject: str,
        content_body_html: str,
        content_digest: str,
        frozen_destination: str,
        frozen_sender_address: str,
        frozen_sender_name: str | None,
        status: str = "SENDING",
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_worker_general")
        now = datetime.now(UTC)
        query = text(
            """
            INSERT INTO public.messages (
                id, workspace_id, purpose, controlled_send_authorization_id,
                mailbox_id, address_id, rfc_message_id, content_subject,
                content_body_html, content_digest, rendered_at,
                frozen_destination, frozen_sender_address, frozen_sender_name,
                due_at, anchor_at, status
            ) VALUES (
                :id, :workspace_id, 'CONTROLLED_TEST', :auth_id,
                :mailbox_id, :address_id, :rfc_message_id, :subject,
                :body_html, :digest, :rendered_at,
                :frozen_dest, :frozen_sender_addr, :frozen_sender_name,
                :due_at, :anchor_at, :status
            )
            RETURNING *
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "id": str(message_id),
                    "workspace_id": str(workspace_id),
                    "auth_id": str(controlled_send_authorization_id),
                    "mailbox_id": str(mailbox_id),
                    "address_id": str(address_id),
                    "rfc_message_id": rfc_message_id,
                    "subject": content_subject,
                    "body_html": content_body_html,
                    "digest": content_digest,
                    "rendered_at": now,
                    "frozen_dest": frozen_destination,
                    "frozen_sender_addr": frozen_sender_address,
                    "frozen_sender_name": frozen_sender_name,
                    "due_at": now,
                    "anchor_at": now,
                    "status": status,
                },
            )
            .mappings()
            .one()
        )
        return dict(row)

    def insert_message_attempt(
        self,
        attempt_id: UUID,
        workspace_id: UUID,
        message_id: UUID,
        mailbox_id: UUID,
        generation: int,
        authorization_deadline: datetime,
        invocation_owner: str = "api",
        evidence_state: str = "PREPARED",
        dispatch_generation: int = 1,
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_worker_send")
        # dispatch_generation ties this attempt to the parent message's own
        # dispatch_generation (messages.dispatch_generation, NOT NULL DEFAULT
        # 1 -- see supabase/migrations/0004) so reconciliation can tell which
        # dispatch cycle produced it. The column has no DB-side default of
        # its own, so it must always be passed explicitly; callers creating
        # the first attempt for a freshly inserted message use the default 1
        # to match that message's own default.
        query = text(
            """
            INSERT INTO public.message_attempts (
                id, workspace_id, message_id, mailbox_id, ordinal,
                invocation_owner, credential_generation, authorization_deadline,
                dispatch_generation, evidence_state
            ) VALUES (
                :id, :workspace_id, :message_id, :mailbox_id, 1,
                :invocation_owner, :generation, :authorization_deadline,
                :dispatch_generation, :evidence_state
            )
            RETURNING *
            """
        )
        row = (
            self.session.execute(
                query,
                {
                    "id": str(attempt_id),
                    "workspace_id": str(workspace_id),
                    "message_id": str(message_id),
                    "mailbox_id": str(mailbox_id),
                    "invocation_owner": invocation_owner,
                    "generation": generation,
                    "authorization_deadline": authorization_deadline,
                    "dispatch_generation": dispatch_generation,
                    "evidence_state": evidence_state,
                },
            )
            .mappings()
            .one()
        )
        return dict(row)

    def update_message_and_attempt_result(
        self,
        workspace_id: UUID,
        message_id: UUID,
        attempt_id: UUID,
        status: str,
        evidence_state: str,
        provider_message_id: str | None = None,
        provider_thread_id: str | None = None,
        error_category: str | None = None,
        error_code: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        _safe_set_role(self.session, "app_worker_send")
        now = datetime.now(UTC)
        accepted_at = now if status == "SENT" else None

        # Update attempt
        #
        # message_attempts_acceptance_evidence_check requires evidence_state
        # = 'ACCEPTED' to carry at least one of provider_request_id /
        # provider_message_ref / non-empty reconciliation_metadata. Not
        # every provider returns a provider_message_id synchronously (Graph
        # sendMail returns 202 with no body; generic SMTP submission has no
        # message-id concept at all) -- callers must pass provider_request_id
        # (their own locally-generated attempt identity, e.g. the RFC
        # Message-ID minted before invocation) so acceptance evidence is
        # always present regardless of what the provider itself supplies.
        self.session.execute(
            text(
                """
                UPDATE public.message_attempts
                SET evidence_state = :evidence_state,
                    completed_at = :completed_at,
                    provider_request_id = :provider_request_id,
                    provider_message_ref = :provider_message_id,
                    provider_thread_ref = :provider_thread_id,
                    error_category = :error_category,
                    error_code = :error_code
                WHERE workspace_id = :workspace_id AND id = :attempt_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "attempt_id": str(attempt_id),
                "evidence_state": evidence_state,
                "completed_at": now,
                "provider_request_id": provider_request_id,
                "provider_message_id": provider_message_id,
                "provider_thread_id": provider_thread_id,
                "error_category": error_category,
                "error_code": error_code,
            },
        )

        # Update message
        self.session.execute(
            text(
                """
                UPDATE public.messages
                SET status = :status,
                    accepted_at = :accepted_at,
                    provider_message_id = :provider_message_id,
                    terminal_reason = CASE
                        WHEN :status = 'FAILED' THEN :error_category
                        ELSE NULL
                    END
                WHERE workspace_id = :workspace_id AND id = :message_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "message_id": str(message_id),
                "status": status,
                "accepted_at": accepted_at,
                "provider_message_id": provider_message_id,
                "error_category": error_category,
            },
        )
