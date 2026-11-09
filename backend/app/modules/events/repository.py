from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.events.schemas import (
    ReceiptStatus,
    ScopeKind,
)
from app.modules.mailboxes.providers.message_builder import sql_normalized_message_id


def _safe_set_role(session: Session, role: str) -> None:
    bind = session.get_bind()
    if bind and getattr(bind.dialect, "name", "") == "postgresql":
        session.execute(text(f"SET LOCAL ROLE {role}"))


def _safe_set_workspace(session: Session, workspace_id: UUID | None) -> None:
    # Must be `app.workspace_id`: that is the setting public.app_current_workspace_id()
    # reads, so it is what every RLS policy sees. (This used to set an unrelated
    # `app.current_workspace_id`, which left RLS with no workspace at all.)
    bind = session.get_bind()
    if bind and getattr(bind.dialect, "name", "") == "postgresql":
        session.execute(
            text("SELECT set_config('app.workspace_id', :ws, true)"),
            {"ws": str(workspace_id) if workspace_id else ""},
        )


# NOTE: UPDATE statements on provider_receipts, safety_holds and suppressions do
# not assign `version`: the *_touch_row triggers maintain it, and the worker
# role's column grants deliberately exclude it (assigning it fails with
# "permission denied"; only real PostgreSQL can show that).
class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -------------------------------------------------------------------------
    # Mailbox & Tenant Resolution
    # -------------------------------------------------------------------------

    def resolve_mailbox_by_email_or_account(
        self,
        *,
        provider: str,
        email_address: str | None = None,
        provider_account_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Resolve internal mailbox and workspace context from provider identifiers.

        A provider account may be active in at most one workspace platform-wide
        (PROVIDER_ARCHITECTURE.md). Resolves server-side ownership without
        trusting caller-supplied workspace IDs.
        """
        clauses = ["m.provider = :provider", "m.connection_state <> 'DISCONNECTED'"]
        params: dict[str, Any] = {"provider": provider.upper()}

        subclauses = []
        if provider_account_id:
            subclauses.append("m.provider_account_id = :provider_account_id")
            params["provider_account_id"] = provider_account_id

        if email_address:
            subclauses.append("LOWER(m.original_address) = LOWER(:email)")
            params["email"] = email_address

        if not subclauses:
            return None

        clauses.append(f"({' OR '.join(subclauses)})")
        stmt = text(
            f"""
            SELECT m.id, m.workspace_id, m.provider, m.provider_account_id,
                   m.original_address, m.connection_state, m.pending_safety_count
            FROM public.mailboxes m
            WHERE {' AND '.join(clauses)}
            LIMIT 1
            """
        )
        return cast(
            Mapping[str, Any] | None,
            self.session.execute(stmt, params).mappings().first(),
        )

    def resolve_mailbox_by_id(
        self,
        *,
        mailbox_id: UUID,
        workspace_id: UUID | None = None,
    ) -> Mapping[str, Any] | None:
        ws_clause = "AND workspace_id = :workspace_id" if workspace_id else ""
        params: dict[str, Any] = {"mailbox_id": str(mailbox_id)}
        if workspace_id:
            params["workspace_id"] = str(workspace_id)

        stmt = text(
            f"""
            SELECT id, workspace_id, provider, provider_account_id,
                   original_address, connection_state, pending_safety_count
            FROM public.mailboxes
            WHERE id = :mailbox_id {ws_clause}
            LIMIT 1
            """
        )
        return cast(
            Mapping[str, Any] | None,
            self.session.execute(stmt, params).mappings().first(),
        )

    # -------------------------------------------------------------------------
    # Durable Raw Receipt Persistence
    # -------------------------------------------------------------------------

    def store_raw_receipt(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID | None,
        provider: str,
        scope_kind: ScopeKind,
        event_identity: str,
        source_schema: str,
        payload_digest: str | None = None,
        payload_ref: str | None = None,
        verified_at: datetime | None = None,
    ) -> tuple[UUID, bool]:
        """Durable insertion into provider_receipts with database-backed deduplication.

        Returns (receipt_id, was_inserted).
        If the receipt already exists under the unique constraint
        (workspace_id, [mailbox_id], provider, event_identity), DO NOTHING occurs
        and the existing receipt ID is returned with was_inserted=False.
        """
        _safe_set_workspace(self.session, workspace_id)

        insert_stmt = text(
            """
            INSERT INTO public.provider_receipts (
                workspace_id, mailbox_id, provider, scope_kind,
                event_identity, source_schema, receipt_status,
                payload_digest, payload_ref, verified_at
            )
            VALUES (
                :workspace_id, :mailbox_id, :provider, :scope_kind,
                :event_identity, :source_schema, 'RECEIVED',
                :payload_digest, :payload_ref, :verified_at
            )
            ON CONFLICT DO NOTHING
            RETURNING id
            """
        )

        params: dict[str, Any] = {
            "workspace_id": str(workspace_id),
            "mailbox_id": str(mailbox_id) if mailbox_id else None,
            "provider": provider.upper(),
            "scope_kind": scope_kind.value,
            "event_identity": event_identity,
            "source_schema": source_schema,
            "payload_digest": payload_digest,
            "payload_ref": payload_ref,
            "verified_at": verified_at,
        }

        row = self.session.execute(insert_stmt, params).mappings().first()
        if row is not None:
            return UUID(str(row["id"])), True

        # Existing receipt lookup
        find_stmt = text(
            """
            SELECT id FROM public.provider_receipts
            WHERE workspace_id = :workspace_id
              AND provider = :provider
              AND event_identity = :event_identity
            LIMIT 1
            """
        )
        existing = self.session.execute(
            find_stmt,
            {
                "workspace_id": str(workspace_id),
                "provider": provider.upper(),
                "event_identity": event_identity,
            },
        ).first()
        assert existing is not None, "Receipt must exist if conflict occurred"
        return UUID(str(existing[0])), False

    def get_receipt(self, receipt_id: UUID) -> Mapping[str, Any] | None:
        stmt = text(
            """
            SELECT * FROM public.provider_receipts
            WHERE id = :id
            LIMIT 1
            """
        )
        return cast(
            Mapping[str, Any] | None,
            self.session.execute(stmt, {"id": str(receipt_id)}).mappings().first(),
        )

    # -------------------------------------------------------------------------
    # Safety Holds
    # -------------------------------------------------------------------------

    def create_safety_hold(
        self,
        *,
        workspace_id: UUID,
        source_receipt_id: UUID,
        source_work_identity: str,
        target_kind: ScopeKind,
        target_mailbox_id: UUID | None,
        reason: str,
    ) -> UUID:
        """Place an active safety hold on the mailbox or workspace.

        Automatically triggers safety_holds_maintain_safety_counter on PostgreSQL,
        incrementing pending_safety_count on the mailbox or workspace.
        """
        _safe_set_workspace(self.session, workspace_id)

        stmt = text(
            """
            INSERT INTO public.safety_holds (
                workspace_id, source_receipt_id, source_work_identity,
                target_kind, target_mailbox_id, status, reason
            )
            VALUES (
                :workspace_id, :source_receipt_id, :source_work_identity,
                :target_kind, :target_mailbox_id, 'ACTIVE', :reason
            )
            ON CONFLICT (workspace_id, source_work_identity) DO UPDATE
            SET status = 'ACTIVE'
            RETURNING id
            """
        )
        params: dict[str, Any] = {
            "workspace_id": str(workspace_id),
            "source_receipt_id": str(source_receipt_id),
            "source_work_identity": source_work_identity,
            "target_kind": target_kind.value,
            "target_mailbox_id": str(target_mailbox_id) if target_mailbox_id else None,
            "reason": reason[:500],
        }
        row = self.session.execute(stmt, params).mappings().first()
        assert row is not None
        return UUID(str(row["id"]))

    def resolve_safety_hold(
        self,
        *,
        workspace_id: UUID,
        source_receipt_id: UUID,
    ) -> int:
        """Mark active safety hold RESOLVED.

        Automatically triggers safety_holds_maintain_safety_counter on PostgreSQL,
        decrementing pending_safety_count.
        """
        _safe_set_workspace(self.session, workspace_id)

        stmt = text(
            """
            UPDATE public.safety_holds
            SET status = 'RESOLVED',
                resolved_at = pg_catalog.transaction_timestamp()
            WHERE workspace_id = :workspace_id
              AND source_receipt_id = :source_receipt_id
              AND status = 'ACTIVE'
            """
        )
        res = self.session.execute(
            stmt,
            {
                "workspace_id": str(workspace_id),
                "source_receipt_id": str(source_receipt_id),
            },
        )
        return res.rowcount

    # -------------------------------------------------------------------------
    # Receipt Lifecycle & Processing Leases
    # -------------------------------------------------------------------------

    def claim_receipt_for_processing(
        self,
        *,
        receipt_id: UUID,
        worker_owner: str,
        lease_duration: timedelta = timedelta(seconds=60),
        now: datetime | None = None,
    ) -> Mapping[str, Any] | None:
        """Atomically acquire short database lease for receipt processing."""
        now = now or datetime.now(UTC)
        lease_expires_at = now + lease_duration

        bind = self.session.get_bind()
        is_sqlite = bind and getattr(bind.dialect, "name", "") == "sqlite"
        lock_clause = "" if is_sqlite else "FOR UPDATE SKIP LOCKED"

        # Check current state under row lock
        select_stmt = text(
            f"""
            SELECT * FROM public.provider_receipts
            WHERE id = :id
            {lock_clause}
            """
        )
        receipt = self.session.execute(select_stmt, {"id": str(receipt_id)}).mappings().first()
        if receipt is None:
            return None

        status = receipt["receipt_status"]
        if status == ReceiptStatus.PROCESSED.value:
            # Already completed; safe no-op
            return receipt

        # If currently in PROCESSING, verify lease expiry
        if status == ReceiptStatus.PROCESSING.value:
            expires = receipt["lease_expires_at"]
            if expires is not None:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if expires > now:
                    # Still actively owned by another worker
                    return None

        # Claim the lease
        update_stmt = text(
            """
            UPDATE public.provider_receipts
            SET receipt_status = 'PROCESSING',
                lease_owner = :worker_owner,
                lease_generation = lease_generation + 1,
                lease_expires_at = :lease_expires_at,
                next_due_at = :lease_expires_at
            WHERE id = :id
              AND version = :version
            RETURNING *
            """
        )
        claimed = self.session.execute(
            update_stmt,
            {
                "id": str(receipt_id),
                "worker_owner": worker_owner,
                "lease_expires_at": lease_expires_at,
                "version": receipt["version"],
            },
        ).mappings().first()

        return claimed

    def mark_receipt_processed(self, receipt_id: UUID) -> None:
        stmt = text(
            """
            UPDATE public.provider_receipts
            SET receipt_status = 'PROCESSED',
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = :id
            """
        )
        self.session.execute(stmt, {"id": str(receipt_id)})

    def mark_receipt_failed(self, receipt_id: UUID, error: str) -> None:
        stmt = text(
            """
            UPDATE public.provider_receipts
            SET receipt_status = 'FAILED',
                safe_error = :error,
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = :id
            """
        )
        self.session.execute(stmt, {"id": str(receipt_id), "error": error[:500]})

    def mark_receipt_retry(
        self,
        receipt_id: UUID,
        *,
        retry_count: int,
        next_due_at: datetime,
        error: str,
    ) -> None:
        stmt = text(
            """
            UPDATE public.provider_receipts
            SET receipt_status = 'RETRY',
                retry_count = :retry_count,
                next_due_at = :next_due_at,
                safe_error = :error,
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = :id
            """
        )
        self.session.execute(
            stmt,
            {
                "id": str(receipt_id),
                "retry_count": retry_count,
                "next_due_at": next_due_at,
                "error": error[:500],
            },
        )

    def recover_stale_processing_receipts(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> int:
        now = now or datetime.now(UTC)
        bind = self.session.get_bind()
        is_sqlite = bind and getattr(bind.dialect, "name", "") == "sqlite"
        lock_clause = "" if is_sqlite else "FOR UPDATE SKIP LOCKED"

        candidates_stmt = text(
            f"""
            SELECT id, version FROM public.provider_receipts
            WHERE receipt_status = 'PROCESSING'
              AND lease_expires_at <= :now
            ORDER BY lease_expires_at ASC
            LIMIT :limit
            {lock_clause}
            """
        )
        rows = self.session.execute(candidates_stmt, {"now": now, "limit": limit}).mappings().all()
        if not rows:
            return 0

        recovered = 0
        for row in rows:
            res = self.session.execute(
                text(
                    """
                    UPDATE public.provider_receipts
                    SET receipt_status = 'RETRY',
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE id = :id
                      AND version = :version
                    """
                ),
                {"id": str(row["id"]), "version": row["version"]},
            )
            if res.rowcount > 0:
                recovered += 1

        self.session.commit()
        return recovered

    # -------------------------------------------------------------------------
    # Recipient Addresses & Suppression Operations
    # -------------------------------------------------------------------------

    def ensure_recipient_address(
        self,
        *,
        workspace_id: UUID,
        canonical_address: str,
        original_display: str | None = None,
    ) -> UUID:
        """Find or insert normalized recipient address record."""
        _safe_set_workspace(self.session, workspace_id)

        find_stmt = text(
            """
            SELECT id FROM public.recipient_addresses
            WHERE workspace_id = :workspace_id
              AND canonical_address = :canonical
              AND normalization_version = 1
            LIMIT 1
            """
        )
        row = self.session.execute(
            find_stmt,
            {"workspace_id": str(workspace_id), "canonical": canonical_address},
        ).first()
        if row is not None:
            return UUID(str(row[0]))

        insert_stmt = text(
            """
            INSERT INTO public.recipient_addresses (
                workspace_id, canonical_address, original_display,
                normalization_version
            )
            VALUES (
                :workspace_id, :canonical, :display, 1
            )
            ON CONFLICT (workspace_id, canonical_address, normalization_version)
            DO UPDATE SET updated_at = pg_catalog.transaction_timestamp()
            RETURNING id
            """
        )
        res = self.session.execute(
            insert_stmt,
            {
                "workspace_id": str(workspace_id),
                "canonical": canonical_address,
                "display": original_display or canonical_address,
            },
        ).mappings().first()
        assert res is not None
        return UUID(str(res["id"]))

    def upsert_suppression(
        self,
        *,
        workspace_id: UUID,
        address_id: UUID,
        reason: str,
        provider_receipt_id: UUID | None = None,
        domain_event_id: UUID | None = None,
        source_key: str,
        evidence: dict[str, Any] | None = None,
    ) -> tuple[UUID, bool]:
        """Durable address suppression and evidence source creation.

        Returns (suppression_id, was_new_prohibition).
        """
        _safe_set_workspace(self.session, workspace_id)

        upsert_stmt = text(
            """
            INSERT INTO public.suppressions (
                workspace_id, address_id, reason, status,
                first_observed_at, last_observed_at
            )
            VALUES (
                :workspace_id, :address_id, :reason, 'ACTIVE',
                pg_catalog.transaction_timestamp(), pg_catalog.transaction_timestamp()
            )
            -- version is maintained by suppressions_touch_row; the worker role has
            -- no UPDATE grant on it, so assigning it here fails with permission denied.
            ON CONFLICT (workspace_id, address_id, reason) DO UPDATE
            SET status = 'ACTIVE',
                last_observed_at = pg_catalog.transaction_timestamp(),
                released_at = NULL,
                release_actor_id = NULL,
                release_audit_id = NULL
            RETURNING id, (xmax = 0) AS was_insert
            """
        )
        row = self.session.execute(
            upsert_stmt,
            {
                "workspace_id": str(workspace_id),
                "address_id": str(address_id),
                "reason": reason,
            },
        ).mappings().first()

        if row is not None:
            suppression_id = UUID(str(row["id"]))
            was_new = bool(row["was_insert"])
        else:
            find_stmt = text(
                """
                SELECT id FROM public.suppressions
                WHERE workspace_id = :workspace_id
                  AND address_id = :address_id
                  AND reason = :reason
                """
            )
            supp = self.session.execute(
                find_stmt,
                {
                    "workspace_id": str(workspace_id),
                    "address_id": str(address_id),
                    "reason": reason,
                },
            ).first()
            assert supp is not None
            suppression_id = UUID(str(supp[0]))
            was_new = False

        # Persist suppression source evidence
        # suppression_sources_source_identity_check requires the key of a
        # PROVIDER_RECEIPT / DOMAIN_EVENT source to be the id of that row.
        stored_source_key = source_key[:200]
        if provider_receipt_id:
            source_kind = "PROVIDER_RECEIPT"
            stored_source_key = str(provider_receipt_id)
        elif domain_event_id:
            source_kind = "DOMAIN_EVENT"
            stored_source_key = str(domain_event_id)
        else:
            source_kind = "UNSUBSCRIBE_TOKEN"
        source_stmt = text(
            """
            INSERT INTO public.suppression_sources (
                workspace_id, suppression_id, source_kind, source_key,
                provider_receipt_id, domain_event_id, evidence
            )
            VALUES (
                :workspace_id, :suppression_id, :source_kind, :source_key,
                :provider_receipt_id, :domain_event_id, CAST(:evidence AS jsonb)
            )
            ON CONFLICT (workspace_id, suppression_id, source_kind, source_key) DO NOTHING
            """
        )
        self.session.execute(
            source_stmt,
            {
                "workspace_id": str(workspace_id),
                "suppression_id": str(suppression_id),
                "source_kind": source_kind,
                "source_key": stored_source_key,
                "provider_receipt_id": str(provider_receipt_id) if provider_receipt_id else None,
                "domain_event_id": str(domain_event_id) if domain_event_id else None,
                "evidence": json.dumps(evidence or {}),
            },
        )

        return suppression_id, was_new

    # -------------------------------------------------------------------------
    # Campaign & Message Invalidation Side Effects
    # -------------------------------------------------------------------------

    def stop_active_enrollments(
        self,
        *,
        workspace_id: UUID,
        address_id: UUID,
        stop_reason: str,
    ) -> Sequence[UUID]:
        """Transition active enrollments for this suppressed address to STOPPED."""
        _safe_set_workspace(self.session, workspace_id)

        stmt = text(
            """
            UPDATE public.campaign_enrollments
            SET state = 'STOPPED',
                stop_reason = :stop_reason,
                next_step_id = NULL,
                version = version + 1
            WHERE workspace_id = :workspace_id
              AND address_id = :address_id
              AND state = 'ACTIVE'
            RETURNING id
            """
        )
        rows = self.session.execute(
            stmt,
            {
                "workspace_id": str(workspace_id),
                "address_id": str(address_id),
                "stop_reason": stop_reason,
            },
        ).mappings().all()

        return [UUID(str(r["id"])) for r in rows]

    def cancel_non_terminal_messages(
        self,
        *,
        workspace_id: UUID,
        address_id: UUID,
        terminal_reason: str,
    ) -> int:
        """Cancel/Skip planned, scheduled, and queued messages for this address.

        In-flight attempts (SENDING, UNKNOWN_OUTCOME) or already terminal
        messages (SENT, FAILED, SKIPPED, CANCELLED) are untouched.
        """
        _safe_set_workspace(self.session, workspace_id)

        stmt = text(
            """
            UPDATE public.messages
            SET status = 'SKIPPED',
                terminal_reason = :terminal_reason,
                version = version + 1
            WHERE workspace_id = :workspace_id
              AND address_id = :address_id
              AND status IN ('PLANNED', 'SCHEDULED', 'QUEUED', 'RETRY_SCHEDULED')
            """
        )
        res = self.session.execute(
            stmt,
            {
                "workspace_id": str(workspace_id),
                "address_id": str(address_id),
                "terminal_reason": terminal_reason[:500],
            },
        )
        return res.rowcount

    def record_recipient_outcome(
        self,
        *,
        workspace_id: UUID,
        enrollment_id: UUID,
        kind: str,
        source_key: str,
        occurred_at: datetime,
    ) -> None:
        """Durable recipient outcome record (REPLIED, UNSUBSCRIBED, HARD_BOUNCE, COMPLAINT)."""
        _safe_set_workspace(self.session, workspace_id)

        stmt = text(
            """
            INSERT INTO public.recipient_outcomes (
                workspace_id, enrollment_id, kind, source_key, occurred_at
            )
            VALUES (
                :workspace_id, :enrollment_id, :kind, :source_key, :occurred_at
            )
            ON CONFLICT (workspace_id, enrollment_id, kind, source_key) DO NOTHING
            """
        )
        self.session.execute(
            stmt,
            {
                "workspace_id": str(workspace_id),
                "enrollment_id": str(enrollment_id),
                "kind": kind,
                "source_key": source_key[:200],
                "occurred_at": occurred_at,
            },
        )

    def record_domain_event(
        self,
        *,
        workspace_id: UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        semantic_key: str,
        occurred_at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> UUID | None:
        """Durable append-only domain event. Returns the row id (existing row
        when the semantic key was already recorded)."""
        _safe_set_workspace(self.session, workspace_id)

        stmt = text(
            """
            INSERT INTO public.domain_events (
                workspace_id, event_type, aggregate_type, aggregate_id,
                semantic_key, occurred_at, payload
            )
            VALUES (
                :workspace_id, :event_type, :aggregate_type, :aggregate_id,
                :semantic_key, :occurred_at, CAST(:payload AS jsonb)
            )
            ON CONFLICT (workspace_id, event_type, semantic_key) DO NOTHING
            """
        )
        self.session.execute(
            stmt,
            {
                "workspace_id": str(workspace_id),
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": str(aggregate_id),
                "semantic_key": semantic_key[:200],
                "occurred_at": occurred_at,
                "payload": json.dumps(payload or {}),
            },
        )
        found = self.session.execute(
            text(
                """
                SELECT id FROM public.domain_events
                WHERE workspace_id = :workspace_id
                  AND event_type = :event_type
                  AND semantic_key = :semantic_key
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "event_type": event_type,
                "semantic_key": semantic_key[:200],
            },
        ).first()
        return UUID(str(found[0])) if found else None

    # -------------------------------------------------------------------------
    # Message-level bounce evidence
    # -------------------------------------------------------------------------

    def find_message_for_bounce(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID | None,
        original_message_ids: Sequence[str],
        referenced_message_ids: Sequence[str],
        provider_message_id: str | None,
        recipient_email: str | None,
        before: datetime,
        window_days: int = 14,
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        """Find the SENT campaign message a bounce refers to.

        Deterministic, strongest evidence first: the bounced email's own
        Message-ID / provider id, then earlier messages named in the returned
        headers, then (weakest, flagged as such) the most recent message sent
        to that recipient from the same mailbox before the bounce arrived.
        Returns (row, evidence) or (None, None); never guesses across ambiguity
        at the identifier stages.
        """
        _safe_set_workspace(self.session, workspace_id)
        is_sqlite = self.session.get_bind().dialect.name == "sqlite"
        rfc_expr = sql_normalized_message_id("m.rfc_message_id", sqlite=is_sqlite)
        mailbox_clause = "AND m.mailbox_id = :mailbox_id" if mailbox_id else ""
        base_params: dict[str, Any] = {"ws": str(workspace_id)}
        if mailbox_id:
            base_params["mailbox_id"] = str(mailbox_id)

        select = (
            "SELECT m.id, m.enrollment_id, m.address_id, m.campaign_id, m.mailbox_id, "
            "m.frozen_destination FROM public.messages m "
            "WHERE m.workspace_id = :ws AND m.purpose = 'CAMPAIGN' "
            "AND m.status = 'SENT' " + mailbox_clause
        )

        def by_ids(raw_ids: Sequence[str], evidence: str) -> tuple[Mapping[str, Any] | None, str | None]:
            wanted = [i.strip().strip("<>").strip().lower() for i in raw_ids if i and i.strip()]
            if not wanted:
                return None, None
            placeholders = ",".join(f":id_{n}" for n in range(len(wanted)))
            params = dict(base_params)
            params.update({f"id_{n}": v for n, v in enumerate(wanted)})
            rows = self.session.execute(
                text(f"{select} AND {rfc_expr} IN ({placeholders}) LIMIT 2"), params
            ).mappings().all()
            return (dict(rows[0]), evidence) if len(rows) == 1 else (None, None)

        found, evidence = by_ids(original_message_ids, "MESSAGE_ID")
        if found:
            return found, evidence

        if provider_message_id:
            params = dict(base_params, pmid=provider_message_id)
            rows = self.session.execute(
                text(f"{select} AND m.provider_message_id = :pmid LIMIT 2"), params
            ).mappings().all()
            if len(rows) == 1:
                return dict(rows[0]), "PROVIDER_MESSAGE_ID"

        found, evidence = by_ids(referenced_message_ids, "REFERENCED_MESSAGE_ID")
        if found:
            return found, evidence

        # Recipient-only association is the weakest evidence and is used only
        # when the notification carried NO message identifiers at all. Identifiers
        # that match nothing are contradicting evidence (e.g. a forged report),
        # not a reason to guess from the recipient.
        had_identifiers = bool(
            [i for i in (*original_message_ids, *referenced_message_ids) if i and i.strip()]
        )
        if recipient_email and not had_identifiers:
            params = dict(
                base_params,
                rcpt=recipient_email.strip().lower(),
                before=before,
                since=before - timedelta(days=window_days),
            )
            row = self.session.execute(
                text(
                    f"{select} AND LOWER(m.frozen_destination) = :rcpt "
                    "AND m.accepted_at <= :before AND m.accepted_at >= :since "
                    "ORDER BY m.accepted_at DESC LIMIT 1"
                ),
                params,
            ).mappings().first()
            if row:
                return dict(row), "RECIPIENT_RECENT"
        return None, None

    def upsert_message_bounce(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        bounce_type: str,
        bounce_code: str | None,
        source: str,
        detail: str | None,
        occurred_at: datetime,
    ) -> None:
        """Record that a message bounced. One row per message: a repeated or
        delayed notification only bumps the counters, and a HARD report is never
        downgraded by a later SOFT one."""
        _safe_set_workspace(self.session, workspace_id)
        self.session.execute(
            text(
                """
                INSERT INTO public.message_events (
                    id, workspace_id, message_id, kind, bounce_type, bounce_code,
                    source, detail, first_occurred_at, last_occurred_at,
                    occurrence_count
                ) VALUES (
                    :id, :ws, :mid, 'BOUNCED', :bt, :code,
                    :source, :detail, :at, :at, 1
                )
                ON CONFLICT (workspace_id, message_id, kind) DO UPDATE SET
                    occurrence_count = message_events.occurrence_count + 1,
                    last_occurred_at = CASE
                        WHEN EXCLUDED.last_occurred_at > message_events.last_occurred_at
                        THEN EXCLUDED.last_occurred_at
                        ELSE message_events.last_occurred_at END,
                    bounce_type = CASE
                        WHEN message_events.bounce_type = 'HARD'
                             OR EXCLUDED.bounce_type = 'HARD' THEN 'HARD'
                        WHEN message_events.bounce_type = 'SOFT'
                             OR EXCLUDED.bounce_type = 'SOFT' THEN 'SOFT'
                        ELSE 'UNKNOWN' END,
                    bounce_code = COALESCE(EXCLUDED.bounce_code, message_events.bounce_code)
                """
            ),
            {
                "id": str(uuid4()),
                "ws": str(workspace_id),
                "mid": str(message_id),
                "bt": bounce_type,
                "code": bounce_code,
                "source": source[:50],
                "detail": detail[:300] if detail else None,
                "at": occurred_at,
            },
        )
