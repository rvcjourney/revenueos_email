from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from app.modules.scheduler.schemas import ClaimResult, DueMessageCandidate

logger = logging.getLogger(__name__)


def _safe_set_role(session: Session, role_name: str) -> None:
    """Attempt SET LOCAL ROLE if running against PostgreSQL; ignore in SQLite."""
    bind = session.get_bind()
    if bind and getattr(bind.dialect, "name", "") != "sqlite":
        session.execute(text(f"RESET ROLE; SET LOCAL ROLE {role_name}"))


def _safe_set_workspace(session: Session, workspace_id: UUID | None) -> None:
    """Set transaction-local app.workspace_id if running against PostgreSQL."""
    bind = session.get_bind()
    if bind and getattr(bind.dialect, "name", "") != "sqlite":
        if workspace_id is not None:
            session.execute(
                text("SELECT set_config('app.workspace_id', :ws, true)"),
                {"ws": str(workspace_id)},
            )
        else:
            session.execute(text("SELECT set_config('app.workspace_id', '', true)"))


def _mailbox_can_send(mailbox: Any) -> bool:
    """The mailbox half of the send gates, evaluated at claim time so a claim is
    not wasted on a message the send worker is certain to refuse (sending/gates.py
    check_mailbox_state and check_safety_holds; blocked_until is handled separately
    because it needs the authoritative clock)."""
    return (
        mailbox["connection_state"] == "CONNECTED"
        and mailbox["policy_state"] == "ENABLED"
        and mailbox["health_state"] != "DEGRADED"
        and mailbox["circuit_state"] != "OPEN"
        and int(mailbox["pending_safety_count"] or 0) == 0
    )


