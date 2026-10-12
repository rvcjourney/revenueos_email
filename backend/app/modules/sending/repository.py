from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.sending.schemas import LoadedSendContext

logger = logging.getLogger(__name__)


def _safe_set_role(session: Session, role_name: str) -> None:
    bind = session.get_bind()
    if bind and getattr(bind.dialect, "name", "") != "sqlite":
        session.execute(text(f"RESET ROLE; SET LOCAL ROLE {role_name}"))


def _safe_set_workspace(session: Session, workspace_id: UUID | None) -> None:
    bind = session.get_bind()
    if bind and getattr(bind.dialect, "name", "") != "sqlite":
        if workspace_id is not None:
            session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"),
                {"ws": str(workspace_id)},
            )
        else:
            session.execute(text("SELECT set_config('app.workspace_id', '', true)"))


class MessageLockedByAnotherWorker(Exception):
    """The message row exists but is currently locked (FOR UPDATE) by a
    concurrent transaction -- a duplicate/racing task delivery. Callers must
    treat this as a safe no-op, not an error: whichever worker holds the
    lock owns this send attempt."""


class AttemptAlreadyClaimed(Exception):
    """Raised when `insert_prepared_attempt` loses the race for the unique
    partial index `message_attempts_unresolved_idx` -- another
    transaction's PREPARED/UNKNOWN attempt already exists for this message.
    This IS the send-ownership claim mechanism; losing it is a safe no-op,
    not an error."""


