from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from app.modules.leads.fields import PROFILE_FIELD_NAMES


def _safe_set_role(session: Session, role_name: str) -> None:
    """Attempt SET LOCAL ROLE if running against PostgreSQL; ignore in SQLite.

    Mirrors app.modules.mailboxes.repository._safe_set_role -- duplicated
    rather than imported, matching the existing per-repository convention.
    """
    bind = session.get_bind()
    if bind and getattr(bind.dialect, "name", "") != "sqlite":
        session.execute(text(f"RESET ROLE; SET LOCAL ROLE {role_name}"))


class CampaignWorkerRepository:
    """Everything requiring the app_worker_general role.

    Kept in a separate file from CampaignRepository (app_api role) so
    app_api-serving code paths can never accidentally import a role-switching
    method. Only workers/campaigns.py constructs this class.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def claim_capture_job(
        self,
        *,
        workspace_id: UUID,
        audience_id: UUID,
        lease_owner: str,
        ttl_seconds: int,
    ) -> RowMapping | None:
        _safe_set_role(self.session, "app_worker_general")
        row = (
            self.session.execute(
                text(
                    """
                    UPDATE campaign_planning_jobs
                    SET state = 'PROCESSING',
                        lease_owner = :lease_owner,
                        lease_generation = lease_generation + 1,
                        lease_expires_at = pg_catalog.transaction_timestamp()
                            + make_interval(secs => :ttl_seconds),
                        next_due_at = pg_catalog.transaction_timestamp()
                            + make_interval(secs => :ttl_seconds)
                    WHERE workspace_id = :workspace_id
                      AND audience_id = :audience_id
                      AND phase = 'CAPTURE'
                      AND state IN ('PENDING', 'PROCESSING')
                      AND (
                          lease_owner IS NULL
                          OR lease_expires_at < pg_catalog.transaction_timestamp()
                          OR lease_owner = :lease_owner
                      )
                    RETURNING *
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "audience_id": str(audience_id),
                    "lease_owner": lease_owner,
                    "ttl_seconds": ttl_seconds,
                },
            )
            .mappings()
            .first()
        )
        return row

    def get_audience(
        self, *, workspace_id: UUID, audience_id: UUID
    ) -> RowMapping | None:
        _safe_set_role(self.session, "app_worker_general")
        row = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_audiences
                    WHERE workspace_id = :workspace_id AND id = :audience_id
                    """
                ),
                {"workspace_id": str(workspace_id), "audience_id": str(audience_id)},
            )
            .mappings()
            .first()
        )
        return row

    def resolve_candidate_lead_ids(
        self, *, workspace_id: UUID, list_ids: Sequence[str], lead_ids: Sequence[str]
    ) -> list[str]:
        """Deduplicated, deterministically ordered candidate lead ids for a
        selection manifest. Computed once (chunk 0) and persisted verbatim
        into campaign_planning_jobs.cursor_data so later chunks resume
        without recomputing -- selection_manifest is bounded to 65536 bytes,
        so this candidate set is bounded too."""
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT DISTINCT lead_id::text AS lead_id FROM (
                        SELECT id AS lead_id FROM leads
                        WHERE workspace_id = :workspace_id AND id = ANY(:lead_ids)
                        UNION
                        SELECT llm.lead_id FROM lead_list_memberships llm
                        WHERE llm.workspace_id = :workspace_id
                          AND llm.list_id = ANY(:list_ids)
                    ) candidates
                    ORDER BY lead_id
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "lead_ids": list(lead_ids),
                    "list_ids": list(list_ids),
                },
            )
            .mappings()
            .all()
        )
        return [str(row["lead_id"]) for row in rows]

    def fetch_lead_capture_rows(
        self,
        *,
        workspace_id: UUID,
        lead_ids: Sequence[str],
        campaign_id: UUID | None = None,
    ) -> list[RowMapping]:
        """One round trip for a whole chunk -- avoids N+1 lead/suppression
        lookups. LEFT JOINs recipient_addresses (by canonical address, since
        leads has no direct address_id FK) and correlates the suppression
        check per row rather than issuing one query per lead."""
        _safe_set_role(self.session, "app_worker_general")
        # Every profile field must be selected here: build_lead_render_context reads
        # them from this row, and a missing column would render as empty in the
        # frozen snapshot rather than fail.
        profile_columns = ", ".join(f"l.{name}" for name in PROFILE_FIELD_NAMES)
        rows = (
            self.session.execute(
                text(
                    f"""
                    SELECT
                        l.id AS lead_id, l.status AS lead_status,
                        l.contact_revision, l.first_name, l.last_name,
                        l.company, l.title, {profile_columns}, l.custom_fields,
                        l.original_address, l.canonical_address,
                        ra.id AS address_id,
                        EXISTS (
                            SELECT 1 FROM suppressions s
                            WHERE s.workspace_id = l.workspace_id
                              AND s.address_id = ra.id
                              AND s.status = 'ACTIVE'
                        ) AS is_suppressed,
                        -- Supplement capture (ADR-0020): someone already enrolled in
                        -- this campaign, by lead or by address, is never offered twice.
                        (
                            CAST(:campaign_id AS uuid) IS NOT NULL
                            AND EXISTS (
                                SELECT 1 FROM campaign_enrollments e
                                WHERE e.workspace_id = l.workspace_id
                                  AND e.campaign_id = CAST(:campaign_id AS uuid)
                                  AND (e.lead_id = l.id OR e.address_id = ra.id)
                            )
                        ) AS is_enrolled
                    FROM leads l
                    LEFT JOIN recipient_addresses ra
                      ON ra.workspace_id = l.workspace_id
                     AND ra.canonical_address = l.canonical_address
                     AND ra.normalization_version = l.normalization_version
                    WHERE l.workspace_id = :workspace_id AND l.id = ANY(:lead_ids)
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "lead_ids": list(lead_ids),
                    "campaign_id": str(campaign_id) if campaign_id else None,
                },
            )
            .mappings()
            .all()
        )
        return list(rows)

    def insert_audience_members(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        audience_id: UUID,
        members: list[dict[str, Any]],
    ) -> None:
        if not members:
            return
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                INSERT INTO campaign_audience_members
                    (id, workspace_id, campaign_id, audience_id, lead_id, address_id,
                     capture_ordinal, contact_revision, frozen_variables,
                     eligibility_status, exclusion_reason)
                VALUES
                    (:id, :workspace_id, :campaign_id, :audience_id, :lead_id,
                     :address_id, :capture_ordinal, :contact_revision,
                     CAST(:frozen_variables AS jsonb),
                     :eligibility_status, :exclusion_reason)
                ON CONFLICT DO NOTHING
                """
            ),
            [
                {
                    "id": str(uuid.uuid4()),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "audience_id": str(audience_id),
                    **member,
                    "frozen_variables": json.dumps(member["frozen_variables"]),
                }
                for member in members
            ],
        )

    def update_job_progress(
        self,
        *,
        workspace_id: UUID,
        job_id: UUID,
        cursor_data: str,
        processed_count: int,
        lease_owner: str,
        ttl_seconds: int,
    ) -> None:
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE campaign_planning_jobs
                SET cursor_data = :cursor_data,
                    processed_count = :processed_count,
                    lease_owner = :lease_owner,
                    lease_expires_at = pg_catalog.transaction_timestamp()
                        + make_interval(secs => :ttl_seconds),
                    next_due_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "job_id": str(job_id),
                "cursor_data": cursor_data,
                "processed_count": processed_count,
                "lease_owner": lease_owner,
                "ttl_seconds": ttl_seconds,
            },
        )

    def complete_capture(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        audience_id: UUID,
        job_id: UUID,
        processed_count: int,
        source_manifest_digest: str,
    ) -> None:
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE campaign_planning_jobs
                SET state = 'READY', processed_count = :processed_count,
                    next_due_at = NULL
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "job_id": str(job_id),
                "processed_count": processed_count,
            },
        )
        self.session.execute(
            text(
                """
                UPDATE campaign_audiences
                SET status = 'READY',
                    source_manifest_digest = :digest,
                    completed_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND id = :audience_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "audience_id": str(audience_id),
                "digest": source_manifest_digest,
            },
        )
        self._release_capture_sources(
            workspace_id=workspace_id, audience_id=audience_id
        )

    def fail_capture(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        audience_id: UUID,
        job_id: UUID,
        error_reason: str,
    ) -> None:
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE campaign_planning_jobs
                SET state = 'FAILED', error_reason = :error_reason, next_due_at = NULL
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "job_id": str(job_id),
                "error_reason": error_reason[:500],
            },
        )
        self.session.execute(
            text(
                """
                UPDATE campaign_audiences
                SET status = 'FAILED', completed_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND id = :audience_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "audience_id": str(audience_id),
            },
        )
        self._release_capture_sources(
            workspace_id=workspace_id, audience_id=audience_id
        )

    def abandon_capture(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        audience_id: UUID,
        job_id: UUID,
    ) -> None:
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE campaign_planning_jobs
                SET state = 'FAILED', error_reason = 'abandoned_by_user',
                    next_due_at = NULL
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {"workspace_id": str(workspace_id), "job_id": str(job_id)},
        )
        self.session.execute(
            text(
                """
                UPDATE campaign_audiences
                SET status = 'ABANDONED',
                    completed_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND id = :audience_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "audience_id": str(audience_id),
            },
        )
        self._release_capture_sources(
            workspace_id=workspace_id, audience_id=audience_id
        )

    # -------------------------------------------------------------------
    # Activation planning (ENROLL / RENDER phases)
    # -------------------------------------------------------------------

    def claim_planning_job(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        activation_id: UUID,
        phase: str,
        lease_owner: str,
        ttl_seconds: int,
        audience_id: UUID | None = None,
    ) -> RowMapping | None:
        """Claim the activation's own job, or -- with `audience_id` -- the job of
        one supplement audience (ADR-0020); the two never share a row."""
        _safe_set_role(self.session, "app_worker_general")
        row = (
            self.session.execute(
                text(
                    """
                    UPDATE campaign_planning_jobs
                    SET state = 'PROCESSING',
                        lease_owner = :lease_owner,
                        lease_generation = lease_generation + 1,
                        lease_expires_at = pg_catalog.transaction_timestamp()
                            + make_interval(secs => :ttl_seconds),
                        next_due_at = pg_catalog.transaction_timestamp()
                            + make_interval(secs => :ttl_seconds)
                    WHERE workspace_id = :workspace_id
                      AND campaign_id = :campaign_id
                      AND activation_id = :activation_id
                      AND phase = :phase
                      AND audience_id IS NOT DISTINCT FROM CAST(:audience_id AS uuid)
                      AND state IN ('PENDING', 'PROCESSING')
                      AND (
                          lease_owner IS NULL
                          OR lease_expires_at < pg_catalog.transaction_timestamp()
                          OR lease_owner = :lease_owner
                      )
                    RETURNING *
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "activation_id": str(activation_id),
                    "phase": phase,
                    "audience_id": str(audience_id) if audience_id else None,
                    "lease_owner": lease_owner,
                    "ttl_seconds": ttl_seconds,
                },
            )
            .mappings()
            .first()
        )
        return row

    def get_activation_context(
        self, *, workspace_id: UUID, campaign_id: UUID, activation_id: UUID
    ) -> RowMapping | None:
        """Guards on activation_id matching the campaign's *current*
        activation: if the campaign has since been reactivated (a new
        activation_id), this returns None so the caller can recognize its
        job as stale work superseded by a fresh activation, not a failure."""
        _safe_set_role(self.session, "app_worker_general")
        row = (
            self.session.execute(
                text(
                    """
                    SELECT c.start_at, c.activated_audience_id, c.campaign_type,
                           c.activated_sequence_id, sv.timezone, sv.weekday_set,
                           sv.window_start_local, sv.window_end_local,
                           sv.daily_limit,
                           ss.id AS first_step_id
                    FROM campaigns c
                    JOIN campaign_settings_versions sv
                      ON sv.workspace_id = c.workspace_id
                     AND sv.id = c.current_settings_id
                    JOIN sequence_steps ss
                      ON ss.workspace_id = c.workspace_id
                     AND ss.sequence_id = c.activated_sequence_id
                     AND ss.position = 1
                    WHERE c.workspace_id = :workspace_id AND c.id = :campaign_id
                      AND c.activation_id = :activation_id
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "activation_id": str(activation_id),
                },
            )
            .mappings()
            .first()
        )
        return row

    def count_campaign_enrollments(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> int:
        _safe_set_role(self.session, "app_worker_general")
        value = self.session.execute(
            text(
                """
                SELECT count(*) FROM campaign_enrollments
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        ).scalar()
        return int(value or 0)

    def list_eligible_campaign_mailboxes(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> list[RowMapping]:
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT mailbox_id FROM campaign_mailboxes
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                      AND active = true
                    ORDER BY allocation_position ASC
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .all()
        )
        return list(rows)

    def list_mailbox_limit_policies(
        self, *, workspace_id: UUID, mailbox_ids: list[UUID]
    ) -> dict[UUID, list[dict[str, int]]]:
        """Each mailbox's MAILBOX-kind limit rows, for spreading a campaign's
        messages (see campaigns/pacing.py). A mailbox with none is absent."""
        if not mailbox_ids:
            return {}
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT mailbox_id, window_seconds, limit_value, min_spacing_seconds
                    FROM tenant_rate_policies
                    WHERE workspace_id = :workspace_id AND kind = 'MAILBOX'
                      AND unit = 'MESSAGE' AND mailbox_id = ANY(:mailbox_ids)
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "mailbox_ids": [str(m) for m in mailbox_ids],
                },
            )
            .mappings()
            .all()
        )
        result: dict[UUID, list[dict[str, int]]] = {}
        for row in rows:
            result.setdefault(UUID(str(row["mailbox_id"])), []).append(
                {
                    "window_seconds": int(row["window_seconds"]),
                    "limit_value": int(row["limit_value"]),
                    "min_spacing_seconds": int(row["min_spacing_seconds"] or 0),
                }
            )
        return result

    def fetch_accepted_audience_members_chunk(
        self,
        *,
        workspace_id: UUID,
        audience_id: UUID,
        after_ordinal: int,
        limit: int,
    ) -> list[RowMapping]:
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT m.id AS audience_member_id, m.lead_id, m.address_id,
                           m.capture_ordinal, m.frozen_variables,
                           ra.canonical_address
                    FROM campaign_audience_members m
                    JOIN recipient_addresses ra
                      ON ra.workspace_id = m.workspace_id AND ra.id = m.address_id
                    WHERE m.workspace_id = :workspace_id
                      AND m.audience_id = :audience_id
                      AND m.eligibility_status = 'ACCEPTED'
                      AND m.capture_ordinal > :after_ordinal
                    ORDER BY m.capture_ordinal ASC
                    LIMIT :limit
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "audience_id": str(audience_id),
                    "after_ordinal": after_ordinal,
                    "limit": limit,
                },
            )
            .mappings()
            .all()
        )
        return list(rows)

    def insert_enrollments(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        audience_id: UUID,
        sequence_id: UUID,
        first_step_id: UUID,
        rows: list[dict[str, Any]],
    ) -> None:
        """rows: each dict with audience_member_id, lead_id, address_id,
        frozen_destination, frozen_variables, assigned_mailbox_id.
        ON CONFLICT DO NOTHING against campaign_enrollments' unique
        (workspace_id, campaign_id, lead_id) / (..., address_id) keys makes a
        redelivered chunk converge on the same rows rather than duplicate."""
        if not rows:
            return
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                INSERT INTO campaign_enrollments
                    (id, workspace_id, campaign_id, audience_id, audience_member_id,
                     sequence_id, lead_id, address_id, frozen_destination,
                     frozen_variables, assigned_mailbox_id, state, next_step_id,
                     next_sequence_position)
                VALUES
                    (:id, :workspace_id, :campaign_id, :audience_id,
                     :audience_member_id, :sequence_id, :lead_id, :address_id,
                     :frozen_destination, CAST(:frozen_variables AS jsonb),
                     :assigned_mailbox_id, 'ACTIVE', :next_step_id, 1)
                ON CONFLICT DO NOTHING
                """
            ),
            [
                {
                    "id": str(uuid.uuid4()),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "audience_id": str(audience_id),
                    "sequence_id": str(sequence_id),
                    "next_step_id": str(first_step_id),
                    **row,
                    "frozen_variables": json.dumps(row["frozen_variables"]),
                }
                for row in rows
            ],
        )

    def get_enrollments_for_leads(
        self, *, workspace_id: UUID, campaign_id: UUID, lead_ids: Sequence[str]
    ) -> list[RowMapping]:
        if not lead_ids:
            return []
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT id, lead_id, assigned_mailbox_id, address_id
                    FROM campaign_enrollments
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                      AND lead_id = ANY(:lead_ids)
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "lead_ids": list(lead_ids),
                },
            )
            .mappings()
            .all()
        )
        return list(rows)

    def insert_planned_first_messages(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        sequence_id: UUID,
        step_id: UUID,
        rows: list[dict[str, Any]],
    ) -> None:
        """rows: each dict with enrollment_id, mailbox_id, address_id.
        ON CONFLICT DO NOTHING against messages_enrollment_step_key -- a
        retried chunk converges on the same message row, never a duplicate."""
        if not rows:
            return
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                INSERT INTO messages
                    (id, workspace_id, purpose, campaign_id, enrollment_id,
                     sequence_id, step_id, mailbox_id, address_id, status)
                VALUES
                    (:id, :workspace_id, 'CAMPAIGN', :campaign_id, :enrollment_id,
                     :sequence_id, :step_id, :mailbox_id, :address_id, 'PLANNED')
                ON CONFLICT DO NOTHING
                """
            ),
            [
                {
                    "id": str(uuid.uuid4()),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "sequence_id": str(sequence_id),
                    "step_id": str(step_id),
                    **row,
                }
                for row in rows
            ],
        )

    def complete_enroll_job(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        job_id: UUID,
        processed_count: int,
        mark_planning_ready: bool = True,
    ) -> None:
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE campaign_planning_jobs
                SET state = 'READY', processed_count = :processed_count,
                    next_due_at = NULL
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "job_id": str(job_id),
                "processed_count": processed_count,
            },
        )
        if not mark_planning_ready:
            # A supplement joins a campaign whose planning is already READY.
            return
        self.session.execute(
            text(
                """
                UPDATE campaigns SET planning_status = 'READY'
                WHERE workspace_id = :workspace_id AND id = :campaign_id
                """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        )

    def fetch_planned_messages_chunk(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        limit: int,
        hyper: bool = False,
    ) -> list[RowMapping]:
        """FOR UPDATE OF m SKIP LOCKED: rows leave PLANNED as they're
        rendered, so redelivery is naturally idempotent and multiple RENDER
        workers can safely run concurrently over the same campaign.

        For a hyper-personalized campaign (ADR-0011) a message stays PLANNED until
        its content is generated, so "not yet handled" is "PLANNED without a
        generation row" instead. The standard query is unchanged, and does not
        touch message_generations (so it works before migration 0028)."""
        _safe_set_role(self.session, "app_worker_general")
        pending_filter = (
            """AND NOT EXISTS (
                          SELECT 1 FROM message_generations g
                          WHERE g.workspace_id = m.workspace_id AND g.message_id = m.id
                      )"""
            if hyper
            else ""
        )
        rows = (
            self.session.execute(
                text(
                    f"""
                    SELECT m.id, m.mailbox_id, m.enrollment_id, m.sequence_id,
                           m.step_id, e.frozen_variables, e.frozen_destination,
                           ss.email_subject, ss.email_body_html, ss.email_preheader,
                           mb.original_address AS sender_address,
                           mb.sender_display_name AS sender_name,
                           am.capture_ordinal
                    FROM messages m
                    JOIN campaign_enrollments e
                      ON e.workspace_id = m.workspace_id AND e.id = m.enrollment_id
                    JOIN campaign_audience_members am
                      ON am.workspace_id = e.workspace_id
                     AND am.id = e.audience_member_id
                    JOIN sequence_steps ss
                      ON ss.workspace_id = m.workspace_id AND ss.id = m.step_id
                    JOIN mailboxes mb
                      ON mb.workspace_id = m.workspace_id AND mb.id = m.mailbox_id
                    WHERE m.workspace_id = :workspace_id
                      AND m.campaign_id = :campaign_id
                      AND m.status = 'PLANNED'
                      {pending_filter}
                    ORDER BY m.id
                    LIMIT :limit
                    FOR UPDATE OF m SKIP LOCKED
                    """  # noqa: S608 -- pending_filter is a fixed constant above
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
        return list(rows)

    def render_message(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        content_subject: str,
        content_body_html: str,
        content_digest: str,
        frozen_destination: str,
        frozen_sender_address: str,
        frozen_sender_name: str | None,
        due_at: Any,
        anchor_at: Any,
    ) -> None:
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE messages
                SET content_subject = :content_subject,
                    content_body_html = :content_body_html,
                    content_digest = :content_digest,
                    frozen_destination = :frozen_destination,
                    frozen_sender_address = :frozen_sender_address,
                    frozen_sender_name = :frozen_sender_name,
                    rendered_at = pg_catalog.transaction_timestamp(),
                    due_at = :due_at,
                    anchor_at = :anchor_at,
                    -- ADR-0020: a message must carry the campaign's current
                    -- generation, or the scheduler would never claim it after a
                    -- resume bumped the campaign's.
                    schedule_generation = (
                        SELECT c.schedule_generation FROM campaigns c
                        WHERE c.workspace_id = messages.workspace_id
                          AND c.id = messages.campaign_id
                    ),
                    status = 'SCHEDULED'
                WHERE workspace_id = :workspace_id AND id = :message_id
                  AND status = 'PLANNED'
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "message_id": str(message_id),
                "content_subject": content_subject,
                "content_body_html": content_body_html,
                "content_digest": content_digest,
                "frozen_destination": frozen_destination,
                "frozen_sender_address": frozen_sender_address,
                "frozen_sender_name": frozen_sender_name,
                "due_at": due_at,
                "anchor_at": anchor_at,
            },
        )

    def mark_generation_pending(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        sequence_id: UUID,
        step_id: UUID,
        enrollment_id: UUID,
        message_id: UUID,
        due_at: Any,
        anchor_at: Any,
        next_attempt_at: Any,
        max_attempts: int,
    ) -> bool:
        """Hyper-personalized message (ADR-0011): keep it PLANNED with its
        intended due/anchor time and create the durable generation job that the
        personalization worker will pick up one lead time before it is due.
        Returns False when a job already exists (a retried chunk converges)."""
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE messages
                SET due_at = :due_at, anchor_at = :anchor_at
                WHERE workspace_id = :workspace_id AND id = :message_id
                  AND status = 'PLANNED' AND rendered_at IS NULL
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "message_id": str(message_id),
                "due_at": due_at,
                "anchor_at": anchor_at,
            },
        )
        row = self.session.execute(
            text(
                """
                INSERT INTO message_generations
                    (id, workspace_id, campaign_id, sequence_id, step_id,
                     enrollment_id, message_id, state, max_attempts, next_attempt_at)
                VALUES
                    (:id, :workspace_id, :campaign_id, :sequence_id, :step_id,
                     :enrollment_id, :message_id, 'PENDING', :max_attempts,
                     :next_attempt_at)
                ON CONFLICT (workspace_id, message_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "sequence_id": str(sequence_id),
                "step_id": str(step_id),
                "enrollment_id": str(enrollment_id),
                "message_id": str(message_id),
                "max_attempts": max_attempts,
                "next_attempt_at": next_attempt_at,
            },
        ).first()
        return row is not None

    # -------------------------------------------------------------------
    # Follow-up progression (see progression.py / docs/adr/0009). Every
    # statement is workspace-scoped and runs as app_worker_general, which
    # already holds the messages INSERT/UPDATE and campaign_enrollments UPDATE
    # column grants used below (0003/0004) -- no new privileges.
    # -------------------------------------------------------------------

    def get_progression_context(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> RowMapping | None:
        """Only a RUNNING campaign with READY planning is progressed; anything
        else (paused, completed, still planning) yields None."""
        _safe_set_role(self.session, "app_worker_general")
        return (
            self.session.execute(
                text(
                    """
                    SELECT c.activated_sequence_id, c.schedule_generation,
                           c.campaign_type,
                           sv.timezone, sv.weekday_set,
                           sv.window_start_local, sv.window_end_local,
                           sv.daily_limit
                    FROM campaigns c
                    JOIN campaign_settings_versions sv
                      ON sv.workspace_id = c.workspace_id
                     AND sv.id = c.current_settings_id
                    WHERE c.workspace_id = :workspace_id AND c.id = :campaign_id
                      AND c.status = 'RUNNING' AND c.planning_status = 'READY'
                      AND c.activated_sequence_id IS NOT NULL
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .first()
        )

    def lock_campaign_for_planning(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> None:
        """Serialise work that reads the campaign's unsent schedule and then adds to
        it (spreading overdue follow-ups, the post-resume reconcile): two of them
        reading the same free slots would plan two messages into one. FOR NO KEY
        UPDATE conflicts with itself and with FOR UPDATE (pause, send) but not with
        the FOR KEY SHARE that inserting a message takes, so it never blocks the
        inserts it protects. Taken only when a batch actually has overdue work."""
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                SELECT 1 FROM campaigns
                WHERE workspace_id = :workspace_id AND id = :campaign_id
                FOR NO KEY UPDATE
                """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        )

    def lock_campaign_for_reopen(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> RowMapping | None:
        """Lock the campaign row and return what the reopen is bound to. Pause takes
        the same row, so a pause either finished before this read or waits for our
        commit (a reopened lead of a paused campaign is harmless: progression only
        runs for RUNNING campaigns)."""
        _safe_set_role(self.session, "app_worker_general")
        return (
            self.session.execute(
                text(
                    """
                    SELECT status, campaign_type, schedule_generation
                    FROM campaigns
                    WHERE workspace_id = :workspace_id AND id = :campaign_id
                    FOR NO KEY UPDATE
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .first()
        )

    def fetch_reopen_candidates(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[RowMapping]:
        """COMPLETED enrollments that can take an email appended after their last one:
        the last email they were sent (highest step position) is SENT, a later EMAIL
        step exists, no message exists for any later step, a mailbox is still assigned
        and there is no recipient outcome (reply, unsubscribe, bounce, complaint).
        FOR UPDATE OF e SKIP LOCKED, like fetch_progressable_enrollments."""
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT e.id AS enrollment_id, e.address_id,
                           last.step_id, last.position
                    FROM campaign_enrollments e
                    JOIN LATERAL (
                        SELECT m.step_id, m.status, m.sequence_id, ss.position
                        FROM messages m
                        JOIN sequence_steps ss
                          ON ss.workspace_id = m.workspace_id AND ss.id = m.step_id
                        WHERE m.workspace_id = e.workspace_id
                          AND m.enrollment_id = e.id
                          AND m.purpose = 'CAMPAIGN'
                        ORDER BY ss.position DESC
                        LIMIT 1
                    ) last ON true
                    WHERE e.workspace_id = :workspace_id
                      AND e.campaign_id = :campaign_id
                      AND e.state = 'COMPLETED'
                      AND e.assigned_mailbox_id IS NOT NULL
                      AND last.status = 'SENT'
                      AND EXISTS (
                          SELECT 1 FROM sequence_steps s2
                          WHERE s2.workspace_id = e.workspace_id
                            AND s2.sequence_id = last.sequence_id
                            AND s2.position > last.position
                            AND s2.kind = 'EMAIL'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM recipient_outcomes r
                          WHERE r.workspace_id = e.workspace_id
                            AND r.enrollment_id = e.id
                      )
                      AND (CAST(:after_id AS uuid) IS NULL
                           OR e.id > CAST(:after_id AS uuid))
                    ORDER BY e.id
                    LIMIT :limit
                    FOR UPDATE OF e SKIP LOCKED
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "limit": limit,
                    "after_id": str(after_id) if after_id else None,
                },
            )
            .mappings()
            .all()
        )
        return list(rows)

    def reopen_enrollment(
        self,
        *,
        workspace_id: UUID,
        enrollment_id: UUID,
        step_id: UUID,
        position: int,
    ) -> bool:
        """COMPLETED -> ACTIVE, pointing at the last (SENT) email. Guarded on
        state = COMPLETED, so a replay or a lead that already moved changes nothing."""
        _safe_set_role(self.session, "app_worker_general")
        row = self.session.execute(
            text(
                """
                UPDATE campaign_enrollments
                SET state = 'ACTIVE',
                    next_step_id = :step_id,
                    next_sequence_position = :position
                WHERE workspace_id = :workspace_id AND id = :enrollment_id
                  AND state = 'COMPLETED' AND next_step_id IS NULL
                RETURNING id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "enrollment_id": str(enrollment_id),
                "step_id": str(step_id),
                "position": position,
            },
        ).first()
        return row is not None

    def list_mailbox_unsent_hourly(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        from_at: Any,
        horizon_days: int = 90,
    ) -> dict[UUID, list[tuple[Any, int]]]:
        """Per mailbox, how many unsent campaign messages are due in each future
        hour -- compact however large the campaign is (see MailboxOccupancy)."""
        _safe_set_role(self.session, "app_worker_general")
        rows = self.session.execute(
            text(
                """
                SELECT mailbox_id, pg_catalog.date_trunc('hour', due_at) AS hour,
                       count(*) AS n
                FROM messages
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND purpose = 'CAMPAIGN'
                  AND status IN ('PLANNED', 'SCHEDULED', 'RETRY_SCHEDULED')
                  AND due_at >= :from_at
                  AND due_at < CAST(:from_at AS timestamptz)
                                + make_interval(days => :horizon_days)
                GROUP BY mailbox_id, 2
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "from_at": from_at,
                "horizon_days": horizon_days,
            },
        ).all()
        by_mailbox: dict[UUID, list[tuple[Any, int]]] = {}
        for mailbox_id, hour, count in rows:
            by_mailbox.setdefault(UUID(str(mailbox_id)), []).append((hour, int(count)))
        return by_mailbox

    # -------------------------------------------------------------------
    # Post-resume reconcile (reconcile.py / docs/adr/0020). Runs as
    # app_worker_general, which already holds UPDATE on messages.due_at and
    # messages.schedule_generation (0004) -- no new privileges.
    # -------------------------------------------------------------------

    def get_reconcile_context(
        self, *, workspace_id: UUID, campaign_id: UUID, generation: int
    ) -> RowMapping | None:
        """None unless the campaign is RUNNING/READY *at exactly this
        generation*: a later pause or resume supersedes the task, which then does
        nothing (the newer resume dispatched its own)."""
        _safe_set_role(self.session, "app_worker_general")
        return (
            self.session.execute(
                text(
                    """
                    SELECT c.activated_sequence_id, c.schedule_generation,
                           c.campaign_type, sv.timezone, sv.weekday_set,
                           sv.window_start_local, sv.window_end_local,
                           sv.daily_limit
                    FROM campaigns c
                    JOIN campaign_settings_versions sv
                      ON sv.workspace_id = c.workspace_id
                     AND sv.id = c.current_settings_id
                    WHERE c.workspace_id = :workspace_id AND c.id = :campaign_id
                      AND c.status = 'RUNNING' AND c.planning_status = 'READY'
                      AND c.schedule_generation = :generation
                      AND c.activated_sequence_id IS NOT NULL
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "generation": generation,
                },
            )
            .mappings()
            .first()
        )

    def fetch_stale_messages_chunk(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        generation: int,
        limit: int,
        after_due_at: Any = None,
        after_id: UUID | None = None,
    ) -> list[RowMapping]:
        """Unsent messages stamped with an older generation, oldest due first.
        QUEUED/SENDING/UNKNOWN_OUTCOME are deliberately absent: they are already
        past the send gate and are never touched here. FOR UPDATE ... SKIP
        LOCKED so concurrent passes split the work; the (due_at, id) keyset
        guarantees progress past rows that are skipped."""
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT m.id, m.status, m.due_at, m.anchor_at, m.next_retry_at,
                           m.mailbox_id, m.step_id, m.schedule_generation,
                           m.content_digest, m.renderer_version, m.rfc_message_id,
                           m.dispatch_origin,
                           e.frozen_variables,
                           ss.email_subject, ss.email_body_html, ss.email_preheader,
                           EXISTS (
                               SELECT 1 FROM message_attempts a
                               WHERE a.workspace_id = m.workspace_id
                                 AND a.message_id = m.id
                                 AND a.evidence_state IN
                                     ('PREPARED', 'ACCEPTED', 'UNKNOWN')
                           ) AS has_live_attempt
                    FROM messages m
                    JOIN campaign_enrollments e
                      ON e.workspace_id = m.workspace_id AND e.id = m.enrollment_id
                    JOIN sequence_steps ss
                      ON ss.workspace_id = m.workspace_id AND ss.id = m.step_id
                    WHERE m.workspace_id = :workspace_id
                      AND m.campaign_id = :campaign_id
                      AND m.purpose = 'CAMPAIGN'
                      AND m.status IN ('SCHEDULED', 'RETRY_SCHEDULED')
                      AND m.schedule_generation < :generation
                      AND (CAST(:after_id AS uuid) IS NULL
                           OR (m.due_at, m.id) >
                              (CAST(:after_due_at AS timestamptz),
                               CAST(:after_id AS uuid)))
                    ORDER BY m.due_at, m.id
                    LIMIT :limit
                    FOR UPDATE OF m SKIP LOCKED
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "generation": generation,
                    "limit": limit,
                    "after_due_at": after_due_at,
                    "after_id": str(after_id) if after_id else None,
                },
            )
            .mappings()
            .all()
        )
        return list(rows)

    def reschedule_message(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        expected_status: str,
        due_at: Any,
        generation: int,
        content: tuple[str, str, str] | None = None,
    ) -> bool:
        """Set the new time and stamp the generation. Guarded on the status the
        row was read in and on an *older* generation, so a replay, a concurrent
        claim or a newer resume changes nothing. RETRY_SCHEDULED keeps
        due_at >= next_retry_at (caller guarantees it; the CHECK enforces it).

        `content` = (subject, body_html, digest) rewrites the rendered snapshot
        in the same statement. That is only legal through the narrow exemption in
        app_guard_message_snapshot (migration 0042), which this switches on for
        the transaction and which the database re-checks row by row."""
        _safe_set_role(self.session, "app_worker_general")
        params: dict[str, Any] = {
            "workspace_id": str(workspace_id),
            "message_id": str(message_id),
            "expected_status": expected_status,
            "due_at": due_at,
            "generation": generation,
        }
        content_sql = ""
        if content is not None:
            self.session.execute(
                text("SELECT pg_catalog.set_config('app.reconcile', 'on', true)")
            )
            content_sql = (
                ", content_subject = :content_subject"
                ", content_body_html = :content_body_html"
                ", content_digest = :content_digest"
            )
            params.update(
                content_subject=content[0],
                content_body_html=content[1],
                content_digest=content[2],
            )
        row = self.session.execute(
            text(
                f"""
                UPDATE messages
                SET due_at = :due_at, schedule_generation = :generation{content_sql}
                WHERE workspace_id = :workspace_id AND id = :message_id
                  AND status = :expected_status
                  AND schedule_generation < :generation
                RETURNING id
                """  # noqa: S608 -- content_sql is a fixed constant above
            ),
            params,
        ).first()
        if content is not None:
            self.session.execute(
                text("SELECT pg_catalog.set_config('app.reconcile', 'off', true)")
            )
        return row is not None

    def list_sequence_steps(
        self, *, workspace_id: UUID, sequence_id: UUID
    ) -> list[RowMapping]:
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT id, position, kind, wait_duration_minutes,
                           email_subject, email_body_html, email_preheader
                    FROM sequence_steps
                    WHERE workspace_id = :workspace_id AND sequence_id = :sequence_id
                    ORDER BY position ASC
                    """
                ),
                {"workspace_id": str(workspace_id), "sequence_id": str(sequence_id)},
            )
            .mappings()
            .all()
        )
        return list(rows)

    def fetch_progressable_enrollments(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        limit: int,
        after_id: UUID | None = None,
    ) -> list[RowMapping]:
        """ACTIVE enrollments whose current email (the one they still point at)
        has a final outcome: SENT (provider accepted it) or FAILED.

        FOR UPDATE OF e SKIP LOCKED: concurrent sweeps split the work, and the
        row lock is held for the rest of the transaction so nothing else moves
        the enrollment while its follow-up is built."""
        _safe_set_role(self.session, "app_worker_general")
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT e.id AS enrollment_id, e.address_id,
                           e.assigned_mailbox_id, e.frozen_variables,
                           e.frozen_destination, e.next_step_id,
                           m.status AS message_status, m.accepted_at,
                           mb.original_address AS sender_address,
                           mb.sender_display_name AS sender_name
                    FROM campaign_enrollments e
                    JOIN messages m
                      ON m.workspace_id = e.workspace_id
                     AND m.enrollment_id = e.id
                     AND m.step_id = e.next_step_id
                    LEFT JOIN mailboxes mb
                      ON mb.workspace_id = e.workspace_id
                     AND mb.id = e.assigned_mailbox_id
                    WHERE e.workspace_id = :workspace_id
                      AND e.campaign_id = :campaign_id
                      AND e.state = 'ACTIVE'
                      AND e.next_step_id IS NOT NULL
                      AND m.purpose = 'CAMPAIGN'
                      AND m.status IN ('SENT', 'FAILED')
                      AND (CAST(:after_id AS uuid) IS NULL
                           OR e.id > CAST(:after_id AS uuid))
                    ORDER BY e.id
                    LIMIT :limit
                    FOR UPDATE OF e SKIP LOCKED
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "limit": limit,
                    "after_id": str(after_id) if after_id else None,
                },
            )
            .mappings()
            .all()
        )
        return list(rows)

    def insert_followup_message(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        sequence_id: UUID,
        step_id: UUID,
        enrollment_id: UUID,
        mailbox_id: UUID,
        address_id: UUID,
        schedule_generation: int,
    ) -> UUID | None:
        """PLANNED message for a later step. None when a message for this
        (enrollment, step) already exists -- the unique key makes a retried or
        raced insert converge instead of duplicating. The campaign's current
        schedule_generation is stamped so the scheduler (which only claims
        messages matching it) will pick this one up."""
        _safe_set_role(self.session, "app_worker_general")
        new_id = uuid.uuid4()
        inserted = self.session.execute(
            text(
                """
                INSERT INTO messages
                    (id, workspace_id, purpose, campaign_id, enrollment_id,
                     sequence_id, step_id, mailbox_id, address_id,
                     schedule_generation, status)
                VALUES
                    (:id, :workspace_id, 'CAMPAIGN', :campaign_id, :enrollment_id,
                     :sequence_id, :step_id, :mailbox_id, :address_id,
                     :schedule_generation, 'PLANNED')
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": str(new_id),
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "enrollment_id": str(enrollment_id),
                "sequence_id": str(sequence_id),
                "step_id": str(step_id),
                "mailbox_id": str(mailbox_id),
                "address_id": str(address_id),
                "schedule_generation": schedule_generation,
            },
        ).first()
        return new_id if inserted is not None else None

    def advance_enrollment(
        self,
        *,
        workspace_id: UUID,
        enrollment_id: UUID,
        expected_step_id: UUID,
        next_step_id: UUID,
        next_position: int,
        accepted_at: Any,
    ) -> bool:
        """Move the pointer to the next email and record the acceptance.
        Guarded on the step it is expected to still point at, so a replay after
        the pointer already moved changes nothing (returns False)."""
        _safe_set_role(self.session, "app_worker_general")
        row = self.session.execute(
            text(
                """
                UPDATE campaign_enrollments
                SET next_step_id = :next_step_id,
                    next_sequence_position = :next_position,
                    first_acceptance_at =
                        COALESCE(first_acceptance_at, :accepted_at),
                    last_acceptance_at = :accepted_at
                WHERE workspace_id = :workspace_id AND id = :enrollment_id
                  AND state = 'ACTIVE' AND next_step_id = :expected_step_id
                RETURNING id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "enrollment_id": str(enrollment_id),
                "expected_step_id": str(expected_step_id),
                "next_step_id": str(next_step_id),
                "next_position": next_position,
                "accepted_at": accepted_at,
            },
        ).first()
        return row is not None

    def complete_enrollment(
        self,
        *,
        workspace_id: UUID,
        enrollment_id: UUID,
        expected_step_id: UUID,
        accepted_at: Any,
    ) -> bool:
        """Last email accepted: the enrollment is COMPLETED (terminal states
        must have no next step per campaign_enrollments_terminal_step_check)."""
        _safe_set_role(self.session, "app_worker_general")
        row = self.session.execute(
            text(
                """
                UPDATE campaign_enrollments
                SET state = 'COMPLETED',
                    next_step_id = NULL,
                    next_sequence_position = NULL,
                    first_acceptance_at =
                        COALESCE(first_acceptance_at, :accepted_at),
                    last_acceptance_at = :accepted_at
                WHERE workspace_id = :workspace_id AND id = :enrollment_id
                  AND state = 'ACTIVE' AND next_step_id = :expected_step_id
                RETURNING id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "enrollment_id": str(enrollment_id),
                "expected_step_id": str(expected_step_id),
                "accepted_at": accepted_at,
            },
        ).first()
        return row is not None

    def fail_enrollment(
        self,
        *,
        workspace_id: UUID,
        enrollment_id: UUID,
        expected_step_id: UUID,
    ) -> bool:
        """The current email permanently failed: no next step will ever run."""
        _safe_set_role(self.session, "app_worker_general")
        row = self.session.execute(
            text(
                """
                UPDATE campaign_enrollments
                SET state = 'FAILED', next_step_id = NULL,
                    next_sequence_position = NULL
                WHERE workspace_id = :workspace_id AND id = :enrollment_id
                  AND state = 'ACTIVE' AND next_step_id = :expected_step_id
                RETURNING id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "enrollment_id": str(enrollment_id),
                "expected_step_id": str(expected_step_id),
            },
        ).first()
        return row is not None

    def complete_render_job(
        self, *, workspace_id: UUID, job_id: UUID, processed_count: int
    ) -> None:
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE campaign_planning_jobs
                SET state = 'READY', processed_count = :processed_count,
                    next_due_at = NULL
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "job_id": str(job_id),
                "processed_count": processed_count,
            },
        )

    def reset_failed_planning_jobs(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        activation_id: UUID,
        audience_id: UUID | None,
    ) -> list[str]:
        """FAILED -> PENDING for the ENROLL/RENDER jobs of one activation (or of one
        supplement audience), keeping each job's cursor and counters so work resumes
        where it stopped; chunks are idempotent (ON CONFLICT DO NOTHING, unique message
        key), so replaying the failed chunk is safe. Returns the phases reset; a repeat
        finds nothing FAILED and returns []."""
        _safe_set_role(self.session, "app_worker_general")
        rows = self.session.execute(
            text(
                """
                UPDATE campaign_planning_jobs
                SET state = 'PENDING', error_reason = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    next_due_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND activation_id = :activation_id
                  AND audience_id IS NOT DISTINCT FROM CAST(:audience_id AS uuid)
                  AND phase IN ('ENROLL', 'RENDER') AND state = 'FAILED'
                RETURNING phase
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "activation_id": str(activation_id),
                "audience_id": str(audience_id) if audience_id else None,
            },
        ).all()
        return sorted(row[0] for row in rows)

    def enroll_job_is_ready(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        activation_id: UUID,
        audience_id: UUID | None,
    ) -> bool:
        """RENDER renders every PLANNED message of the campaign, so it must not run
        (and finish) before ENROLL has created them all, or it would strand the rest."""
        _safe_set_role(self.session, "app_worker_general")
        return bool(
            self.session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM campaign_planning_jobs
                        WHERE workspace_id = :workspace_id
                          AND campaign_id = :campaign_id
                          AND activation_id = :activation_id
                          AND audience_id IS NOT DISTINCT FROM
                              CAST(:audience_id AS uuid)
                          AND phase = 'ENROLL' AND state = 'READY'
                    )
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "activation_id": str(activation_id),
                    "audience_id": str(audience_id) if audience_id else None,
                },
            ).scalar()
        )

    def fail_planning_job(
        self, *, workspace_id: UUID, job_id: UUID, error_reason: str
    ) -> None:
        """Fails the job only -- deliberately never touches campaigns.status.
        Auto-classifying a planning failure as campaign-level ERROR is an
        unapproved product decision (CAMPAIGN_ENGINE.md Open Decisions);
        nothing in this codebase sets ERROR today."""
        _safe_set_role(self.session, "app_worker_general")
        self.session.execute(
            text(
                """
                UPDATE campaign_planning_jobs
                SET state = 'FAILED', error_reason = :error_reason, next_due_at = NULL
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "job_id": str(job_id),
                "error_reason": error_reason[:500],
            },
        )

    def _release_capture_sources(
        self, *, workspace_id: UUID, audience_id: UUID
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE audience_capture_sources
                SET gate_held = false, released_at = pg_catalog.transaction_timestamp()
                WHERE workspace_id = :workspace_id AND audience_id = :audience_id
                  AND gate_held = true
                """
            ),
            {"workspace_id": str(workspace_id), "audience_id": str(audience_id)},
        )