class SchedulerRepository:
    """Database operations for the app_scheduler role.

    Follows the principle of least privilege:
    - Discovery queries run globally using discovery SELECT policies.
    - Claim and recovery mutations run strictly within workspace-scoped transactions
      with app.workspace_id bound.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_running_campaigns(
        self, *, after_id: UUID | None, limit: int
    ) -> list[tuple[UUID, UUID]]:
        """(workspace_id, campaign_id) of RUNNING campaigns whose planning is
        READY, keyset-paginated on campaign id.

        Uses the cross-tenant campaigns discovery policy the scheduler already
        relies on for due-message discovery. Enrollments are NOT readable
        cross-tenant by this role, so it cannot tell which campaigns actually
        have follow-ups to plan; the progression task does that per workspace.
        """
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)
        rows = self.session.execute(
            text(
                """
                SELECT c.workspace_id, c.id
                FROM campaigns c
                WHERE c.status = 'RUNNING'
                  AND c.planning_status = 'READY'
                  AND (CAST(:after_id AS uuid) IS NULL
                       OR c.id > CAST(:after_id AS uuid))
                ORDER BY c.id
                LIMIT :limit
                """
            ),
            {"after_id": str(after_id) if after_id else None, "limit": limit},
        ).all()
        return [(UUID(str(ws)), UUID(str(cid))) for ws, cid in rows]

    def find_stuck_planning_jobs(
        self, *, limit: int
    ) -> list[tuple[UUID, UUID, UUID, UUID | None, str]]:
        """(workspace, campaign, activation, supplement audience or None, phase) of
        ENROLL/RENDER jobs nobody is working on: PENDING that was never picked up
        (publish lost) or PROCESSING with an expired lease (task or its retry message
        lost). A minute of grace keeps this off jobs whose task is simply in flight.
        RENDER is only offered once its ENROLL is READY: it renders every PLANNED
        message of the campaign, so running it early would strand the rest. Read-only;
        the recovery is a worker task (migration 0045 grants SELECT only)."""
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)
        rows = self.session.execute(
            text(
                """
                SELECT j.workspace_id, j.campaign_id, j.activation_id,
                       j.audience_id, j.phase
                FROM campaign_planning_jobs j
                JOIN campaigns c ON c.workspace_id = j.workspace_id
                                AND c.id = j.campaign_id
                WHERE j.phase IN ('ENROLL', 'RENDER')
                  AND j.state IN ('PENDING', 'PROCESSING')
                  AND j.next_due_at
                      <= pg_catalog.transaction_timestamp() - interval '1 minute'
                  AND (j.lease_expires_at IS NULL
                       OR j.lease_expires_at < pg_catalog.transaction_timestamp())
                  AND c.status <> 'ARCHIVED'
                  AND c.activation_id = j.activation_id
                  AND (
                      j.phase = 'ENROLL'
                      OR EXISTS (
                          SELECT 1 FROM campaign_planning_jobs e
                          WHERE e.workspace_id = j.workspace_id
                            AND e.campaign_id = j.campaign_id
                            AND e.activation_id = j.activation_id
                            AND e.audience_id IS NOT DISTINCT FROM j.audience_id
                            AND e.phase = 'ENROLL' AND e.state = 'READY'
                      )
                  )
                ORDER BY j.next_due_at, j.id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).all()
        return [
            (
                UUID(str(ws)),
                UUID(str(cid)),
                UUID(str(act)),
                UUID(str(aud)) if aud else None,
                str(phase),
            )
            for ws, cid, act, aud, phase in rows
        ]

    def find_due_scheduled_campaigns(self, *, limit: int) -> list[tuple[UUID, UUID]]:
        """(workspace_id, campaign_id) of SCHEDULED campaigns whose start time has
        arrived by the database clock, oldest first. Uses campaigns_status_start_idx
        (status, start_at, id), which exists for exactly this scan."""
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)
        rows = self.session.execute(
            text(
                """
                SELECT workspace_id, id FROM campaigns
                WHERE status = 'SCHEDULED'
                  AND start_at <= pg_catalog.transaction_timestamp()
                ORDER BY start_at, id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).all()
        return [(UUID(str(ws)), UUID(str(cid))) for ws, cid in rows]

    def start_due_campaign(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> int | None:
        """SCHEDULED -> RUNNING (CAMPAIGN_STATE_MACHINE "Due start"). Returns the
        campaign's schedule_generation when it started, None when it did not.

        The guard is the WHERE clause, evaluated under the row lock the UPDATE takes:
        a pause (or archive) that committed first leaves the campaign PAUSED, so this
        matches nothing -- a stale due-start can never resume a paused campaign, and
        two schedulers racing start it once. Planning need not be READY: sends are
        gated on that separately. The generation is deliberately not bumped."""
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, workspace_id)
        row = self.session.execute(
            text(
                """
                UPDATE campaigns
                SET status = 'RUNNING'
                WHERE workspace_id = :ws AND id = :cid
                  AND status = 'SCHEDULED'
                  AND start_at <= pg_catalog.transaction_timestamp()
                RETURNING schedule_generation
                """
            ),
            {"ws": str(workspace_id), "cid": str(campaign_id)},
        ).first()
        if row is None:
            return None
        self.session.execute(
            text(
                """
                INSERT INTO public.audit_events (
                    id, workspace_id, actor_kind, actor_id, action, target_type,
                    target_id, before_state, after_state, reason
                ) VALUES (
                    :id, :ws, 'SYSTEM', NULL, 'campaign.start', 'campaign', :cid,
                    CAST(:before AS jsonb), CAST(:after AS jsonb), :reason
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "ws": str(workspace_id),
                "cid": str(campaign_id),
                "before": json.dumps({"status": "SCHEDULED"}),
                "after": json.dumps({"status": "RUNNING"}),
                "reason": "scheduled start time reached",
            },
        )
        return int(row[0])

    def find_campaigns_with_stale_generation(
        self, *, after_id: UUID | None, limit: int
    ) -> list[tuple[UUID, UUID, int]]:
        """(workspace_id, campaign_id, schedule_generation) of RUNNING campaigns
        that still hold unsent messages stamped with an older generation, i.e.
        a resume whose reconcile pass did not finish (ADR-0020). Only campaigns
        changed (resumed, started) in the last two days are looked at: a lost
        reconcile is noticed long before that, and without the bound every large
        resumed campaign would be scanned every minute for ever (the probe walks
        the campaign's messages through messages_workspace_campaign_idx).
        Read-only cross-tenant discovery through the existing scheduler SELECT
        policies; the reconcile itself is workspace-scoped."""
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)
        rows = self.session.execute(
            text(
                """
                SELECT c.workspace_id, c.id, c.schedule_generation
                FROM campaigns c
                WHERE c.status = 'RUNNING'
                  AND c.planning_status = 'READY'
                  AND c.schedule_generation > 1
                  AND c.updated_at >= pg_catalog.transaction_timestamp()
                                      - interval '2 days'
                  AND (CAST(:after_id AS uuid) IS NULL
                       OR c.id > CAST(:after_id AS uuid))
                  AND EXISTS (
                      SELECT 1 FROM messages m
                      WHERE m.workspace_id = c.workspace_id
                        AND m.campaign_id = c.id
                        AND m.purpose = 'CAMPAIGN'
                        AND m.status IN ('SCHEDULED', 'RETRY_SCHEDULED')
                        AND m.schedule_generation < c.schedule_generation
                  )
                ORDER BY c.id
                LIMIT :limit
                """
            ),
            {"after_id": str(after_id) if after_id else None, "limit": limit},
        ).all()
        return [(UUID(str(ws)), UUID(str(cid)), int(gen)) for ws, cid, gen in rows]

    def find_campaigns_with_due_generation(
        self, *, after_id: UUID | None, limit: int
    ) -> list[tuple[UUID, UUID]]:
        """(workspace_id, campaign_id) of RUNNING campaigns that have
        hyper-personalized messages ready for generation (ADR-0011): a PENDING
        job that is due (or has used up its attempts and needs failing) and that
        no worker currently holds.

        Read-only cross-tenant discovery through the narrow scheduler policy on
        message_generations (migration 0028), mirroring find_running_campaigns.
        The claim itself is workspace-scoped and done by the personalization
        task; this only names campaigns."""
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)
        rows = self.session.execute(
            text(
                """
                SELECT g.workspace_id, g.campaign_id
                FROM message_generations g
                JOIN campaigns c
                  ON c.workspace_id = g.workspace_id AND c.id = g.campaign_id
                WHERE g.state = 'PENDING'
                  AND (g.lease_owner IS NULL
                       OR g.lease_expires_at < pg_catalog.transaction_timestamp())
                  AND (g.next_attempt_at <= pg_catalog.transaction_timestamp()
                       OR g.attempt_count >= g.max_attempts)
                  AND c.status = 'RUNNING' AND c.planning_status = 'READY'
                  AND (CAST(:after_id AS uuid) IS NULL
                       OR g.campaign_id > CAST(:after_id AS uuid))
                GROUP BY g.workspace_id, g.campaign_id
                ORDER BY g.campaign_id
                LIMIT :limit
                """
            ),
            {"after_id": str(after_id) if after_id else None, "limit": limit},
        ).all()
        return [(UUID(str(ws)), UUID(str(cid))) for ws, cid in rows]

    def count_outstanding_claims(self) -> int:
        """Messages the scheduler has claimed that no send worker has finished
        with yet (status QUEUED), across all workspaces. Uses
        messages_claim_expiry_idx."""
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)
        count = self.session.execute(
            text("SELECT count(*) FROM messages WHERE status = 'QUEUED'")
        ).scalar()
        return int(count or 0)

    def find_due_messages(
        self,
        *,
        limit: int = 50,
        authoritative_now: datetime | None = None,
    ) -> list[DueMessageCandidate]:
        """Find messages in SCHEDULED or RETRY_SCHEDULED state whose due_at <= now.

        Uses the messages_due_idx (due_at, id) partial index.
        Evaluates parent gates (campaign RUNNING, planning READY, schedule generation match,
        mailbox not blocked).
        """
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)

        bind = self.session.get_bind()
        is_sqlite = bind and getattr(bind.dialect, "name", "") == "sqlite"

        now_expr = ":now" if authoritative_now else (
            "CURRENT_TIMESTAMP" if is_sqlite else "pg_catalog.transaction_timestamp()"
        )

        query = f"""
            SELECT m.id, m.workspace_id, m.campaign_id, m.mailbox_id, m.due_at,
                   m.purpose, m.schedule_generation, m.status, m.dispatch_generation
            FROM messages m
            LEFT JOIN campaigns c
              ON c.workspace_id = m.workspace_id AND c.id = m.campaign_id
            LEFT JOIN mailboxes mb
              ON mb.workspace_id = m.workspace_id AND mb.id = m.mailbox_id
            LEFT JOIN controlled_send_authorizations csa
              ON csa.workspace_id = m.workspace_id AND csa.id = m.controlled_send_authorization_id
            WHERE m.status IN ('SCHEDULED', 'RETRY_SCHEDULED')
              AND m.due_at <= {now_expr}
              -- A mailbox that cannot send is not worth a claim: the send worker
              -- would refuse the message and it would sit QUEUED for the whole claim
              -- lease, using up claim slots that healthy mailboxes need. Same rules
              -- as sending/gates.py check_mailbox_state + check_safety_holds; a
              -- missing mailbox row fails closed (NULL comparisons are not true).
              AND mb.connection_state = 'CONNECTED'
              AND mb.policy_state = 'ENABLED'
              AND mb.health_state <> 'DEGRADED'
              AND mb.circuit_state <> 'OPEN'
              AND mb.pending_safety_count = 0
              AND (
                  (
                      m.purpose = 'CAMPAIGN'
                      AND c.status = 'RUNNING'
                      AND c.planning_status = 'READY'
                      AND m.schedule_generation = c.schedule_generation
                      AND (mb.blocked_until IS NULL OR mb.blocked_until <= {now_expr})
                  )
                  OR (
                      m.purpose = 'CONTROLLED_TEST'
                      AND csa.revoked_at IS NULL
                      AND csa.expires_at > {now_expr}
                      AND (mb.blocked_until IS NULL OR mb.blocked_until <= {now_expr})
                  )
              )
            ORDER BY m.due_at, m.id
            LIMIT :limit
        """

        params: dict[str, Any] = {"limit": limit}
        if authoritative_now:
            params["now"] = authoritative_now

        rows = self.session.execute(text(query), params).mappings().all()

        return [
            DueMessageCandidate(
                id=row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"])),
                workspace_id=(
                    row["workspace_id"]
                    if isinstance(row["workspace_id"], UUID)
                    else UUID(str(row["workspace_id"]))
                ),
                campaign_id=(
                    row["campaign_id"]
                    if row["campaign_id"] is None or isinstance(row["campaign_id"], UUID)
                    else UUID(str(row["campaign_id"]))
                ),
                mailbox_id=(
                    row["mailbox_id"]
                    if isinstance(row["mailbox_id"], UUID)
                    else UUID(str(row["mailbox_id"]))
                ),
                due_at=row["due_at"],
                purpose=row["purpose"],
                schedule_generation=row["schedule_generation"],
                status=row["status"],
                dispatch_generation=row["dispatch_generation"],
            )
            for row in rows
        ]

    def claim_due_message(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        lease_seconds: int = 300,
        authoritative_now: datetime | None = None,
    ) -> ClaimResult:
        """Atomically claim one due message under workspace context.

        1. Enters workspace-scoped transaction (app.workspace_id = workspace_id).
        2. Acquires parent gates in engine order: campaign -> mailbox -> message FOR UPDATE SKIP LOCKED.
        3. Transitions message to QUEUED with dispatch_origin, dispatch_generation + 1, claim_expires_at.
        4. Inserts outbox_work and outbox_deliveries atomically.
        """
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, workspace_id)

        bind = self.session.get_bind()
        is_sqlite = bind and getattr(bind.dialect, "name", "") == "sqlite"

        now_expr = ":now" if authoritative_now else (
            "CURRENT_TIMESTAMP" if is_sqlite else "pg_catalog.transaction_timestamp()"
        )
        lock_clause = "" if is_sqlite else "FOR UPDATE SKIP LOCKED"
        parent_lock_clause = "" if is_sqlite else "FOR UPDATE"

        params: dict[str, Any] = {
            "ws": str(workspace_id),
            "mid": str(message_id),
            "lease_secs": lease_seconds,
        }
        if authoritative_now:
            params["now"] = authoritative_now

        # 1. Lock message row with SKIP LOCKED
        msg_query = f"""
            SELECT id, workspace_id, campaign_id, mailbox_id, status,
                   dispatch_generation, schedule_generation, purpose,
                   controlled_send_authorization_id, due_at, version
            FROM messages
            WHERE workspace_id = :ws AND id = :mid
              AND status IN ('SCHEDULED', 'RETRY_SCHEDULED')
              AND due_at <= {now_expr}
            {lock_clause}
        """
        msg_row = self.session.execute(text(msg_query), params).mappings().first()
        if not msg_row:
            return ClaimResult(
                message_id=message_id,
                workspace_id=workspace_id,
                dispatch_generation=0,
                claimed=False,
                reason="Message not found, already claimed, or locked by another worker",
            )

        # 2. Acquire parent gates in engine order
        if msg_row["purpose"] == "CAMPAIGN":
            camp_query = f"""
                SELECT id, status, planning_status, schedule_generation
                FROM campaigns
                WHERE workspace_id = :ws AND id = :cid
                {parent_lock_clause}
            """
            camp_row = (
                self.session.execute(
                    text(camp_query),
                    {"ws": str(workspace_id), "cid": str(msg_row["campaign_id"])},
                )
                .mappings()
                .first()
            )
            if not camp_row or camp_row["status"] != "RUNNING" or camp_row["planning_status"] != "READY":
                return ClaimResult(
                    message_id=message_id,
                    workspace_id=workspace_id,
                    dispatch_generation=msg_row["dispatch_generation"],
                    claimed=False,
                    reason="Campaign is not RUNNING and planning READY",
                )
            if camp_row["schedule_generation"] != msg_row["schedule_generation"]:
                return ClaimResult(
                    message_id=message_id,
                    workspace_id=workspace_id,
                    dispatch_generation=msg_row["dispatch_generation"],
                    claimed=False,
                    reason="Message schedule_generation does not match campaign current schedule_generation",
                )

        # Mailbox gate (read-only check under message lock)
        mb_query = f"""
            SELECT id, blocked_until, connection_state, policy_state, health_state,
                   circuit_state, pending_safety_count
            FROM mailboxes
            WHERE workspace_id = :ws AND id = :mb_id
        """
        mb_row = (
            self.session.execute(
                text(mb_query),
                {"ws": str(workspace_id), "mb_id": str(msg_row["mailbox_id"])},
            )
            .mappings()
            .first()
        )
        if mb_row is not None and not _mailbox_can_send(mb_row):
            return ClaimResult(
                message_id=message_id,
                workspace_id=workspace_id,
                dispatch_generation=msg_row["dispatch_generation"],
                claimed=False,
                reason="Mailbox cannot send right now (state, health or hold)",
            )
        if mb_row and mb_row["blocked_until"] is not None:
            # Check blocked_until against authoritative now
            now_dt = authoritative_now or datetime.now(timezone.utc)
            blocked_until = mb_row["blocked_until"]
            if hasattr(blocked_until, "tzinfo") and blocked_until.tzinfo is None:
                blocked_until = blocked_until.replace(tzinfo=timezone.utc)
            if blocked_until > now_dt:
                return ClaimResult(
                    message_id=message_id,
                    workspace_id=workspace_id,
                    dispatch_generation=msg_row["dispatch_generation"],
                    claimed=False,
                    reason="Mailbox is currently in cooldown/blocked_until",
                )

        # 3. Transition message to QUEUED
        old_status = msg_row["status"]
        old_generation = msg_row["dispatch_generation"]
        new_generation = old_generation + 1
        work_id = uuid.uuid4()
        delivery_id = uuid.uuid4()
        correlation_id = uuid.uuid4()
        semantic_key = f"{message_id}:{new_generation}"

        lease_expiry_expr = (
            f"datetime({now_expr}, '+{lease_seconds} seconds')"
            if is_sqlite
            else f"{now_expr} + make_interval(secs => :lease_secs)"
        )

        update_msg_query = f"""
            UPDATE messages
            SET status = 'QUEUED',
                dispatch_origin = :old_status,
                dispatch_generation = :new_generation,
                claim_expires_at = {lease_expiry_expr},
                version = version + 1
            WHERE workspace_id = :ws AND id = :mid
              AND status = :old_status
              AND dispatch_generation = :old_generation
            RETURNING id, dispatch_generation
        """
        updated = (
            self.session.execute(
                text(update_msg_query),
                {
                    **params,
                    "old_status": old_status,
                    "old_generation": old_generation,
                    "new_generation": new_generation,
                },
            )
            .mappings()
            .first()
        )
        if not updated:
            return ClaimResult(
                message_id=message_id,
                workspace_id=workspace_id,
                dispatch_generation=old_generation,
                claimed=False,
                reason="Concurrent claim collision on message state update",
            )

        # 4. Insert outbox_work and outbox_deliveries
        outbox_work_query = f"""
            INSERT INTO outbox_work
                (id, workspace_id, kind, schema_version, resource_id, resource_type,
                 semantic_key, correlation_id, available_at)
            VALUES
                (:work_id, :ws, 'email.send', 1, :mid, 'message',
                 :semantic_key, :correlation_id, {now_expr})
            ON CONFLICT (workspace_id, kind, semantic_key) DO NOTHING
        """
        self.session.execute(
            text(outbox_work_query),
            {
                **params,
                "work_id": str(work_id),
                "semantic_key": semantic_key,
                "correlation_id": str(correlation_id),
            },
        )

        outbox_delivery_query = f"""
            INSERT INTO outbox_deliveries
                (id, workspace_id, work_id, consumer, state, next_attempt_at)
            VALUES
                (:delivery_id, :ws, :work_id, 'worker-send', 'PENDING', {now_expr})
            ON CONFLICT (workspace_id, work_id, consumer) DO NOTHING
        """
        self.session.execute(
            text(outbox_delivery_query),
            {
                **params,
                "delivery_id": str(delivery_id),
                "work_id": str(work_id),
            },
        )

        return ClaimResult(
            message_id=message_id,
            workspace_id=workspace_id,
            dispatch_generation=new_generation,
            claimed=True,
        )

    def find_expired_claims(
        self,
        *,
        limit: int = 50,
        authoritative_now: datetime | None = None,
    ) -> list[RowMapping]:
        """Find messages in QUEUED state whose claim_expires_at <= now.

        Uses messages_claim_expiry_idx (claim_expires_at, id).
        """
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)

        bind = self.session.get_bind()
        is_sqlite = bind and getattr(bind.dialect, "name", "") == "sqlite"

        now_expr = ":now" if authoritative_now else (
            "CURRENT_TIMESTAMP" if is_sqlite else "pg_catalog.transaction_timestamp()"
        )

        query = f"""
            SELECT id, workspace_id, dispatch_origin, dispatch_generation,
                   claim_expires_at, version
            FROM messages
            WHERE status = 'QUEUED'
              AND claim_expires_at <= {now_expr}
            ORDER BY claim_expires_at, id
            LIMIT :limit
        """
        params: dict[str, Any] = {"limit": limit}
        if authoritative_now:
            params["now"] = authoritative_now

        rows = self.session.execute(text(query), params).mappings().all()
        return list(rows)

    def recover_expired_claim(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        dispatch_generation: int,
    ) -> bool:
        """Recover one expired message claim back to its dispatch_origin.

        Safety invariant:
        - If an attempt was committed in message_attempts for this message and dispatch_generation,
          it is NEVER reset by this lease recovery sweep (owned by send reconciler).
        - If NO attempt was authorized, revert message status to dispatch_origin (SCHEDULED / RETRY_SCHEDULED),
          clear claim_expires_at, increment version, and mark active outbox deliveries as SUPERSEDED.
        """
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, workspace_id)

        bind = self.session.get_bind()
        is_sqlite = bind and getattr(bind.dialect, "name", "") == "sqlite"
        lock_clause = "" if is_sqlite else "FOR UPDATE SKIP LOCKED"

        params: dict[str, Any] = {
            "ws": str(workspace_id),
            "mid": str(message_id),
            "gen": dispatch_generation,
        }

        # 1. Lock message
        msg_query = f"""
            SELECT id, status, dispatch_origin, dispatch_generation, version
            FROM messages
            WHERE workspace_id = :ws AND id = :mid
              AND status = 'QUEUED'
              AND dispatch_generation = :gen
            {lock_clause}
        """
        msg = self.session.execute(text(msg_query), params).mappings().first()
        if not msg:
            return False

        # 2. Check if an attempt exists in message_attempts
        attempt_query = """
            SELECT 1 FROM message_attempts
            WHERE workspace_id = :ws AND message_id = :mid
              AND dispatch_generation = :gen
            LIMIT 1
        """
        attempt = self.session.execute(text(attempt_query), params).scalar()
        if attempt:
            logger.warning(
                f"Message {message_id} has an authorized attempt; cannot recover via lease expiry."
            )
            return False

        origin = msg["dispatch_origin"] or "SCHEDULED"
        semantic_key = f"{message_id}:{dispatch_generation}"

        # 3. Reset message status to dispatch_origin
        update_query = """
            UPDATE messages
            SET status = :origin,
                dispatch_origin = NULL,
                claim_expires_at = NULL,
                version = version + 1
            WHERE workspace_id = :ws AND id = :mid
              AND status = 'QUEUED'
              AND dispatch_generation = :gen
        """
        self.session.execute(
            text(update_query),
            {**params, "origin": origin},
        )

        # 4. Mark uncompleted outbox deliveries as SUPERSEDED
        supersede_query = """
            UPDATE outbox_deliveries
            SET state = 'SUPERSEDED'
            WHERE workspace_id = :ws
              AND work_id IN (
                  SELECT id FROM outbox_work
                  WHERE workspace_id = :ws
                    AND kind = 'email.send'
                    AND semantic_key = :semantic_key
              )
              AND state IN ('PENDING', 'LEASED', 'RETRY')
        """
        self.session.execute(
            text(supersede_query),
            {"ws": str(workspace_id), "semantic_key": semantic_key},
        )

        return True

    def get_oldest_due_lag_seconds(
        self,
        *,
        authoritative_now: datetime | None = None,
    ) -> float | None:
        """Compute the age (in seconds) of the oldest eligible due message."""
        _safe_set_role(self.session, "app_scheduler")
        _safe_set_workspace(self.session, None)

        bind = self.session.get_bind()
        is_sqlite = bind and getattr(bind.dialect, "name", "") == "sqlite"

        now_expr = ":now" if authoritative_now else (
            "CURRENT_TIMESTAMP" if is_sqlite else "pg_catalog.transaction_timestamp()"
        )

        query = f"""
            SELECT MIN(due_at) AS oldest_due
            FROM messages
            WHERE status IN ('SCHEDULED', 'RETRY_SCHEDULED')
              AND due_at <= {now_expr}
        """
        params: dict[str, Any] = {}
        if authoritative_now:
            params["now"] = authoritative_now

        oldest = self.session.execute(text(query), params).scalar()
        if oldest is None:
            return None

        now = authoritative_now or datetime.now(timezone.utc)
        if hasattr(oldest, "tzinfo") and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        if hasattr(now, "tzinfo") and now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        lag = (now - oldest).total_seconds()
        return max(0.0, lag)
