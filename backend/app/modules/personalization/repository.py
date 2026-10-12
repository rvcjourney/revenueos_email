"""All personalization SQL that runs as app_worker_general (ADR-0011).

Every statement is workspace-scoped. No method holds a lock across network I/O:
the runner opens a short transaction per unit (`GenerationDb.transaction`),
never one that spans a research fetch or a model call.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import CursorResult, RowMapping, text
from sqlalchemy.orm import Session

from app.db.context import enter_worker_scope
from app.modules.suppression.checks import is_address_suppressed

_ROLE = "app_worker_general"


def _codes(value: Sequence[str] | None) -> list[str] | None:
    if not value:
        return None
    return [str(code)[:64] for code in list(value)[:10]]


def _is_sqlite(session: Session) -> bool:
    bind = session.get_bind()
    return bool(bind is not None and getattr(bind.dialect, "name", "") == "sqlite")


class PersonalizationRepository:
    """Session-bound. Callers must already be inside `enter_worker_scope`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def is_suppressed(self, *, workspace_id: UUID, address_id: UUID) -> bool:
        """Courtesy re-check inside the finalizing transaction; the send gates
        remain the authoritative suppression check at send time."""
        return is_address_suppressed(self.session, workspace_id, address_id)

    # ------------------------------------------------------------------
    # Usage budget (ADR-0012): atomic per-workspace daily counters
    # ------------------------------------------------------------------

    def reserve_usage(
        self, *, workspace_id: UUID, day: date, kind: str, cap: int
    ) -> bool:
        """Atomically add one unit unless the daily cap is reached. The single
        INSERT ... ON CONFLICT DO UPDATE ... WHERE units < cap is race-free."""
        row = self.session.execute(
            text(
                """
                INSERT INTO personalization_usage_daily
                    (workspace_id, usage_day, kind, units, updated_at)
                VALUES
                    (:workspace_id, :day, :kind, 1, pg_catalog.transaction_timestamp())
                ON CONFLICT (workspace_id, usage_day, kind) DO UPDATE
                SET units = personalization_usage_daily.units + 1,
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE personalization_usage_daily.units < :cap
                RETURNING units
                """
            ),
            {"workspace_id": str(workspace_id), "day": day, "kind": kind, "cap": cap},
        ).first()
        return row is not None

    def refund_usage(self, *, workspace_id: UUID, day: date, kind: str) -> None:
        self.session.execute(
            text(
                """
                UPDATE personalization_usage_daily
                SET units = GREATEST(units - 1, 0),
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND usage_day = :day AND kind = :kind
                """
            ),
            {"workspace_id": str(workspace_id), "day": day, "kind": kind},
        )

    def add_tokens(
        self,
        *,
        workspace_id: UUID,
        day: date,
        kind: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE personalization_usage_daily
                SET input_tokens = input_tokens + :input_tokens,
                    output_tokens = output_tokens + :output_tokens,
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND usage_day = :day AND kind = :kind
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "day": day,
                "kind": kind,
                "input_tokens": max(0, int(input_tokens)),
                "output_tokens": max(0, int(output_tokens)),
            },
        )

    # ------------------------------------------------------------------
    # Research cache (worker-only, per workspace, never shared)
    # ------------------------------------------------------------------

    def get_cache(
        self, *, workspace_id: UUID, source: str, url_hash: str
    ) -> RowMapping | None:
        return (
            self.session.execute(
                text(
                    """
                    SELECT status, extracted_text, normalized_url
                    FROM personalization_research_cache
                    WHERE workspace_id = :workspace_id AND source = :source
                      AND url_hash = :url_hash
                      AND expires_at > pg_catalog.transaction_timestamp()
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "source": source,
                    "url_hash": url_hash,
                },
            )
            .mappings()
            .first()
        )

    def upsert_cache(
        self,
        *,
        workspace_id: UUID,
        source: str,
        url_hash: str,
        normalized_url: str,
        status: str,
        extracted_text: str | None,
        content_sha256: str | None,
        http_status: int | None,
        ttl_seconds: int,
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO personalization_research_cache
                    (id, workspace_id, source, url_hash, normalized_url, status,
                     extracted_text, content_sha256, http_status, fetched_at, expires_at)
                VALUES
                    (:id, :workspace_id, :source, :url_hash, :normalized_url, :status,
                     :extracted_text, :content_sha256, :http_status,
                     pg_catalog.transaction_timestamp(),
                     pg_catalog.transaction_timestamp() + make_interval(secs => :ttl))
                ON CONFLICT (workspace_id, source, url_hash) DO UPDATE
                SET status = EXCLUDED.status,
                    extracted_text = EXCLUDED.extracted_text,
                    content_sha256 = EXCLUDED.content_sha256,
                    http_status = EXCLUDED.http_status,
                    fetched_at = EXCLUDED.fetched_at,
                    expires_at = EXCLUDED.expires_at
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "workspace_id": str(workspace_id),
                "source": source,
                "url_hash": url_hash,
                "normalized_url": normalized_url[:2048],
                "status": status,
                "extracted_text": extracted_text,
                "content_sha256": content_sha256,
                "http_status": http_status,
                "ttl": ttl_seconds,
            },
        )

    def purge_expired(self, *, workspace_id: UUID, limit: int = 200) -> int:
        """Opportunistic cleanup of this workspace's expired cache and preview
        rows; the DELETE policies only permit expired rows."""
        removed = 0
        for table in ("personalization_research_cache", "personalization_previews"):
            result = self.session.execute(
                text(
                    f"""
                    DELETE FROM {table}
                    WHERE workspace_id = :workspace_id
                      AND id IN (
                          SELECT id FROM {table}
                          WHERE workspace_id = :workspace_id
                            AND expires_at < pg_catalog.transaction_timestamp()
                          LIMIT :limit
                      )
                    """  # noqa: S608 -- table name is a fixed constant above
                ),
                {"workspace_id": str(workspace_id), "limit": limit},
            )
            removed += cast(CursorResult[Any], result).rowcount or 0
        return removed

    # ------------------------------------------------------------------
    # Generation jobs
    # ------------------------------------------------------------------

    def fail_exhausted(
        self, *, workspace_id: UUID, campaign_id: UUID, limit: int
    ) -> list[RowMapping]:
        """PENDING generations whose attempts are used up and which no worker
        holds: the message is FAILED (never sent) and the generation FAILED.
        Returns the affected rows (ids only are needed for logging)."""
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT g.id, g.message_id, g.last_failure_codes
                    FROM message_generations g
                    WHERE g.workspace_id = :workspace_id AND g.campaign_id = :campaign_id
                      AND g.state = 'PENDING'
                      AND g.attempt_count >= g.max_attempts
                      AND (g.lease_owner IS NULL
                           OR g.lease_expires_at < pg_catalog.transaction_timestamp())
                    ORDER BY g.id
                    LIMIT :limit
                    FOR UPDATE OF g SKIP LOCKED
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "limit": limit,
                },
            )
            .mappings()
            .all()
        )
        for row in rows:
            code = (row["last_failure_codes"] or ["attempts_exhausted"])[0]
            self.abandon_open_attempts(
                workspace_id=workspace_id, generation_id=row["id"]
            )
            self.fail_generation(
                workspace_id=workspace_id,
                generation_id=row["id"],
                message_id=row["message_id"],
                failure_code="attempts_exhausted",
                failure_codes=[code],
            )
        return list(rows)

    def claim_due(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        lease_owner: str,
        ttl_seconds: int,
        limit: int,
    ) -> list[RowMapping]:
        """Lease due generations of a RUNNING/READY campaign whose message is
        still PLANNED. FOR UPDATE SKIP LOCKED lets workers split the work; the
        attempt is counted at claim time so a crash can never yield unbounded
        model calls."""
        return list(
            self.session.execute(
                text(
                    """
                    WITH picked AS (
                        SELECT g.id
                        FROM message_generations g
                        JOIN messages m
                          ON m.workspace_id = g.workspace_id AND m.id = g.message_id
                        JOIN campaigns c
                          ON c.workspace_id = g.workspace_id AND c.id = g.campaign_id
                        WHERE g.workspace_id = :workspace_id
                          AND g.campaign_id = :campaign_id
                          AND g.state = 'PENDING'
                          AND g.next_attempt_at <= pg_catalog.transaction_timestamp()
                          AND g.attempt_count < g.max_attempts
                          AND (g.lease_owner IS NULL
                               OR g.lease_expires_at < pg_catalog.transaction_timestamp())
                          AND m.status = 'PLANNED'
                          AND c.status = 'RUNNING' AND c.planning_status = 'READY'
                        ORDER BY g.next_attempt_at, g.id
                        LIMIT :limit
                        FOR UPDATE OF g SKIP LOCKED
                    )
                    UPDATE message_generations g
                    SET lease_owner = :lease_owner,
                        lease_expires_at = pg_catalog.transaction_timestamp()
                            + make_interval(secs => :ttl),
                        attempt_count = g.attempt_count + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    FROM picked
                    WHERE g.workspace_id = :workspace_id AND g.id = picked.id
                    RETURNING g.id, g.message_id, g.attempt_count, g.max_attempts,
                              g.transient_error_count, g.last_failure_codes
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "lease_owner": lease_owner,
                    "ttl": ttl_seconds,
                    "limit": limit,
                },
            )
            .mappings()
            .all()
        )

    def abandon_open_attempts(self, *, workspace_id: UUID, generation_id: UUID) -> None:
        """A RESERVED attempt whose worker vanished (lease expired) is closed as
        ABANDONED. It still counts toward attempt_count (bounded billing)."""
        self.session.execute(
            text(
                """
                UPDATE message_generation_attempts
                SET outcome = 'ABANDONED', finished_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND generation_id = :generation_id
                  AND outcome = 'RESERVED'
                """
            ),
            {"workspace_id": str(workspace_id), "generation_id": str(generation_id)},
        )

    def insert_attempt(self, *, workspace_id: UUID, generation_id: UUID) -> int:
        """Append a RESERVED ledger row and return its number. The ledger number
        is independent of the charged-attempt counter (transient provider errors
        refund a charge but must never reuse a ledger row). The caller holds the
        generation row (claimed in the same transaction), so numbering is
        race-free."""
        row = self.session.execute(
            text(
                """
                INSERT INTO message_generation_attempts
                    (id, workspace_id, generation_id, attempt_no, outcome)
                SELECT :id, :workspace_id, :generation_id,
                       COALESCE(MAX(attempt_no), 0) + 1, 'RESERVED'
                FROM message_generation_attempts
                WHERE workspace_id = :workspace_id AND generation_id = :generation_id
                RETURNING attempt_no
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "workspace_id": str(workspace_id),
                "generation_id": str(generation_id),
            },
        ).one()
        return int(row[0])

    def finish_attempt(
        self,
        *,
        workspace_id: UUID,
        generation_id: UUID,
        attempt_no: int,
        outcome: str,
        failure_codes: Sequence[str] | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        context_digest: str | None = None,
        output_digest: str | None = None,
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE message_generation_attempts
                SET outcome = :outcome, failure_codes = :failure_codes, model = :model,
                    prompt_version = :prompt_version, input_tokens = :input_tokens,
                    output_tokens = :output_tokens, latency_ms = :latency_ms,
                    context_digest = :context_digest, output_digest = :output_digest,
                    finished_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND generation_id = :generation_id
                  AND attempt_no = :attempt_no AND outcome = 'RESERVED'
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "generation_id": str(generation_id),
                "attempt_no": attempt_no,
                "outcome": outcome,
                "failure_codes": _codes(failure_codes),
                "model": model[:128] if model else None,
                "prompt_version": prompt_version,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "context_digest": context_digest,
                "output_digest": output_digest,
            },
        )

    def release_claim(
        self,
        *,
        workspace_id: UUID,
        generation_id: UUID,
        delay_seconds: float,
        refund_attempt: bool,
        transient_error: bool = False,
        failure_codes: Sequence[str] | None = None,
    ) -> None:
        """Give the job back (lease cleared) to be retried after a delay.
        `refund_attempt` undoes the attempt counted at claim time (used when no
        model call was made or the provider failed transiently)."""
        self.session.execute(
            text(
                """
                UPDATE message_generations
                SET lease_owner = NULL, lease_expires_at = NULL,
                    attempt_count = CASE WHEN :refund AND attempt_count > 0
                                         THEN attempt_count - 1 ELSE attempt_count END,
                    transient_error_count = transient_error_count + :transient,
                    next_attempt_at = pg_catalog.transaction_timestamp()
                        + make_interval(secs => :delay),
                    last_failure_codes = COALESCE(:failure_codes, last_failure_codes),
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND id = :generation_id
                  AND state = 'PENDING'
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "generation_id": str(generation_id),
                "delay": float(delay_seconds),
                "refund": refund_attempt,
                "transient": 1 if transient_error else 0,
                "failure_codes": _codes(failure_codes),
            },
        )

    def load_work_item(
        self, *, workspace_id: UUID, generation_id: UUID
    ) -> RowMapping | None:
        return (
            self.session.execute(
                text(
                    """
                    SELECT g.id AS generation_id, g.message_id, g.campaign_id,
                           g.sequence_id, g.step_id, g.enrollment_id,
                           g.attempt_count, g.max_attempts, g.transient_error_count,
                           m.status AS message_status, m.due_at, m.anchor_at,
                           m.created_at AS message_created_at,
                           c.campaign_type, c.status AS campaign_status,
                           c.planning_status,
                           e.state AS enrollment_state, e.next_step_id,
                           e.frozen_variables, e.address_id,
                           ss.email_subject, ss.email_body_html, ss.email_preheader,
                           ss.position AS step_position,
                           ss.version AS step_version,
                           seq.personalization_config,
                           sv.timezone, sv.weekday_set,
                           sv.window_start_local, sv.window_end_local
                    FROM message_generations g
                    JOIN messages m
                      ON m.workspace_id = g.workspace_id AND m.id = g.message_id
                    JOIN campaigns c
                      ON c.workspace_id = g.workspace_id AND c.id = g.campaign_id
                    JOIN campaign_enrollments e
                      ON e.workspace_id = g.workspace_id AND e.id = g.enrollment_id
                    JOIN sequence_steps ss
                      ON ss.workspace_id = g.workspace_id AND ss.id = g.step_id
                    JOIN campaign_sequences seq
                      ON seq.workspace_id = g.workspace_id AND seq.id = g.sequence_id
                    JOIN campaign_settings_versions sv
                      ON sv.workspace_id = c.workspace_id AND sv.id = c.current_settings_id
                    WHERE g.workspace_id = :workspace_id AND g.id = :generation_id
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "generation_id": str(generation_id),
                },
            )
            .mappings()
            .first()
        )

    def load_previous_email(
        self, *, workspace_id: UUID, enrollment_id: UUID, before_position: int
    ) -> RowMapping | None:
        """The most recent earlier email of this enrollment that was sent, with
        the facts/angle recorded when it was generated."""
        return (
            self.session.execute(
                text(
                    """
                    SELECT m2.content_subject, m2.content_body_html, m2.accepted_at,
                           g2.angle, g2.personalization_facts
                    FROM messages m2
                    JOIN sequence_steps s2
                      ON s2.workspace_id = m2.workspace_id AND s2.id = m2.step_id
                    LEFT JOIN message_generations g2
                      ON g2.workspace_id = m2.workspace_id AND g2.message_id = m2.id
                    WHERE m2.workspace_id = :workspace_id
                      AND m2.enrollment_id = :enrollment_id
                      AND m2.purpose = 'CAMPAIGN'
                      AND m2.status = 'SENT'
                      AND s2.position < :before_position
                    ORDER BY s2.position DESC
                    LIMIT 1
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "enrollment_id": str(enrollment_id),
                    "before_position": before_position,
                },
            )
            .mappings()
            .first()
        )

    def lock_for_finalize(
        self, *, workspace_id: UUID, generation_id: UUID
    ) -> RowMapping | None:
        """Re-read the eligibility facts under a row lock on the generation and
        its message, so a concurrent stop (reply/unsubscribe/pause) either
        finished before this read or waits for our commit."""
        return (
            self.session.execute(
                text(
                    """
                    SELECT g.state AS generation_state, g.message_id, g.step_id,
                           g.enrollment_id,
                           m.status AS message_status, m.due_at, m.anchor_at,
                           m.address_id,
                           c.status AS campaign_status, c.planning_status,
                           e.state AS enrollment_state, e.next_step_id,
                           ss.version AS step_version
                    FROM message_generations g
                    JOIN messages m
                      ON m.workspace_id = g.workspace_id AND m.id = g.message_id
                    JOIN campaigns c
                      ON c.workspace_id = g.workspace_id AND c.id = g.campaign_id
                    JOIN campaign_enrollments e
                      ON e.workspace_id = g.workspace_id AND e.id = g.enrollment_id
                    JOIN sequence_steps ss
                      ON ss.workspace_id = g.workspace_id AND ss.id = g.step_id
                    WHERE g.workspace_id = :workspace_id AND g.id = :generation_id
                    FOR UPDATE OF g, m
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "generation_id": str(generation_id),
                },
            )
            .mappings()
            .first()
        )

    def finalize_message(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        subject: str,
        body_html: str,
        content_digest: str,
        renderer_version: int,
        due_at: datetime,
    ) -> bool:
        """Write the immutable snapshot and move PLANNED -> SCHEDULED. Guarded on
        `status='PLANNED' AND rendered_at IS NULL`, so a duplicate finisher or a
        message already stopped changes nothing."""
        row = self.session.execute(
            text(
                """
                UPDATE messages m
                SET content_subject = :subject,
                    content_body_html = :body_html,
                    content_digest = :content_digest,
                    renderer_version = :renderer_version,
                    frozen_destination = e.frozen_destination,
                    frozen_sender_address = mb.original_address,
                    frozen_sender_name = mb.sender_display_name,
                    rendered_at = pg_catalog.transaction_timestamp(),
                    due_at = :due_at,
                    -- ADR-0020: stamp the campaign's current generation so a
                    -- message generated after a resume stays claimable.
                    schedule_generation = (
                        SELECT c.schedule_generation FROM campaigns c
                        WHERE c.workspace_id = m.workspace_id
                          AND c.id = m.campaign_id
                    ),
                    status = 'SCHEDULED'
                FROM campaign_enrollments e, mailboxes mb
                WHERE m.workspace_id = :workspace_id AND m.id = :message_id
                  AND m.status = 'PLANNED' AND m.rendered_at IS NULL
                  AND e.workspace_id = m.workspace_id AND e.id = m.enrollment_id
                  AND mb.workspace_id = m.workspace_id AND mb.id = m.mailbox_id
                RETURNING m.id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "message_id": str(message_id),
                "subject": subject,
                "body_html": body_html,
                "content_digest": content_digest,
                "renderer_version": renderer_version,
                "due_at": due_at,
            },
        ).first()
        return row is not None

    def retime_planned_message(
        self, *, workspace_id: UUID, message_id: UUID, due_at: datetime
    ) -> bool:
        """Move a message that is still waiting to be written to the time its wait
        now implies (ADR-0020: waits can be edited while a campaign is paused).
        Guarded on `PLANNED` and not yet rendered, so a written or stopped message
        is never touched. The timing columns are already granted to this role."""
        row = self.session.execute(
            text(
                """
                UPDATE messages
                SET due_at = :due_at
                WHERE workspace_id = :workspace_id AND id = :message_id
                  AND status = 'PLANNED' AND rendered_at IS NULL
                RETURNING id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "message_id": str(message_id),
                "due_at": due_at,
            },
        ).first()
        return row is not None

    def complete_generation(
        self,
        *,
        workspace_id: UUID,
        generation_id: UUID,
        state: str,
        provider: str | None,
        model: str | None,
        prompt_version: str | None,
        context_digest: str | None,
        fallback_used: bool,
        facts: Sequence[Mapping[str, str]] | None,
        angle: str | None,
        research_status: str | None,
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE message_generations
                SET state = :state, lease_owner = NULL, lease_expires_at = NULL,
                    provider = :provider, model = :model, prompt_version = :prompt_version,
                    context_digest = :context_digest, fallback_used = :fallback_used,
                    personalization_facts = CAST(:facts AS jsonb), angle = :angle,
                    research_status = :research_status,
                    completed_at = pg_catalog.transaction_timestamp(),
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND id = :generation_id
                  AND state = 'PENDING'
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "generation_id": str(generation_id),
                "state": state,
                "provider": provider,
                "model": model[:128] if model else None,
                "prompt_version": prompt_version,
                "context_digest": context_digest,
                "fallback_used": fallback_used,
                "facts": json.dumps(list(facts)) if facts is not None else None,
                "angle": angle[:240] if angle else None,
                "research_status": research_status,
            },
        )

    def fail_generation(
        self,
        *,
        workspace_id: UUID,
        generation_id: UUID,
        message_id: UUID,
        failure_code: str,
        failure_codes: Sequence[str] | None = None,
    ) -> None:
        """Permanent failure: the message is FAILED and will never be sent. The
        progression sweeper then fails the enrollment (ADR-0009)."""
        reason = f"personalization_failed:{failure_code}"[:500]
        self.session.execute(
            text(
                """
                UPDATE messages
                SET status = 'FAILED', terminal_reason = :reason
                WHERE workspace_id = :workspace_id AND id = :message_id
                  AND status = 'PLANNED'
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "message_id": str(message_id),
                "reason": reason,
            },
        )
        self.session.execute(
            text(
                """
                UPDATE message_generations
                SET state = 'FAILED', failure_code = :failure_code,
                    last_failure_codes = COALESCE(:failure_codes, last_failure_codes),
                    lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = pg_catalog.transaction_timestamp(),
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND id = :generation_id
                  AND state = 'PENDING'
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "generation_id": str(generation_id),
                "failure_code": failure_code[:64],
                "failure_codes": _codes(failure_codes),
            },
        )

    def supersede_generation(self, *, workspace_id: UUID, generation_id: UUID) -> None:
        """The message was stopped (reply, unsubscribe, cancel): nothing to do."""
        self.session.execute(
            text(
                """
                UPDATE message_generations
                SET state = 'SUPERSEDED', lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = pg_catalog.transaction_timestamp(),
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND id = :generation_id
                  AND state = 'PENDING'
                """
            ),
            {"workspace_id": str(workspace_id), "generation_id": str(generation_id)},
        )

    # ------------------------------------------------------------------
    # Previews (worker side)
    # ------------------------------------------------------------------

    def claim_preview_rows(
        self, *, workspace_id: UUID, campaign_id: UUID, batch_id: UUID
    ) -> list[RowMapping]:
        """PENDING preview rows of a batch in step order per member, with
        everything needed to generate them."""
        return list(
            self.session.execute(
                text(
                    """
                    SELECT p.id, p.audience_member_id, p.step_id, p.config_digest,
                           ss.position AS step_position, ss.email_subject,
                           ss.email_body_html, ss.email_preheader,
                           am.frozen_variables
                    FROM personalization_previews p
                    JOIN sequence_steps ss
                      ON ss.workspace_id = p.workspace_id AND ss.id = p.step_id
                    JOIN campaign_audience_members am
                      ON am.workspace_id = p.workspace_id AND am.id = p.audience_member_id
                    WHERE p.workspace_id = :workspace_id AND p.campaign_id = :campaign_id
                      AND p.batch_id = :batch_id AND p.state = 'PENDING'
                    ORDER BY p.audience_member_id, ss.position
                    FOR UPDATE OF p SKIP LOCKED
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "batch_id": str(batch_id),
                },
            )
            .mappings()
            .all()
        )

    def load_sequence_config(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> RowMapping | None:
        return (
            self.session.execute(
                text(
                    """
                    SELECT c.status, c.campaign_type, s.id AS sequence_id,
                           s.personalization_config
                    FROM campaigns c
                    LEFT JOIN campaign_sequences s
                      ON s.workspace_id = c.workspace_id AND s.id = c.draft_sequence_id
                    WHERE c.workspace_id = :workspace_id AND c.id = :campaign_id
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .first()
        )

    def list_step_waits(
        self, *, workspace_id: UUID, sequence_id: UUID
    ) -> list[RowMapping]:
        return list(
            self.session.execute(
                text(
                    """
                    SELECT id, position, kind, wait_duration_minutes, updated_at
                    FROM sequence_steps
                    WHERE workspace_id = :workspace_id AND sequence_id = :sequence_id
                    ORDER BY position
                    """
                ),
                {"workspace_id": str(workspace_id), "sequence_id": str(sequence_id)},
            )
            .mappings()
            .all()
        )

    def fail_pending_previews(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        batch_id: UUID,
        failure_codes: Sequence[str],
    ) -> int:
        """End every still-PENDING preview of a batch as FAILED, so the page stops
        waiting for work that will not happen. Only PENDING rows are touched, so
        samples that were finished stay."""
        result = self.session.execute(
            text(
                """
                UPDATE personalization_previews
                SET state = 'FAILED', failure_codes = :failure_codes,
                    completed_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND batch_id = :batch_id AND state = 'PENDING'
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "batch_id": str(batch_id),
                "failure_codes": list(failure_codes),
            },
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def finish_preview(
        self,
        *,
        workspace_id: UUID,
        preview_id: UUID,
        state: str,
        subject: str | None,
        body_html: str | None,
        facts: Sequence[Mapping[str, str]] | None,
        research_summary: Mapping[str, Any] | None,
        fallback_used: bool,
        failure_codes: Sequence[str] | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE personalization_previews
                SET state = :state, subject = :subject, body_html = :body_html,
                    facts = CAST(:facts AS jsonb),
                    research_summary = CAST(:research_summary AS jsonb),
                    fallback_used = :fallback_used, failure_codes = :failure_codes,
                    input_tokens = :input_tokens, output_tokens = :output_tokens,
                    completed_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND id = :preview_id
                  AND state = 'PENDING'
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "preview_id": str(preview_id),
                "state": state,
                "subject": subject,
                "body_html": body_html,
                "facts": json.dumps(list(facts)) if facts is not None else None,
                "research_summary": (
                    json.dumps(dict(research_summary))
                    if research_summary is not None
                    else None
                ),
                "fallback_used": fallback_used,
                "failure_codes": _codes(failure_codes),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )


class GenerationDbProtocol(Protocol):
    def transaction(self) -> Any: ...


class GenerationDb:
    """Opens short worker transactions. Each `with db.transaction() as repo:`
    is one commit unit bound to one workspace and app_worker_general; nothing
    stays open while the runner talks to the network."""

    def __init__(self, session_factory: Any, workspace_id: UUID) -> None:
        self._session_factory = session_factory
        self.workspace_id = workspace_id

    @contextmanager
    def transaction(self) -> Iterator[PersonalizationRepository]:
        session: Session = self._session_factory()
        try:
            if not _is_sqlite(session):
                enter_worker_scope(
                    session, workspace_id=self.workspace_id, role_name=_ROLE
                )
            yield PersonalizationRepository(session)
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()