class SendingRepository:
    """Database operations for the `app_worker_send` role. Mirrors the
    dual sqlite/postgres pattern used by SchedulerRepository."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _is_sqlite(self) -> bool:
        bind = self.session.get_bind()
        return bool(bind and getattr(bind.dialect, "name", "") == "sqlite")

    # -------------------------------------------------------------------------
    # Load + lock authoritative state
    # -------------------------------------------------------------------------

    def load_message_for_send(
        self, *, workspace_id: UUID, message_id: UUID
    ) -> LoadedSendContext | None:
        """Reload every piece of state a send decision depends on, locking
        rows FOR UPDATE in the order: message (to discover its FKs) ->
        campaign -> enrollment -> mailbox. Returns None if the message truly
        does not exist. Raises `MessageLockedByAnotherWorker` if it exists
        but is currently locked by a concurrent transaction.
        """
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        is_sqlite = self._is_sqlite()
        lock_clause = "" if is_sqlite else "FOR UPDATE SKIP LOCKED"
        plain_lock_clause = "" if is_sqlite else "FOR UPDATE"

        params = {"ws": str(workspace_id), "mid": str(message_id)}

        msg_query = f"""
            SELECT id, workspace_id, purpose, campaign_id, enrollment_id,
                   mailbox_id, address_id, status, dispatch_generation, version,
                   retry_count, retry_budget, content_subject, content_body_html,
                   content_digest, renderer_version, rendered_at, frozen_destination,
                   frozen_sender_address, frozen_sender_name, rfc_message_id, step_id,
                   schedule_generation
            FROM public.messages
            WHERE workspace_id = :ws AND id = :mid
            {lock_clause}
        """
        msg = self.session.execute(text(msg_query), params).mappings().first()
        if not msg:
            exists = self.session.execute(
                text(
                    "SELECT 1 FROM public.messages "
                    "WHERE workspace_id = :ws AND id = :mid"
                ),
                params,
            ).first()
            if exists:
                raise MessageLockedByAnotherWorker(str(message_id))
            return None

        campaign_status = None
        campaign_planning_status = None
        campaign_schedule_generation = None
        if msg["purpose"] == "CAMPAIGN" and msg["campaign_id"] is not None:
            camp = (
                self.session.execute(
                    text(
                        f"""
                    SELECT status, planning_status, schedule_generation
                    FROM public.campaigns
                    WHERE workspace_id = :ws AND id = :cid
                    {plain_lock_clause}
                    """
                    ),
                    {"ws": str(workspace_id), "cid": str(msg["campaign_id"])},
                )
                .mappings()
                .first()
            )
            if camp:
                campaign_status = camp["status"]
                campaign_planning_status = camp["planning_status"]
                campaign_schedule_generation = camp["schedule_generation"]

        enrollment_state = None
        if msg["purpose"] == "CAMPAIGN" and msg["enrollment_id"] is not None:
            enr = (
                self.session.execute(
                    text(
                        f"""
                    SELECT state FROM public.campaign_enrollments
                    WHERE workspace_id = :ws AND id = :eid
                    {plain_lock_clause}
                    """
                    ),
                    {"ws": str(workspace_id), "eid": str(msg["enrollment_id"])},
                )
                .mappings()
                .first()
            )
            if enr:
                enrollment_state = enr["state"]

        mailbox = (
            self.session.execute(
                text(
                    f"""
                SELECT workspace_id, provider, provider_account_id, connection_state,
                       health_state, policy_state, policy_reason, circuit_state,
                       blocked_until, current_connection_generation, original_address,
                       sender_display_name, pending_safety_count
                FROM public.mailboxes
                WHERE workspace_id = :ws AND id = :mbid
                {plain_lock_clause}
                """
                ),
                {"ws": str(workspace_id), "mbid": str(msg["mailbox_id"])},
            )
            .mappings()
            .first()
        )
        if not mailbox:
            raise MessageLockedByAnotherWorker(
                f"mailbox {msg['mailbox_id']} missing for message {message_id}"
            )

        address = (
            self.session.execute(
                text(
                    "SELECT canonical_address FROM public.recipient_addresses "
                    "WHERE workspace_id = :ws AND id = :aid"
                ),
                {"ws": str(workspace_id), "aid": str(msg["address_id"])},
            )
            .mappings()
            .first()
        )
        canonical_address = address["canonical_address"] if address else ""

        return LoadedSendContext(
            message_id=UUID(str(msg["id"])),
            workspace_id=UUID(str(msg["workspace_id"])),
            purpose=msg["purpose"],
            campaign_id=UUID(str(msg["campaign_id"])) if msg["campaign_id"] else None,
            enrollment_id=(
                UUID(str(msg["enrollment_id"])) if msg["enrollment_id"] else None
            ),
            mailbox_id=UUID(str(msg["mailbox_id"])),
            address_id=UUID(str(msg["address_id"])),
            message_status=msg["status"],
            dispatch_generation=msg["dispatch_generation"],
            message_version=msg["version"],
            retry_count=msg["retry_count"],
            retry_budget=msg["retry_budget"],
            content_subject=msg["content_subject"],
            content_body_html=msg["content_body_html"],
            content_digest=msg["content_digest"],
            renderer_version=msg["renderer_version"],
            rendered_at=msg["rendered_at"],
            frozen_destination=msg["frozen_destination"],
            frozen_sender_address=msg["frozen_sender_address"],
            frozen_sender_name=msg["frozen_sender_name"],
            rfc_message_id=msg["rfc_message_id"],
            campaign_status=campaign_status,
            campaign_planning_status=campaign_planning_status,
            enrollment_state=enrollment_state,
            canonical_address=canonical_address,
            mailbox_workspace_id=UUID(str(mailbox["workspace_id"])),
            mailbox_provider=mailbox["provider"],
            mailbox_provider_account_id=mailbox["provider_account_id"],
            mailbox_connection_state=mailbox["connection_state"],
            mailbox_health_state=mailbox["health_state"],
            mailbox_policy_state=mailbox["policy_state"],
            mailbox_policy_reason=mailbox["policy_reason"],
            mailbox_circuit_state=mailbox["circuit_state"],
            mailbox_blocked_until=mailbox["blocked_until"],
            mailbox_current_connection_generation=mailbox[
                "current_connection_generation"
            ],
            mailbox_original_address=mailbox["original_address"],
            mailbox_sender_display_name=mailbox["sender_display_name"],
            mailbox_pending_safety_count=int(mailbox.get("pending_safety_count", 0)),
            message_schedule_generation=msg["schedule_generation"],
            campaign_schedule_generation=campaign_schedule_generation,
            raw=dict(msg),
        )


    def list_step_attachments(
        self, *, workspace_id: UUID, step_id: UUID
    ) -> list[dict[str, Any]]:
        """ Attachment rows for the step a campaign message was rendered from.
        The rows are immutable once the sequence is frozen, so they are exactly
        what was approved at activation."""
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        rows = (
            self.session.execute(
                text(
                    """
                SELECT id, storage_key, filename, content_type, size_bytes,
                       sha256, disposition, content_id
                FROM public.campaign_step_attachments
                WHERE workspace_id = :ws AND step_id = :sid
                ORDER BY created_at ASC, id ASC
                """
                ),
                {"ws": str(workspace_id), "sid": str(step_id)},
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    def get_workspace_defaults(self, *, workspace_id: UUID) -> dict[str, Any]:
        """The workspace's `defaults` JSON (footer settings live under
        `compliance`). Empty when absent or not an object."""
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        row = (
            self.session.execute(
                text("SELECT defaults FROM public.workspaces WHERE id = :ws"),
                {"ws": str(workspace_id)},
            )
            .mappings()
            .first()
        )
        value = row["defaults"] if row else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        return value if isinstance(value, dict) else {}

    def get_mailbox_connection(
        self, *, workspace_id: UUID, mailbox_id: UUID, generation: int
    ) -> dict[str, Any] | None:
        # app_worker_send (not app_connection): this role's own SELECT grant
        # on mailbox_connections is what the send worker actually runs
        # under, per the DB role separation in WORKERS.md/DATABASE.md.
        _safe_set_role(self.session, "app_worker_send")
        # app.workspace_id is transaction-local: the send commits its
        # authorization transaction before the provider call, so every
        # later transaction must set the workspace again or RLS hides
        # every row (and rejects every insert) for this role.
        _safe_set_workspace(self.session, workspace_id)
        row = (
            self.session.execute(
                text(
                    """
                SELECT credential_ciphertext, encryption_key_id, nonce,
                       protected_config, expires_at
                FROM public.mailbox_connections
                WHERE workspace_id = :ws AND mailbox_id = :mbid AND generation = :gen
                  AND destroyed_at IS NULL
                """
                ),
                {"ws": str(workspace_id), "mbid": str(mailbox_id), "gen": generation},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def insert_mailbox_connection(
        self,
        *,
        connection_id: UUID,
        workspace_id: UUID,
        mailbox_id: UUID,
        generation: int,
        credential_ciphertext: bytes,
        encryption_key_id: str,
        nonce: bytes,
        auth_mechanism: str,
        granted_scopes: list[str] | None,
        expires_at: datetime | None,
    ) -> None:
        """Rotates a refreshed credential into a NEW connection generation
        (never mutates the current row -- credential history is immutable,
        enforced by mailboxes_connections_guard_connection_history). Mirrors
        MailboxRepository.insert_mailbox_connection, run under app_worker_send
        rather than app_connection, matching this repository's own grant.
        """
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        self.session.execute(
            text(
                """
                INSERT INTO public.mailbox_connections (
                    id, workspace_id, mailbox_id, generation, credential_ciphertext,
                    encryption_key_id, nonce, auth_mechanism, granted_scopes, expires_at
                ) VALUES (
                    :id, :ws, :mbid, :gen, :ciphertext,
                    :key_id, :nonce, :auth_mechanism, :scopes, :expires_at
                )
                """
            ),
            {
                "id": str(connection_id),
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "gen": generation,
                "ciphertext": credential_ciphertext,
                "key_id": encryption_key_id,
                "nonce": nonce,
                "auth_mechanism": auth_mechanism,
                "scopes": json.dumps(granted_scopes or []),
                "expires_at": expires_at,
            },
        )

    def update_mailbox_connection_state(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        connection_state: str,
        health_state: str,
        connected_generation: int | None,
        current_connection_generation: int,
    ) -> None:
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        self.session.execute(
            text(
                """
                UPDATE public.mailboxes
                SET connection_state = :connection_state,
                    health_state = :health_state,
                    connected_generation = :connected_generation,
                    current_connection_generation = :current_connection_generation
                WHERE workspace_id = :ws AND id = :mbid
                """
            ),
            {
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "connection_state": connection_state,
                "health_state": health_state,
                "connected_generation": connected_generation,
                "current_connection_generation": current_connection_generation,
            },
        )

    def update_mailbox_blocked_until(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        blocked_until: datetime,
    ) -> None:
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        self.session.execute(
            text(
                """
                UPDATE public.mailboxes
                SET blocked_until = :blocked_until
                WHERE workspace_id = :ws AND id = :mbid
                """
            ),
            {
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "blocked_until": blocked_until,
            },
        )

    def get_rate_control_status(self) -> dict[str, Any] | None:
        _safe_set_role(self.session, "app_worker_send")
        row = (
            self.session.execute(
                text("SELECT generation, status FROM public.rate_control WHERE id")
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def skip_message(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        expected_version: int,
        terminal_reason: str,
    ) -> bool:
        """Direct SKIPPED transition for gate rejections that never reach
        the attempt-claim step (suppression, invalid recipient, snapshot
        integrity, cross-tenant/provider mismatch) -- there is no
        message_attempts row to finalize for these."""
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        result = self.session.execute(
            text(
                """
                UPDATE public.messages
                SET status = 'SKIPPED', terminal_reason = :terminal_reason,
                    version = version + 1
                WHERE workspace_id = :ws AND id = :mid AND version = :expected_version
                """
            ),
            {
                "ws": str(workspace_id),
                "mid": str(message_id),
                "terminal_reason": terminal_reason,
                "expected_version": expected_version,
            },
        )
        return result.rowcount > 0

    def defer_for_capacity(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        dispatch_generation: int,
        due_at: datetime,
    ) -> bool:
        """Hand a claimed message back to the scheduler to try again at `due_at`.

        Used when the message cannot be sent yet but nothing is wrong with it (the
        mailbox is at its limit, or held). Without this a denied message would sit
        QUEUED until its claim lease expired and then be claimed again at once.

        Only a still-QUEUED message of this claim that has no attempt is touched, so
        a message that reached the provider is never moved. `due_at` is kept at or
        after `next_retry_at` for a retrying message (messages_retry_state_check).
        The generation is bumped so that a late duplicate of the task that made this
        deferral is recognised as stale and ignored: at the old generation it would
        pass the generation check, find the message SCHEDULED instead of QUEUED, and
        have it permanently skipped. The claim's outbox delivery was already
        published; the next claim opens a new one (semantic key
        `{message}:{dispatch_generation + 1}`).
        """
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        result = self.session.execute(
            text(
                """
                UPDATE public.messages
                SET status = dispatch_origin,
                    dispatch_origin = NULL,
                    claim_expires_at = NULL,
                    dispatch_generation = dispatch_generation + 1,
                    due_at = CASE WHEN next_retry_at IS NOT NULL
                                       AND next_retry_at > :due_at
                                  THEN next_retry_at ELSE :due_at END,
                    version = version + 1
                WHERE workspace_id = :ws AND id = :mid
                  AND status = 'QUEUED'
                  AND dispatch_generation = :gen
                  AND dispatch_origin IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM public.message_attempts a
                      WHERE a.workspace_id = :ws AND a.message_id = :mid
                        AND a.dispatch_generation = :gen
                  )
                """
            ),
            {
                "ws": str(workspace_id),
                "mid": str(message_id),
                "gen": dispatch_generation,
                "due_at": due_at,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    # -------------------------------------------------------------------------
    # Authorization transaction: claim + debit + transition
    # -------------------------------------------------------------------------

    def insert_prepared_attempt(
        self,
        *,
        attempt_id: UUID,
        workspace_id: UUID,
        message_id: UUID,
        mailbox_id: UUID,
        ordinal: int,
        invocation_owner: str,
        credential_generation: int,
        authorization_deadline: datetime,
        dispatch_generation: int,
    ) -> None:
        """Insert the PREPARED attempt row that IS the send-ownership claim.
        The unique partial index message_attempts_unresolved_idx (workspace_id,
        message_id) WHERE evidence_state IN ('PREPARED','UNKNOWN') guarantees
        at most one caller wins this insert; the loser must raise
        AttemptAlreadyClaimed, never proceed to the provider.
        """
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        savepoint = self.session.begin_nested()
        try:
            self.session.execute(
                text(
                    """
                    INSERT INTO public.message_attempts (
                        id, workspace_id, message_id, mailbox_id, ordinal,
                        invocation_owner, credential_generation, authorization_deadline,
                        dispatch_generation, evidence_state
                    ) VALUES (
                        :id, :ws, :mid, :mbid, :ordinal,
                        :owner, :cred_gen, :deadline, :dispatch_gen, 'PREPARED'
                    )
                    """
                ),
                {
                    "id": str(attempt_id),
                    "ws": str(workspace_id),
                    "mid": str(message_id),
                    "mbid": str(mailbox_id),
                    "ordinal": ordinal,
                    "owner": invocation_owner,
                    "cred_gen": credential_generation,
                    "deadline": authorization_deadline,
                    "dispatch_gen": dispatch_generation,
                },
            )
            savepoint.commit()
        except IntegrityError as exc:
            savepoint.rollback()
            raise AttemptAlreadyClaimed(str(message_id)) from exc

    def next_attempt_ordinal(self, *, workspace_id: UUID, message_id: UUID) -> int:
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        max_ordinal = self.session.execute(
            text(
                "SELECT MAX(ordinal) FROM public.message_attempts "
                "WHERE workspace_id = :ws AND message_id = :mid"
            ),
            {"ws": str(workspace_id), "mid": str(message_id)},
        ).scalar()
        return int(max_ordinal or 0) + 1

    def insert_capacity_debit(
        self,
        *,
        workspace_id: UUID,
        attempt_id: UUID,
        reservation_id: str,
        unit: str,
        quantity: int,
        scopes: list[dict[str, Any]],
    ) -> UUID:
        """Insert one capacity_debits row (always -- even with zero scopes,
        for audit consistency: "this attempt consumed N units") plus one
        capacity_debit_scopes child row per applicable policy. Fails via the
        capacity_debits_require_rate_ready trigger if the durable rate
        controller has not marked the system READY -- this is intentional
        defense in depth alongside the Redis-side generation/READY check.
        """
        from uuid import uuid4

        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        debit_id = uuid4()
        self.session.execute(
            text(
                """
                INSERT INTO public.capacity_debits (
                    id, workspace_id, attempt_id, reservation_id, unit, quantity
                ) VALUES (:id, :ws, :attempt_id, :reservation_id, :unit, :quantity)
                """
            ),
            {
                "id": str(debit_id),
                "ws": str(workspace_id),
                "attempt_id": str(attempt_id),
                "reservation_id": reservation_id,
                "unit": unit,
                "quantity": quantity,
            },
        )
        for scope in scopes:
            self.session.execute(
                text(
                    """
                    INSERT INTO public.capacity_debit_scopes (
                        id, workspace_id, debit_id,
                        rate_scope_id, tenant_rate_policy_id,
                        unit, quantity, policy_version, policy_snapshot,
                        window_kind, bucket_start_at, bucket_end_at
                    ) VALUES (
                        :id, :ws, :debit_id,
                        :rate_scope_id, :tenant_rate_policy_id,
                        :unit, :quantity, :policy_version, :policy_snapshot,
                        :window_kind, :bucket_start_at, :bucket_end_at
                    )
                    """
                ),
                {
                    "id": str(uuid4()),
                    "ws": str(workspace_id),
                    "debit_id": str(debit_id),
                    "rate_scope_id": scope.get("rate_scope_id"),
                    "tenant_rate_policy_id": scope.get("tenant_rate_policy_id"),
                    "unit": unit,
                    "quantity": quantity,
                    "policy_version": scope["policy_version"],
                    "policy_snapshot": json.dumps(scope["policy_snapshot"]),
                    "window_kind": scope.get("window_kind", "ROLLING"),
                    "bucket_start_at": scope.get("bucket_start_at"),
                    "bucket_end_at": scope.get("bucket_end_at"),
                },
            )
        return debit_id

    def transition_message_to_sending(
        self, *, workspace_id: UUID, message_id: UUID, expected_version: int
    ) -> bool:
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        result = self.session.execute(
            text(
                """
                UPDATE public.messages
                SET status = 'SENDING', version = version + 1
                WHERE workspace_id = :ws AND id = :mid AND version = :expected_version
                """
            ),
            {
                "ws": str(workspace_id),
                "mid": str(message_id),
                "expected_version": expected_version,
            },
        )
        return result.rowcount > 0

    # -------------------------------------------------------------------------
    # Result persistence (second, short transaction -- after the provider call)
    # -------------------------------------------------------------------------

    def finalize_attempt_result(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        attempt_id: UUID,
        message_status: str,
        evidence_state: str,
        provider_message_id: str | None = None,
        provider_thread_id: str | None = None,
        provider_request_id: str | None = None,
        error_category: str | None = None,
        error_code: str | None = None,
        terminal_reason: str | None = None,
        hold_reason: str | None = None,
        retry_count: int | None = None,
        next_retry_at: datetime | None = None,
        due_at: datetime | None = None,
        rfc_message_id: str | None = None,
    ) -> None:
        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        now_col = (
            "CURRENT_TIMESTAMP"
            if self._is_sqlite()
            else "pg_catalog.transaction_timestamp()"
        )
        completed_at_expr = "NULL" if evidence_state == "UNKNOWN" else now_col

        self.session.execute(
            text(
                f"""
                UPDATE public.message_attempts
                SET evidence_state = :evidence_state,
                    completed_at = {completed_at_expr},
                    provider_request_id = :provider_request_id,
                    provider_message_ref = :provider_message_id,
                    provider_thread_ref = :provider_thread_id,
                    error_category = :error_category,
                    error_code = :error_code
                WHERE workspace_id = :ws AND id = :attempt_id
                """
            ),
            {
                "ws": str(workspace_id),
                "attempt_id": str(attempt_id),
                "evidence_state": evidence_state,
                "provider_request_id": provider_request_id,
                "provider_message_id": provider_message_id,
                "provider_thread_id": provider_thread_id,
                "error_category": error_category,
                "error_code": error_code,
            },
        )

        accepted_at_expr = now_col if message_status == "SENT" else "NULL"
        self.session.execute(
            text(
                f"""
                UPDATE public.messages
                SET status = :status,
                    accepted_at = {accepted_at_expr},
                    provider_message_id =
                        COALESCE(:provider_message_id, provider_message_id),
                    rfc_message_id = COALESCE(rfc_message_id, :rfc_message_id),
                    terminal_reason = :terminal_reason,
                    hold_reason = :hold_reason,
                    retry_count = COALESCE(:retry_count, retry_count),
                    next_retry_at = :next_retry_at,
                    due_at = COALESCE(:due_at, due_at),
                    version = version + 1
                WHERE workspace_id = :ws AND id = :mid
                """
            ),
            {
                "ws": str(workspace_id),
                "mid": str(message_id),
                "status": message_status,
                "provider_message_id": provider_message_id,
                "rfc_message_id": rfc_message_id,
                "terminal_reason": terminal_reason,
                "hold_reason": hold_reason,
                "retry_count": retry_count,
                "next_retry_at": next_retry_at,
                "due_at": due_at,
            },
        )

    def record_audit_event(
        self,
        *,
        workspace_id: UUID,
        action: str,
        target_id: UUID,
        after_state: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        from uuid import uuid4

        _safe_set_role(self.session, "app_worker_send")
        _safe_set_workspace(self.session, workspace_id)
        self.session.execute(
            text(
                """
                INSERT INTO public.audit_events (
                    id, workspace_id, actor_kind, actor_id, action, target_type,
                    target_id, after_state, reason
                ) VALUES (
                    :id, :ws, 'SYSTEM', NULL, :action, 'message', :target_id,
                    :after_state, :reason
                )
                """
            ),
            {
                "id": str(uuid4()),
                "ws": str(workspace_id),
                "action": action,
                "target_id": str(target_id),
                "after_state": json.dumps(after_state),
                "reason": reason,
            },
        )
