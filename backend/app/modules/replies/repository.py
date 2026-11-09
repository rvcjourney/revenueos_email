from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.mailboxes.providers.message_builder import sql_normalized_message_id
from app.modules.replies.matcher import OutboundMessageCandidate
from app.modules.replies.schemas import NormalizedInboundMessage

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


class ReplyRepository:
    """Database repository for reply sync, message correlation, and campaign safety.
    Operates under the app_worker_sync database role.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def _is_sqlite(self) -> bool:
        bind = self.session.get_bind()
        return bool(bind and getattr(bind.dialect, "name", "") == "sqlite")

    # -------------------------------------------------------------------------
    # Sync State & Lease Management
    # -------------------------------------------------------------------------

    def get_sync_state(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        sync_scope: str = "INBOX",
    ) -> dict[str, Any] | None:
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        query = text(
            """
            SELECT id, workspace_id, mailbox_id, connection_generation, sync_scope,
                   cursor_data, lease_owner, lease_generation, lease_expires_at,
                   next_due_at, last_complete_at, status, failure_count, version
            FROM public.mailbox_sync_states
            WHERE workspace_id = :ws AND mailbox_id = :mbid AND sync_scope = :scope
            """
        )
        row = self.session.execute(
            query,
            {"ws": str(workspace_id), "mbid": str(mailbox_id), "scope": sync_scope},
        ).mappings().first()
        return dict(row) if row else None

    def ensure_sync_state(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        connection_generation: int,
        sync_scope: str = "INBOX",
    ) -> dict[str, Any]:
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        existing = self.get_sync_state(
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            sync_scope=sync_scope,
        )
        if existing:
            return existing

        # version/created_at/updated_at are DB-managed (column defaults plus the
        # mailbox_sync_states_touch_row trigger); app_worker_sync has no grant
        # on them (migrations 0003/0015), so they must not appear in this SQL.
        now = datetime.now(UTC)
        insert_query = text(
            """
            INSERT INTO public.mailbox_sync_states (
                workspace_id, mailbox_id, connection_generation, sync_scope,
                cursor_data, lease_owner, lease_generation, lease_expires_at,
                next_due_at, status, failure_count
            ) VALUES (
                :ws, :mbid, :gen, :scope,
                NULL, NULL, 1, NULL,
                :now, 'INITIALIZING', 0
            )
            ON CONFLICT (workspace_id, mailbox_id, sync_scope) DO NOTHING
            """
        )
        self.session.execute(
            insert_query,
            {
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "gen": connection_generation,
                "scope": sync_scope,
                "now": now,
            },
        )
        state = self.get_sync_state(
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            sync_scope=sync_scope,
        )
        if not state:
            raise RuntimeError(f"Failed to ensure sync state for mailbox {mailbox_id}")
        return state

    def discover_mailboxes_needing_sync_state(
        self, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """CONNECTED mailboxes with no sync state, or whose UNAVAILABLE state
        predates their current connection (a reconnect or a changed IMAP
        configuration may have made reply sync possible).

        Cross-workspace discovery, so it runs as app_scheduler (read-only
        discovery policies, migrations 0011/0019); writes happen afterwards as
        app_worker_sync inside each row's workspace.
        """
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)
        rows = self.session.execute(
            text(
                """
                SELECT m.workspace_id, m.id AS mailbox_id, m.provider,
                       m.current_connection_generation, s.id AS sync_state_id
                FROM public.mailboxes m
                LEFT JOIN public.mailbox_sync_states s
                  ON s.workspace_id = m.workspace_id AND s.mailbox_id = m.id
                 AND s.sync_scope = 'INBOX'
                WHERE m.connection_state = 'CONNECTED'
                  AND (
                        s.id IS NULL
                        OR (s.status = 'UNAVAILABLE'
                            AND s.connection_generation < m.current_connection_generation)
                      )
                ORDER BY m.created_at ASC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
        return [dict(r) for r in rows]

    def rearm_sync_state(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        connection_generation: int,
        sync_scope: str = "INBOX",
    ) -> None:
        """Make an UNAVAILABLE sync state eligible again after a reconnect."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)
        self.session.execute(
            text(
                """
                UPDATE public.mailbox_sync_states
                SET status = 'INITIALIZING',
                    connection_generation = :gen,
                    next_due_at = :now,
                    failure_count = 0
                WHERE workspace_id = :ws AND mailbox_id = :mbid AND sync_scope = :scope
                  AND status = 'UNAVAILABLE'
                """
            ),
            {
                "gen": connection_generation,
                "now": datetime.now(UTC),
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "scope": sync_scope,
            },
        )

    def mark_sync_unavailable(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        lease_owner: str | None = None,
        sync_scope: str = "INBOX",
    ) -> None:
        """Park reply sync for a mailbox that cannot sync (missing scope, no
        IMAP settings, not connected). Not retried until its connection
        generation changes (see discover_mailboxes_needing_sync_state)."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)
        self.session.execute(
            text(
                """
                UPDATE public.mailbox_sync_states
                SET status = 'UNAVAILABLE',
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE workspace_id = :ws AND mailbox_id = :mbid AND sync_scope = :scope
                """
            ),
            {"ws": str(workspace_id), "mbid": str(mailbox_id), "scope": sync_scope},
        )
        self._set_mailbox_sync_state(workspace_id, mailbox_id, "UNAVAILABLE")

    def _set_mailbox_sync_state(
        self, workspace_id: UUID, mailbox_id: UUID, state: str
    ) -> None:
        """Mirror the sync state onto the mailbox row shown in the UI."""
        self.session.execute(
            text(
                """
                UPDATE public.mailboxes SET sync_state = :state
                WHERE workspace_id = :ws AND id = :mbid AND sync_state <> :state
                """
            ),
            {"state": state, "ws": str(workspace_id), "mbid": str(mailbox_id)},
        )

    def acquire_sync_lease(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        lease_owner: str,
        lease_duration_seconds: int = 120,
        sync_scope: str = "INBOX",
    ) -> dict[str, Any] | None:
        """Atomically claim a sync lease on mailbox_sync_states if due or expired."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lease_duration_seconds)
        is_sqlite = self._is_sqlite()
        lock_clause = "" if is_sqlite else "FOR UPDATE SKIP LOCKED"

        # Check existing and lock
        select_query = text(
            f"""
            SELECT id, workspace_id, mailbox_id, connection_generation, sync_scope,
                   cursor_data, lease_owner, lease_generation, lease_expires_at,
                   next_due_at, status, failure_count, version
            FROM public.mailbox_sync_states
            WHERE workspace_id = :ws AND mailbox_id = :mbid AND sync_scope = :scope
            {lock_clause}
            """
        )
        row = self.session.execute(
            select_query,
            {"ws": str(workspace_id), "mbid": str(mailbox_id), "scope": sync_scope},
        ).mappings().first()

        if not row:
            return None

        # Verify lease availability
        current_owner = row["lease_owner"]
        lease_expiry = row["lease_expires_at"]
        if isinstance(lease_expiry, str):
            try:
                lease_expiry = datetime.fromisoformat(lease_expiry)
            except Exception:
                lease_expiry = None
        if lease_expiry and lease_expiry.tzinfo is None:
            lease_expiry = lease_expiry.replace(tzinfo=UTC)

        # A lease already held by this exact owner is the scheduler's hand-off:
        # claim_due_sync_states leases the row under a unique per-claim owner
        # and passes that owner to the mailbox.sync task, which must be able to
        # take over. Any other unexpired owner still blocks.
        is_free = (
            current_owner is None
            or lease_expiry is None
            or lease_expiry <= now
            or current_owner == lease_owner
        )
        if not is_free:
            # Locked by another active worker
            return None

        # Update lease (version/updated_at are maintained by the touch trigger)
        update_query = text(
            """
            UPDATE public.mailbox_sync_states
            SET lease_owner = :owner,
                lease_expires_at = :expires_at,
                lease_generation = lease_generation + 1
            WHERE workspace_id = :ws AND mailbox_id = :mbid AND sync_scope = :scope
              AND version = :ver
            """
        )
        res = self.session.execute(
            update_query,
            {
                "owner": lease_owner,
                "expires_at": expires_at,
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "scope": sync_scope,
                "ver": row["version"],
            },
        )
        if res.rowcount == 0:
            return None

        # Return refreshed state
        updated = dict(row)
        updated["lease_owner"] = lease_owner
        updated["lease_expires_at"] = expires_at
        updated["lease_generation"] = row["lease_generation"] + 1
        updated["version"] = row["version"] + 1
        return updated

    def advance_sync_checkpoint(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        cursor_data: str,
        status: str = "CURRENT",
        next_due_at: datetime | None = None,
        sync_scope: str = "INBOX",
    ) -> None:
        """Durable sync checkpoint advance upon successful page or sync completion."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        now = datetime.now(UTC)
        query = text(
            """
            UPDATE public.mailbox_sync_states
            SET cursor_data = :cursor_data,
                status = :status,
                last_complete_at = :now,
                next_due_at = COALESCE(:next_due_at, next_due_at),
                failure_count = 0
            WHERE workspace_id = :ws AND mailbox_id = :mbid AND sync_scope = :scope
            """
        )
        self.session.execute(
            query,
            {
                "cursor_data": cursor_data,
                "status": status,
                "now": now,
                "next_due_at": next_due_at,
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "scope": sync_scope,
            },
        )
        self._set_mailbox_sync_state(workspace_id, mailbox_id, status)

    def release_sync_lease(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        lease_owner: str,
        next_due_at: datetime | None = None,
        failure: bool = False,
        sync_scope: str = "INBOX",
    ) -> None:
        """Release worker lease ownership, optionally recording a failure count."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        failure_incr = 1 if failure else 0
        query = text(
            """
            UPDATE public.mailbox_sync_states
            SET lease_owner = NULL,
                lease_expires_at = NULL,
                failure_count = failure_count + :failure_incr,
                next_due_at = COALESCE(:next_due_at, next_due_at)
            WHERE workspace_id = :ws AND mailbox_id = :mbid AND sync_scope = :scope
              AND lease_owner = :owner
            """
        )
        self.session.execute(
            query,
            {
                "failure_incr": failure_incr,
                "next_due_at": next_due_at,
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "scope": sync_scope,
                "owner": lease_owner,
            },
        )

    def mark_resync_required(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        sync_scope: str = "INBOX",
    ) -> None:
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        query = text(
            """
            UPDATE public.mailbox_sync_states
            SET status = 'RESYNC_REQUIRED',
                cursor_data = NULL
            WHERE workspace_id = :ws AND mailbox_id = :mbid AND sync_scope = :scope
            """
        )
        self.session.execute(
            query,
            {
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "scope": sync_scope,
            },
        )
        self._set_mailbox_sync_state(workspace_id, mailbox_id, "RESYNC_REQUIRED")

    def recover_stale_sync_leases(self, *, now: datetime, batch_size: int = 50) -> int:
        """Recover sync leases whose lease_expires_at has elapsed.

        Discovery is cross-workspace, so it runs as app_scheduler, which has a
        read-only discovery policy on mailbox_sync_states (migration 0019).
        app_worker_sync is scoped to one workspace per transaction and would
        see no rows here. Each recovery is then written as app_worker_sync
        inside that row's workspace, guarded by the row's version.
        """
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)

        stale = self.session.execute(
            text(
                """
                SELECT id, workspace_id, version
                FROM public.mailbox_sync_states
                WHERE lease_owner IS NOT NULL
                  AND lease_expires_at < :now
                ORDER BY lease_expires_at ASC
                LIMIT :limit
                """
            ),
            {"now": now, "limit": batch_size},
        ).mappings().all()

        recovered = 0
        for row in stale:
            _safe_set_role(self.session, "app_worker_sync")
            _safe_set_workspace(self.session, UUID(str(row["workspace_id"])))
            res = self.session.execute(
                text(
                    """
                    UPDATE public.mailbox_sync_states
                    SET lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE id = :id AND workspace_id = :ws AND version = :ver
                      AND lease_owner IS NOT NULL
                      AND lease_expires_at < :now
                    """
                ),
                {
                    "id": row["id"],
                    "ws": str(row["workspace_id"]),
                    "ver": row["version"],
                    "now": now,
                },
            )
            recovered += res.rowcount
        return recovered

    def claim_due_sync_states(
        self,
        *,
        lease_owner: str,
        now: datetime,
        limit: int = 20,
        lease_duration_seconds: int = 120,
        sync_scope: str = "INBOX",
    ) -> list[dict[str, Any]]:
        """Find and claim due sync states across mailboxes.

        Discovery is cross-workspace, so it runs as app_scheduler (read-only
        discovery policy and a column-limited grant that excludes cursor_data,
        migration 0019). Each claim is then written as app_worker_sync inside
        that row's workspace, guarded by the row's version so concurrent
        claimers cannot both win. Every claim gets its own owner token, which
        the dispatcher passes to the mailbox.sync task; acquire_sync_lease lets
        that same owner take over the lease.
        """
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)

        expires_at = now + timedelta(seconds=lease_duration_seconds)

        select_query = text(
            """
            SELECT id, workspace_id, mailbox_id, connection_generation, sync_scope,
                   lease_owner, lease_generation, lease_expires_at,
                   next_due_at, status, failure_count, version
            FROM public.mailbox_sync_states
            WHERE sync_scope = :scope
              AND status != 'UNAVAILABLE'
              AND (next_due_at IS NULL OR next_due_at <= :now)
              AND (lease_owner IS NULL OR lease_expires_at < :now)
            ORDER BY next_due_at ASC NULLS FIRST
            LIMIT :limit
            """
        )
        rows = self.session.execute(
            select_query,
            {"scope": sync_scope, "now": now, "limit": limit},
        ).mappings().all()

        claimed: list[dict[str, Any]] = []
        for r in rows:
            claim_owner = f"{lease_owner}:{uuid4().hex}"
            _safe_set_role(self.session, "app_worker_sync")
            _safe_set_workspace(self.session, UUID(str(r["workspace_id"])))
            update_query = text(
                """
                UPDATE public.mailbox_sync_states
                SET lease_owner = :owner,
                    lease_expires_at = :expires_at,
                    lease_generation = lease_generation + 1
                WHERE id = :id AND workspace_id = :ws AND version = :ver
                  AND (lease_owner IS NULL OR lease_expires_at < :now)
                """
            )
            res = self.session.execute(
                update_query,
                {
                    "owner": claim_owner,
                    "expires_at": expires_at,
                    "now": now,
                    "id": r["id"],
                    "ws": str(r["workspace_id"]),
                    "ver": r["version"],
                },
            )
            if res.rowcount > 0:
                item = dict(r)
                item["lease_owner"] = claim_owner
                item["lease_expires_at"] = expires_at
                claimed.append(item)

        return claimed

    # -------------------------------------------------------------------------
    # Outbound Message Candidate Loading
    # -------------------------------------------------------------------------

    def load_outbound_candidates(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        rfc_message_ids: list[str],
        sender_address: str,
        provider_thread_id: str | None = None,
        limit: int = 500,
    ) -> list[OutboundMessageCandidate]:
        """Load candidate outbound messages that could match an inbound reply.

        A candidate shares the sender as its recipient, or quotes one of the
        Message-IDs, or sits in the same provider thread. The matcher then
        applies the strict rules; this only bounds what it looks at. Message-ID
        comparison ignores angle brackets and case, and the provider thread is
        read from the send attempt (where the provider reported it).
        """
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        clean_sender = sender_address.strip().lower() if sender_address else ""
        wanted_ids = [
            i.strip().strip("<>").strip().lower()
            for i in rfc_message_ids
            if i and i.strip().strip("<>").strip()
        ]
        rfc_expr = sql_normalized_message_id(
            "m.rfc_message_id", sqlite=self._is_sqlite()
        )
        # Latest attempt that recorded a provider thread for this message.
        attempt_thread = (
            "(SELECT a.provider_thread_ref FROM public.message_attempts a "
            "WHERE a.workspace_id = m.workspace_id AND a.message_id = m.id "
            "AND a.provider_thread_ref IS NOT NULL "
            "ORDER BY a.ordinal DESC LIMIT 1)"
        )

        conditions = ["m.workspace_id = :ws", "m.mailbox_id = :mbid", "m.purpose = 'CAMPAIGN'"]
        or_clauses = []
        params: dict[str, Any] = {"ws": str(workspace_id), "mbid": str(mailbox_id)}

        if clean_sender:
            or_clauses.append("LOWER(m.frozen_destination) = :sender")
            params["sender"] = clean_sender

        if wanted_ids:
            placeholders = [f":rfc_{i}" for i in range(len(wanted_ids))]
            or_clauses.append(f"{rfc_expr} IN ({','.join(placeholders)})")
            for i, rfc in enumerate(wanted_ids):
                params[f"rfc_{i}"] = rfc

        if provider_thread_id:
            or_clauses.append(
                f"(c.provider_thread_id = :thread_id OR {attempt_thread} = :thread_id)"
            )
            params["thread_id"] = provider_thread_id

        if not or_clauses:
            return []

        conditions.append(f"({' OR '.join(or_clauses)})")
        where_sql = " AND ".join(conditions)
        params["limit"] = limit

        query = text(
            f"""
            SELECT m.id AS message_id, m.campaign_id, m.enrollment_id, m.mailbox_id,
                   m.rfc_message_id, m.provider_message_id, m.frozen_destination,
                   COALESCE(c.provider_thread_id, {attempt_thread}) AS provider_thread_id,
                   m.created_at
            FROM public.messages m
            LEFT JOIN public.conversations c ON c.workspace_id = m.workspace_id AND c.id = m.conversation_id
            WHERE {where_sql}
            ORDER BY m.created_at DESC
            LIMIT :limit
            """
        )
        rows = self.session.execute(query, params).mappings().all()

        return [
            OutboundMessageCandidate(
                message_id=r["message_id"],
                campaign_id=r["campaign_id"],
                enrollment_id=r["enrollment_id"],
                mailbox_id=r["mailbox_id"],
                rfc_message_id=r["rfc_message_id"],
                provider_message_id=r["provider_message_id"],
                provider_thread_id=r["provider_thread_id"],
                frozen_destination=r["frozen_destination"],
                sent_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
            if r["campaign_id"] and r["enrollment_id"]
        ]

    # -------------------------------------------------------------------------
    # Conversation Find or Create
    # -------------------------------------------------------------------------

    def find_or_create_conversation(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        provider_thread_id: str | None,
        local_anchor_id: str | None,
        campaign_summary_id: UUID | None,
        activity_at: datetime,
    ) -> UUID:
        """Find or create a conversation record scoped to (workspace_id, mailbox_id)."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        # version/updated_at/created_at are DB-managed (defaults plus the
        # conversations_touch_row trigger), and app_worker_sync has no grant on
        # them, so none of the statements below may assign them.

        # 1. Search by provider_thread_id
        if provider_thread_id:
            sel_thread = text(
                """
                SELECT id FROM public.conversations
                WHERE workspace_id = :ws AND mailbox_id = :mbid AND provider_thread_id = :thread
                """
            )
            row = self.session.execute(
                sel_thread,
                {"ws": str(workspace_id), "mbid": str(mailbox_id), "thread": provider_thread_id},
            ).first()
            if row:
                # Update latest activity
                upd = text(
                    """
                    UPDATE public.conversations
                    SET latest_activity_at = GREATEST(latest_activity_at, :act)
                    WHERE id = :id
                    """
                )
                self.session.execute(upd, {"act": activity_at, "id": row[0]})
                return row[0]

        # 2. Search by local_anchor_id
        if local_anchor_id and not provider_thread_id:
            sel_anchor = text(
                """
                SELECT id FROM public.conversations
                WHERE workspace_id = :ws AND mailbox_id = :mbid
                  AND provider_thread_id IS NULL AND local_anchor_id = :anchor
                """
            )
            row = self.session.execute(
                sel_anchor,
                {"ws": str(workspace_id), "mbid": str(mailbox_id), "anchor": local_anchor_id},
            ).first()
            if row:
                upd = text(
                    """
                    UPDATE public.conversations
                    SET latest_activity_at = GREATEST(latest_activity_at, :act)
                    WHERE id = :id
                    """
                )
                self.session.execute(upd, {"act": activity_at, "id": row[0]})
                return row[0]

        # 3. Create new conversation
        conv_id = uuid4()
        ins = text(
            """
            INSERT INTO public.conversations (
                id, workspace_id, mailbox_id, provider_thread_id, local_anchor_id,
                campaign_summary_id, latest_activity_at
            ) VALUES (
                :id, :ws, :mbid, :thread, :anchor,
                :camp, :act
            )
            """
        )
        self.session.execute(
            ins,
            {
                "id": str(conv_id),
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "thread": provider_thread_id,
                "anchor": local_anchor_id if not provider_thread_id else None,
                "camp": str(campaign_summary_id) if campaign_summary_id else None,
                "act": activity_at,
            },
        )
        return conv_id

    # -------------------------------------------------------------------------
    # Inbound Message Ingestion & Deduplication
    # -------------------------------------------------------------------------

    def insert_inbound_message(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        conversation_id: UUID,
        connection_generation: int,
        inbound: NormalizedInboundMessage,
        association_status: str,
    ) -> tuple[UUID | None, bool]:
        """Insert inbound message deduplicated by (workspace_id, mailbox_id, provider_message_id).
        Returns (inbound_message_id, is_duplicate).
        """
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        now = datetime.now(UTC)
        participants = {
            "from": {"address": inbound.from_address, "name": inbound.from_name},
            "to": [{"address": a} for a in inbound.to_addresses],
            "cc": [{"address": a} for a in inbound.cc_addresses],
            "bcc": [{"address": a} for a in inbound.bcc_addresses],
        }

        # Check existing first for idempotency
        check_query = text(
            """
            SELECT id FROM public.inbound_messages
            WHERE workspace_id = :ws AND mailbox_id = :mbid AND provider_message_id = :pmid
            """
        )
        existing = self.session.execute(
            check_query,
            {"ws": str(workspace_id), "mbid": str(mailbox_id), "pmid": inbound.provider_message_id},
        ).first()
        if existing:
            return existing[0], True

        new_id = uuid4()
        refs_str = " ".join(inbound.references) if inbound.references else None

        insert_query = text(
            """
            INSERT INTO public.inbound_messages (
                id, workspace_id, mailbox_id, conversation_id, connection_generation,
                provider_message_id, rfc_message_id, in_reply_to, references_header,
                participants, subject, content_text, received_at, observed_at,
                direction, classification, association_status
            ) VALUES (
                :id, :ws, :mbid, :cid, :gen,
                :pmid, :rfc_id, :in_reply_to, :refs,
                CAST(:participants AS jsonb), :subject, :content_text, :received_at, :observed_at,
                'INBOUND', :classification, :association_status
            )
            ON CONFLICT (workspace_id, mailbox_id, provider_message_id) DO NOTHING
            """
        )
        self.session.execute(
            insert_query,
            {
                "id": str(new_id),
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "cid": str(conversation_id),
                "gen": connection_generation,
                "pmid": inbound.provider_message_id[:1024],
                "rfc_id": inbound.rfc_message_id[:500] if inbound.rfc_message_id else None,
                "in_reply_to": inbound.in_reply_to[:4000] if inbound.in_reply_to else None,
                "refs": refs_str[:16000] if refs_str else None,
                "participants": json.dumps(participants),
                "subject": inbound.subject[:500] if inbound.subject else None,
                "content_text": inbound.content_text[:50000] if inbound.content_text else None,
                "received_at": inbound.received_at,
                "observed_at": now,
                "classification": inbound.classification,
                "association_status": association_status,
            },
        )

        # Confirm ID
        row = self.session.execute(
            check_query,
            {"ws": str(workspace_id), "mbid": str(mailbox_id), "pmid": inbound.provider_message_id},
        ).first()
        return (row[0], False) if row else (None, False)

    # -------------------------------------------------------------------------
    # Inbound Outreach Linkage
    # -------------------------------------------------------------------------

    def insert_outreach_link(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        inbound_message_id: UUID,
        outbound_message_id: UUID,
        campaign_id: UUID,
        enrollment_id: UUID,
        evidence_type: str,
        confidence: str,
        status: str = "CONFIRMED",
        matched_at: datetime | None = None,
    ) -> UUID:
        """Insert link in inbound_outreach_links.
        Ensures foreign keys and attribution trigger constraints are satisfied.
        """
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        now = datetime.now(UTC)
        effective_matched_at = matched_at or (now if status == "CONFIRMED" else None)
        link_id = uuid4()

        insert_query = text(
            """
            INSERT INTO public.inbound_outreach_links (
                id, workspace_id, mailbox_id, inbound_message_id, outbound_message_id,
                campaign_id, enrollment_id, evidence_type, confidence,
                status, matched_at
            ) VALUES (
                :id, :ws, :mbid, :inbound_id, :outbound_id,
                :camp_id, :enr_id, :evidence, :conf,
                :status, :matched_at
            )
            ON CONFLICT (workspace_id, inbound_message_id, outbound_message_id)
            DO UPDATE SET
                status = EXCLUDED.status,
                matched_at = EXCLUDED.matched_at
            """
        )
        self.session.execute(
            insert_query,
            {
                "id": str(link_id),
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "inbound_id": str(inbound_message_id),
                "outbound_id": str(outbound_message_id),
                "camp_id": str(campaign_id),
                "enr_id": str(enrollment_id),
                "evidence": evidence_type[:100],
                "conf": confidence,
                "status": status,
                "matched_at": effective_matched_at,
            },
        )
        return link_id

    # -------------------------------------------------------------------------
    # Campaign Safety Execution (Enrollment Stop & Message Cancellation)
    # -------------------------------------------------------------------------

    def record_reply_outcome_and_stop_campaign(
        self,
        *,
        workspace_id: UUID,
        enrollment_id: UUID,
        campaign_id: UUID,
        inbound_message_id: UUID,
        provider_message_id: str,
        occurred_at: datetime,
    ) -> tuple[int, int]:
        """Atomically:
        1. Record recipient_outcomes (kind = 'REPLIED')
        2. Stop campaign enrollment (state = 'STOPPED', stop_reason = 'REPLIED', next_step_id = NULL)
        3. Cancel all future messages for this enrollment (status = 'CANCELLED', terminal_reason = 'recipient_replied')
        4. Append domain event and outbox work.
        Returns (enrollments_stopped_count, messages_cancelled_count).
        """
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        # version/updated_at/created_at are DB-managed (defaults plus touch
        # triggers) and not granted to app_worker_sync, so no statement below
        # may assign them.
        now = datetime.now(UTC)
        source_key = provider_message_id[:200]

        # 1. Recipient outcome
        outcome_query = text(
            """
            INSERT INTO public.recipient_outcomes (
                id, workspace_id, enrollment_id, kind, source_key,
                occurred_at, observed_at, inbound_message_id
            ) VALUES (
                :id, :ws, :eid, 'REPLIED', :source_key,
                :occurred_at, :now, :inbound_id
            )
            ON CONFLICT (workspace_id, enrollment_id, kind, source_key) DO NOTHING
            """
        )
        self.session.execute(
            outcome_query,
            {
                "id": str(uuid4()),
                "ws": str(workspace_id),
                "eid": str(enrollment_id),
                "source_key": source_key,
                "occurred_at": occurred_at,
                "now": now,
                "inbound_id": str(inbound_message_id),
            },
        )

        # 2. Stop enrollment if active
        stop_enrollment_query = text(
            """
            UPDATE public.campaign_enrollments
            SET state = 'STOPPED',
                stop_reason = 'REPLIED',
                next_step_id = NULL
            WHERE workspace_id = :ws AND id = :eid AND state = 'ACTIVE'
            """
        )
        enr_res = self.session.execute(
            stop_enrollment_query,
            {"ws": str(workspace_id), "eid": str(enrollment_id)},
        )
        enrollments_stopped = enr_res.rowcount

        # 3. Cancel all pending/scheduled/queued future messages
        cancel_messages_query = text(
            """
            UPDATE public.messages
            SET status = 'CANCELLED',
                terminal_reason = 'recipient_replied'
            WHERE workspace_id = :ws AND enrollment_id = :eid
              AND status IN ('PLANNED', 'SCHEDULED', 'QUEUED', 'RETRY_SCHEDULED')
            """
        )
        msg_res = self.session.execute(
            cancel_messages_query,
            {"ws": str(workspace_id), "eid": str(enrollment_id)},
        )
        messages_cancelled = msg_res.rowcount

        # 4. Domain event & outbox work
        event_id = uuid4()
        semantic_key = f"{enrollment_id}:reply:{inbound_message_id}"[:200]
        payload = {
            "campaign_id": str(campaign_id),
            "enrollment_id": str(enrollment_id),
            "inbound_message_id": str(inbound_message_id),
            "provider_message_id": provider_message_id,
            "occurred_at": occurred_at.isoformat(),
        }

        insert_event = text(
            """
            INSERT INTO public.domain_events (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                semantic_key, occurred_at, recorded_at, payload
            ) VALUES (
                :id, :ws, 'enrollment.replied', 'campaign_enrollment', :eid,
                :key, :occurred_at, :now, CAST(:payload AS jsonb)
            )
            ON CONFLICT (workspace_id, event_type, semantic_key) DO NOTHING
            """
        )
        self.session.execute(
            insert_event,
            {
                "id": str(event_id),
                "ws": str(workspace_id),
                "eid": str(enrollment_id),
                "key": semantic_key,
                "occurred_at": occurred_at,
                "now": now,
                "payload": json.dumps(payload),
            },
        )

        insert_outbox = text(
            """
            INSERT INTO public.outbox_work (
                id, workspace_id, kind, resource_id, resource_type,
                event_id, semantic_key, available_at
            ) VALUES (
                :id, :ws, 'domain_event', :eid, 'campaign_enrollment',
                :eid_fk, :key, :now
            )
            ON CONFLICT (workspace_id, kind, semantic_key) DO NOTHING
            """
        )
        self.session.execute(
            insert_outbox,
            {
                "id": str(uuid4()),
                "ws": str(workspace_id),
                "eid": str(enrollment_id),
                "eid_fk": str(event_id),
                "key": semantic_key,
                "now": now,
            },
        )

        logger.info(
            "Campaign safety applied: enrollment %s stopped, %d messages cancelled",
            enrollment_id,
            messages_cancelled,
        )
        return enrollments_stopped, messages_cancelled

    # -------------------------------------------------------------------------
    # Safety Hold Management
    # -------------------------------------------------------------------------

    def insert_safety_hold(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        source_identity: str,
        reason: str,
    ) -> bool:
        """Insert an active safety hold on a mailbox. False when a hold with this
        source identity already exists (nothing changes)."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        query = text(
            """
            INSERT INTO public.safety_holds (
                id, workspace_id, source_work_identity, target_kind, target_mailbox_id,
                status, reason
            ) VALUES (
                :id, :ws, :source_id, 'MAILBOX', :mbid,
                'ACTIVE', :reason
            )
            ON CONFLICT (workspace_id, source_work_identity) DO NOTHING
            """
        )
        result = self.session.execute(
            query,
            {
                "id": str(uuid4()),
                "ws": str(workspace_id),
                "source_id": source_identity[:100],
                "mbid": str(mailbox_id),
                "reason": reason[:500],
            },
        )
        return bool(getattr(result, "rowcount", 0))

    def resolve_safety_hold(
        self,
        *,
        workspace_id: UUID,
        source_identity: str,
    ) -> None:
        """Resolve an active safety hold."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        now = datetime.now(UTC)
        query = text(
            """
            UPDATE public.safety_holds
            SET status = 'RESOLVED',
                resolved_at = :now
            WHERE workspace_id = :ws AND source_work_identity = :source_id
              AND status = 'ACTIVE'
            """
        )
        self.session.execute(
            query,
            {"ws": str(workspace_id), "source_id": source_identity, "now": now},
        )

    def resolve_resync_holds(self, *, workspace_id: UUID, mailbox_id: UUID) -> int:
        """Release every active resync hold on a mailbox once a full resync has
        completed; until then follow-up sending stays held."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)
        res = self.session.execute(
            text(
                """
                UPDATE public.safety_holds
                SET status = 'RESOLVED', resolved_at = :now
                WHERE workspace_id = :ws AND target_mailbox_id = :mbid
                  AND status = 'ACTIVE' AND source_work_identity LIKE :prefix
                """
            ),
            {
                "now": datetime.now(UTC),
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "prefix": f"resync:{mailbox_id}:%",
            },
        )
        return int(res.rowcount)

    # -------------------------------------------------------------------------
    # Reconciliation Queries
    # -------------------------------------------------------------------------

    def find_unresolved_inbound_messages(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Find UNRESOLVED inbound messages for the periodic reconciliation sweep."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        query = text(
            """
            SELECT im.id, im.mailbox_id, im.conversation_id, im.connection_generation,
                   im.provider_message_id, im.rfc_message_id, im.in_reply_to,
                   im.references_header, im.participants, im.subject, im.content_text,
                   im.received_at, im.classification,
                   c.provider_thread_id AS provider_thread_id
            FROM public.inbound_messages im
            LEFT JOIN public.conversations c
              ON c.workspace_id = im.workspace_id AND c.id = im.conversation_id
            WHERE im.workspace_id = :ws AND im.mailbox_id = :mbid
              AND im.association_status = 'UNRESOLVED'
              AND im.classification <> 'BOUNCE'
            ORDER BY im.observed_at ASC
            LIMIT :limit
            """
        )
        rows = self.session.execute(
            query,
            {"ws": str(workspace_id), "mbid": str(mailbox_id), "limit": limit},
        ).mappings().all()
        return [dict(r) for r in rows]

    def mark_inbound_message_matched(
        self,
        *,
        workspace_id: UUID,
        inbound_message_id: UUID,
    ) -> None:
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        query = text(
            """
            UPDATE public.inbound_messages
            SET association_status = 'MATCHED'
            WHERE workspace_id = :ws AND id = :id
            """
        )
        self.session.execute(query, {"ws": str(workspace_id), "id": str(inbound_message_id)})

    # -------------------------------------------------------------------------
    # Mailbox & Connection Helpers
    # -------------------------------------------------------------------------

    def get_mailbox_for_sync(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
    ) -> dict[str, Any] | None:
        """Load mailbox fields required for reply synchronization."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        query = text(
            """
            SELECT id, workspace_id, provider, connection_state, health_state,
                   current_connection_generation, original_address
            FROM public.mailboxes
            WHERE workspace_id = :ws AND id = :mbid
            """
        )
        row = self.session.execute(
            query,
            {"ws": str(workspace_id), "mbid": str(mailbox_id)},
        ).mappings().first()
        return dict(row) if row else None

    def get_mailbox_connection(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        generation: int,
    ) -> dict[str, Any] | None:
        """Load credential envelope for the specified connection generation."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        query = text(
            """
            SELECT credential_ciphertext, encryption_key_id, nonce, protected_config,
                   expires_at, auth_mechanism, granted_scopes
            FROM public.mailbox_connections
            WHERE workspace_id = :ws AND mailbox_id = :mbid AND generation = :gen
            """
        )
        row = self.session.execute(
            query,
            {"ws": str(workspace_id), "mbid": str(mailbox_id), "gen": generation},
        ).mappings().first()
        return dict(row) if row else None

    def rotate_mailbox_connection(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        new_generation: int,
        credential_ciphertext: bytes,
        encryption_key_id: str,
        nonce: bytes,
        auth_mechanism: str,
        granted_scopes: list[str],
        expires_at: datetime,
    ) -> None:
        """Rotate to a newly refreshed token generation."""
        _safe_set_role(self.session, "app_worker_sync")
        _safe_set_workspace(self.session, workspace_id)

        # version/created_at/updated_at are DB-managed (defaults plus touch
        # triggers) and not granted to app_worker_sync; see ensure_sync_state.
        ins = text(
            """
            INSERT INTO public.mailbox_connections (
                id, workspace_id, mailbox_id, generation, credential_ciphertext,
                encryption_key_id, nonce, auth_mechanism, granted_scopes, expires_at
            ) VALUES (
                :id, :ws, :mbid, :gen, :ct,
                :kid, :nonce, :auth, CAST(:scopes AS jsonb), :exp
            )
            """
        )
        self.session.execute(
            ins,
            {
                "id": str(uuid4()),
                "ws": str(workspace_id),
                "mbid": str(mailbox_id),
                "gen": new_generation,
                "ct": credential_ciphertext,
                "kid": encryption_key_id,
                "nonce": nonce,
                "auth": auth_mechanism,
                "scopes": json.dumps(granted_scopes),
                "exp": expires_at,
            },
        )
        upd_mb = text(
            """
            UPDATE public.mailboxes
            SET connected_generation = :gen,
                current_connection_generation = :gen,
                connection_state = 'CONNECTED',
                health_state = 'HEALTHY'
            WHERE workspace_id = :ws AND id = :mbid
            """
        )
        self.session.execute(
            upd_mb,
            {"gen": new_generation, "ws": str(workspace_id), "mbid": str(mailbox_id)},
        )
        upd_sync = text(
            """
            UPDATE public.mailbox_sync_states
            SET connection_generation = :gen
            WHERE workspace_id = :ws AND mailbox_id = :mbid
            """
        )
        self.session.execute(
            upd_sync,
            {"gen": new_generation, "ws": str(workspace_id), "mbid": str(mailbox_id)},
        )

