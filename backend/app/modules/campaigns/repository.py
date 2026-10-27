from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from app.core.errors import AppError

# Large enough that no sequence in practice has this many steps, small enough
# to keep as a plain bigint. Used as a two-phase temporary offset so a bulk
# reposition never collides with the (workspace_id, sequence_id, position)
# unique constraint or the campaign_mailboxes-equivalent allocation ordering,
# without ever setting position <= 0 (sequence_steps_position_check forbids
# that even transiently -- CHECK constraints are not deferrable here).
_POSITION_TEMP_OFFSET = 1_000_000


_PAUSE_COLUMNS = ("paused_at", "paused_total_seconds", "last_resumed_at")
# {"present": bool, "checked_at": float}. A positive answer is permanent for the
# process; a negative one is re-checked so applying migration 0040 is noticed
# without a restart.
_pause_columns_cache: dict[str, Any] = {}
_PAUSE_COLUMNS_RECHECK_SECONDS = 60.0


def reset_pause_columns_cache() -> None:
    _pause_columns_cache.clear()


def campaigns_have_pause_columns(session: Session) -> bool:
    """Whether migration 0040 is applied. Pause is the emergency stop, so it must
    keep working on a database that has not been migrated yet: without the columns
    pause/resume still change the status (and resume still bumps the generation);
    they just do not record when. Uses the catalog, which every role can read."""
    cached = _pause_columns_cache.get("present")
    if cached is True:
        return True
    now = time.monotonic()
    if cached is False and now - _pause_columns_cache.get("checked_at", 0.0) < (
        _PAUSE_COLUMNS_RECHECK_SECONDS
    ):
        return False
    present = (
        session.execute(
            text(
                "SELECT count(*) FROM pg_catalog.pg_attribute a "
                "WHERE a.attrelid = pg_catalog.to_regclass('public.campaigns') "
                "AND a.attname = ANY(:names) AND NOT a.attisdropped"
            ),
            {"names": list(_PAUSE_COLUMNS)},
        ).scalar()
        == len(_PAUSE_COLUMNS)
    )
    _pause_columns_cache["present"] = present
    _pause_columns_cache["checked_at"] = now
    return present


# Campaign list orderings: sort name -> (key expression, direction, keyset
# comparison). None is the original id order.
_LIST_ORDERS: dict[str | None, tuple[str | None, str, str]] = {
    None: (None, "ASC", ">"),
    "updated_desc": ("updated_at", "DESC", "<"),
    "created_desc": ("created_at", "DESC", "<"),
    "name_asc": ("lower(name)", "ASC", ">"),
}
_LIST_KEY_PARAMS: dict[str | None, str] = {
    "updated_desc": "CAST(:after_key AS timestamptz)",
    "created_desc": "CAST(:after_key AS timestamptz)",
    "name_asc": ":after_key",
}

class CampaignRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -------------------------------------------------------------------
    # Campaigns
    # -------------------------------------------------------------------

    def create_campaign(
        self,
        *,
        workspace_id: UUID,
        name: str,
        description: str | None,
        creator_id: UUID,
        campaign_type: str = "STANDARD",
    ) -> RowMapping:
        campaign_id = uuid.uuid4()
        row = (
            self.session.execute(
                text(
                    """
                    INSERT INTO campaigns
                        (id, workspace_id, name, description, creator_id, status,
                         campaign_type)
                    VALUES
                        (:id, :workspace_id, :name, :description, :creator_id, 'DRAFT',
                         :campaign_type)
                    RETURNING *
                    """
                ),
                {
                    "id": str(campaign_id),
                    "workspace_id": str(workspace_id),
                    "name": name,
                    "description": description,
                    "creator_id": str(creator_id),
                    "campaign_type": campaign_type,
                },
            )
            .mappings()
            .one()
        )
        self._record_audit_event(
            workspace_id=workspace_id,
            actor_id=creator_id,
            action="campaign.create",
            target_id=campaign_id,
            after_state={
                "name": name,
                "status": "DRAFT",
                "campaign_type": campaign_type,
            },
        )
        return row

    def get_campaign(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        for_update: bool = False,
    ) -> RowMapping | None:
        clause = "FOR UPDATE" if for_update else ""
        row = (
            self.session.execute(
                text(
                    f"""
                    SELECT *
                    FROM campaigns
                    WHERE workspace_id = :workspace_id AND id = :campaign_id
                    {clause}
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .first()
        )
        return row

    def get_workspace_defaults(self, *, workspace_id: UUID) -> dict[str, Any]:
        """The workspace's `defaults` JSON (footer settings are under `compliance`)."""
        row = (
            self.session.execute(
                text("SELECT defaults FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": str(workspace_id)},
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

    def list_campaigns(
        self,
        *,
        workspace_id: UUID,
        limit: int,
        after_id: UUID | None,
        status: str | None,
        query: str | None,
        include_archived: bool = False,
        campaign_type: str | None = None,
        sort: str | None = None,
        after_key: str | None = None,
    ) -> Sequence[RowMapping]:
        # `sort` is one of _LIST_ORDERS' keys (validated by the caller); its SQL
        # fragments are constants, never request text.
        key_sql, direction, comparison = _LIST_ORDERS[sort]
        clauses = ["workspace_id = :workspace_id"]
        params: dict[str, Any] = {"workspace_id": str(workspace_id), "limit": limit}
        if after_id is not None and key_sql is None:
            clauses.append("id > :after_id")
            params["after_id"] = str(after_id)
        elif after_id is not None:
            # Keyset on (sort key, id): stable while rows are added or edited.
            key_param = _LIST_KEY_PARAMS[sort]
            clauses.append(f"({key_sql}, id) {comparison} ({key_param}, :after_id)")
            params["after_id"] = str(after_id)
            params["after_key"] = after_key
        if campaign_type:
            clauses.append("campaign_type = :campaign_type")
            params["campaign_type"] = campaign_type
        if status:
            clauses.append("status = :status")
            params["status"] = status
        elif not include_archived:
            clauses.append("status <> 'ARCHIVED'")
        if query:
            clauses.append("name ILIKE :query_pattern")
            params["query_pattern"] = f"%{query}%"
        where_sql = " AND ".join(clauses)
        order_sql = (
            "id ASC" if key_sql is None else f"{key_sql} {direction}, id {direction}"
        )
        rows = (
            self.session.execute(
                text(
                    f"""
                    SELECT *
                    FROM campaigns
                    WHERE {where_sql}
                    ORDER BY {order_sql}
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
        return rows

    def count_campaigns_by_status(
        self,
        *,
        workspace_id: UUID,
        query: str | None,
        campaign_type: str | None,
    ) -> dict[str, int]:
        clauses = ["workspace_id = :workspace_id"]
        params: dict[str, Any] = {"workspace_id": str(workspace_id)}
        if campaign_type:
            clauses.append("campaign_type = :campaign_type")
            params["campaign_type"] = campaign_type
        if query:
            clauses.append("name ILIKE :query_pattern")
            params["query_pattern"] = f"%{query}%"
        rows = self.session.execute(
            text(
                f"""
                SELECT status, count(*) AS total
                FROM campaigns
                WHERE {" AND ".join(clauses)}
                GROUP BY status
                """
            ),
            params,
        ).all()
        return {row[0]: int(row[1]) for row in rows}

    def member_display_names(self) -> dict[str, str]:
        """Names of the selected workspace's members, from the same directory
        function the Team page uses (so no wider read on profiles)."""
        rows = self.session.execute(
            text(
                "SELECT user_id, display_name FROM public.app_list_workspace_members()"
            )
        ).all()
        return {str(row[0]): row[1] for row in rows if row[1]}

    def update_campaign(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        expected_version: int,
        name: str | None,
        description: str | None,
        description_provided: bool,
        actor_id: UUID,
        may_edit_paused: bool = False,
    ) -> RowMapping | None:
        # ADR-0020: a paused campaign's name/description may change, but the row
        # policy only lets campaigns.execute update an activated campaign, and it
        # would hide the row from FOR UPDATE (a misleading 404) -- so answer
        # "forbidden" before locking.
        peek = self.get_campaign(workspace_id=workspace_id, campaign_id=campaign_id)
        if (
            peek is not None
            and peek["status"] == "PAUSED"
            and peek["activation_id"] is not None
            and not may_edit_paused
        ):
            raise AppError(
                "forbidden",
                "Only a manager can edit a paused campaign",
                status_code=403,
            )
        existing = self.get_campaign(
            workspace_id=workspace_id, campaign_id=campaign_id, for_update=True
        )
        if existing is None:
            return None
        if existing["status"] not in ("DRAFT", "PAUSED"):
            raise AppError(
                "state_conflict",
                "Only DRAFT or PAUSED campaigns can be edited",
                status_code=409,
            )
        if existing["version"] != expected_version:
            raise AppError(
                "conflict",
                "Campaign was modified by another user. Please refresh and try again.",
                status_code=409,
            )

        new_name = name if name is not None else existing["name"]
        new_description = (
            description if description_provided else existing["description"]
        )

        row = (
            self.session.execute(
                text(
                    """
                    UPDATE campaigns
                    SET name = :name, description = :description
                    WHERE workspace_id = :workspace_id AND id = :campaign_id
                      AND version = :expected_version
                    RETURNING *
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "name": new_name,
                    "description": new_description,
                    "expected_version": expected_version,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError(
                "conflict",
                "Campaign was concurrently updated. Please refresh and try again.",
                status_code=409,
            )
        self._record_audit_event(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action="campaign.update",
            target_id=campaign_id,
            before_state={"name": existing["name"], "version": existing["version"]},
            after_state={"name": new_name, "version": row["version"]},
        )
        return row

    def archive_campaign(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        expected_version: int,
        actor_id: UUID,
        may_archive_activated: bool = False,
    ) -> RowMapping | None:
        if not may_archive_activated:
            # Row-level security hides non-DRAFT campaigns from FOR UPDATE for a
            # role without campaigns.execute, which would surface as a 404.
            # A plain read is allowed, so answer with the real reason instead.
            peek = self.get_campaign(
                workspace_id=workspace_id, campaign_id=campaign_id
            )
            if peek is not None and peek["status"] not in ("DRAFT", "ARCHIVED"):
                raise AppError(
                    "forbidden",
                    "You do not have permission to archive an activated campaign",
                    status_code=403,
                )
        existing = self.get_campaign(
            workspace_id=workspace_id, campaign_id=campaign_id, for_update=True
        )
        if existing is None:
            return None
        if existing["status"] == "ARCHIVED":
            return existing
        if existing["status"] == "RUNNING":
            # The database guard forbids RUNNING -> ARCHIVED too; this is the
            # readable version of the same rule (CAMPAIGN_STATE_MACHINE.md).
            raise AppError(
                "state_conflict",
                "Pause the campaign before archiving it",
                status_code=409,
            )
        if existing["version"] != expected_version:
            raise AppError(
                "conflict",
                "Campaign was modified by another user. Please refresh and try again.",
                status_code=409,
            )
        row = (
            self.session.execute(
                text(
                    """
                    UPDATE campaigns
                    SET status = 'ARCHIVED',
                        archived_at = pg_catalog.transaction_timestamp()
                    WHERE workspace_id = :workspace_id AND id = :campaign_id
                      AND version = :expected_version
                    RETURNING *
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "expected_version": expected_version,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError(
                "conflict",
                "Campaign was concurrently updated. Please refresh and try again.",
                status_code=409,
            )
        self._record_audit_event(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action="campaign.archive",
            target_id=campaign_id,
            before_state={"status": existing["status"]},
            after_state={"status": "ARCHIVED"},
        )
        return row

    def set_draft_sequence_id(
        self, *, workspace_id: UUID, campaign_id: UUID, sequence_id: UUID
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE campaigns
                SET draft_sequence_id = :sequence_id
                WHERE workspace_id = :workspace_id AND id = :campaign_id
                  AND draft_sequence_id IS NULL
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "sequence_id": str(sequence_id),
            },
        )

    def set_current_settings_id(
        self, *, workspace_id: UUID, campaign_id: UUID, settings_id: UUID
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE campaigns
                SET current_settings_id = :settings_id
                WHERE workspace_id = :workspace_id AND id = :campaign_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "settings_id": str(settings_id),
            },
        )

    def commit_draft_audience(
        self, *, workspace_id: UUID, campaign_id: UUID, audience_id: UUID
    ) -> RowMapping | None:
        row = (
            self.session.execute(
                text(
                    """
                    UPDATE campaigns
                    SET draft_audience_id = :audience_id
                    WHERE workspace_id = :workspace_id AND id = :campaign_id
                      AND status = 'DRAFT'
                      AND EXISTS (
                          SELECT 1 FROM campaign_audiences a
                          WHERE a.workspace_id = :workspace_id
                            AND a.campaign_id = :campaign_id
                            AND a.id = :audience_id
                            AND a.status = 'READY'
                      )
                    RETURNING *
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "audience_id": str(audience_id),
                },
            )
            .mappings()
            .first()
        )
        return row

    # -------------------------------------------------------------------
    # Sequence + steps
    # -------------------------------------------------------------------

    def get_sequence(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> RowMapping | None:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_sequences
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                    ORDER BY revision DESC
                    LIMIT 1
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .first()
        )
        return row

    def create_sequence(self, *, workspace_id: UUID, campaign_id: UUID) -> RowMapping:
        sequence_id = uuid.uuid4()
        row = (
            self.session.execute(
                text(
                    """
                    INSERT INTO campaign_sequences
                        (id, workspace_id, campaign_id, revision, status)
                    VALUES
                        (:id, :workspace_id, :campaign_id, 1, 'DRAFT')
                    RETURNING *
                    """
                ),
                {
                    "id": str(sequence_id),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                },
            )
            .mappings()
            .one()
        )
        self.set_draft_sequence_id(
            workspace_id=workspace_id, campaign_id=campaign_id, sequence_id=sequence_id
        )
        return row

    def set_sequence_personalization_config(
        self,
        *,
        workspace_id: UUID,
        sequence_id: UUID,
        config: dict[str, Any],
        expected_version: int | None = None,
    ) -> RowMapping | None:
        """Store the campaign objective on the draft sequence (ADR-0011). The
        sequence guard trigger rejects this once the campaign is activated or the
        sequence frozen. None when the version guard or DRAFT status did not
        match."""
        return (
            self.session.execute(
                text(
                    """
                    UPDATE campaign_sequences
                    SET personalization_config = CAST(:config AS jsonb)
                    WHERE workspace_id = :workspace_id AND id = :sequence_id
                      AND status = 'DRAFT'
                      AND (CAST(:expected_version AS bigint) IS NULL
                           OR version = CAST(:expected_version AS bigint))
                    RETURNING *
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "sequence_id": str(sequence_id),
                    "config": json.dumps(config),
                    "expected_version": expected_version,
                },
            )
            .mappings()
            .first()
        )

    def record_audit_event(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        target_id: UUID,
        after_state: dict[str, Any] | None = None,
    ) -> None:
        self._record_audit_event(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=action,
            target_id=target_id,
            after_state=after_state,
        )

    def list_steps_ordered(
        self, *, workspace_id: UUID, sequence_id: UUID
    ) -> list[RowMapping]:
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM sequence_steps
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

    def get_sequence_frontier(self, *, workspace_id: UUID, sequence_id: UUID) -> int:
        """Highest step position any message or enrollment pointer already
        references (0 when none). Mirrors the definition in
        app_guard_frozen_sequence_steps (migration 0042), which is authoritative."""
        value = self.session.execute(
            text(
                """
                SELECT COALESCE(MAX(ss.position), 0)
                FROM sequence_steps ss
                WHERE ss.workspace_id = :workspace_id AND ss.sequence_id = :sequence_id
                  AND (
                      EXISTS (SELECT 1 FROM messages m
                              WHERE m.workspace_id = ss.workspace_id
                                AND m.step_id = ss.id)
                      OR EXISTS (SELECT 1 FROM campaign_enrollments e
                                 WHERE e.workspace_id = ss.workspace_id
                                   AND e.next_step_id = ss.id)
                  )
                """
            ),
            {"workspace_id": str(workspace_id), "sequence_id": str(sequence_id)},
        ).scalar()
        return int(value or 0)

    def count_step_messages(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> dict[str, tuple[int, int, int, int]]:
        """step_id -> (sent, unsent, written, unwritten) campaign messages, for the
        paused editor. "Written" = the email text exists (rendered_at set); for a
        personalized campaign an unwritten message will be written from the step's
        current reference, a written one never changes."""
        rows = self.session.execute(
            text(
                """
                SELECT step_id,
                       count(*) FILTER (WHERE status = 'SENT') AS sent,
                       count(*) FILTER (
                           WHERE status IN ('PLANNED', 'SCHEDULED', 'RETRY_SCHEDULED')
                       ) AS unsent,
                       count(*) FILTER (WHERE rendered_at IS NOT NULL) AS written,
                       count(*) FILTER (
                           WHERE rendered_at IS NULL AND status = 'PLANNED'
                       ) AS unwritten
                FROM messages
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND purpose = 'CAMPAIGN'
                GROUP BY step_id
                """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        ).all()
        return {
            str(r[0]): (int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows
        }

    def get_step(
        self, *, workspace_id: UUID, step_id: UUID, for_update: bool = False
    ) -> RowMapping | None:
        clause = "FOR UPDATE" if for_update else ""
        row = (
            self.session.execute(
                text(
                    f"""
                    SELECT * FROM sequence_steps
                    WHERE workspace_id = :workspace_id AND id = :step_id
                    {clause}
                    """
                ),
                {"workspace_id": str(workspace_id), "step_id": str(step_id)},
            )
            .mappings()
            .first()
        )
        return row

    def _apply_full_reorder(
        self,
        *,
        workspace_id: UUID,
        sequence_id: UUID,
        ordering: list[tuple[UUID, int]],
    ) -> None:
        """Two-phase reposition: temp-shift all touched rows, then apply final
        positions individually. Avoids colliding with the (workspace_id,
        sequence_id, position) unique constraint at any intermediate step,
        without ever needing position <= 0 (which the CHECK constraint
        forbids even transiently)."""
        if not ordering:
            return
        # Rows that keep their position are left alone: touching them would bump
        # their version for nothing and, on a paused campaign, trip the frontier
        # guard on steps that leads have already reached.
        current = {
            str(row["id"]): row["position"]
            for row in self.session.execute(
                text(
                    """
                    SELECT id, position FROM sequence_steps
                    WHERE workspace_id = :workspace_id AND sequence_id = :sequence_id
                      AND id = ANY(:ids)
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "sequence_id": str(sequence_id),
                    "ids": [str(step_id) for step_id, _ in ordering],
                },
            )
            .mappings()
            .all()
        }
        ordering = [
            (step_id, position)
            for step_id, position in ordering
            if current.get(str(step_id)) != position
        ]
        if not ordering:
            return
        ids = [str(step_id) for step_id, _ in ordering]
        self.session.execute(
            text(
                """
                UPDATE sequence_steps
                SET position = position + :offset
                WHERE workspace_id = :workspace_id AND sequence_id = :sequence_id
                  AND id = ANY(:ids)
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "sequence_id": str(sequence_id),
                "ids": ids,
                "offset": _POSITION_TEMP_OFFSET,
            },
        )
        for step_id, new_position in ordering:
            self.session.execute(
                text(
                    """
                    UPDATE sequence_steps
                    SET position = :position
                    WHERE workspace_id = :workspace_id AND id = :step_id
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "step_id": str(step_id),
                    "position": new_position,
                },
            )

    def insert_step(
        self,
        *,
        workspace_id: UUID,
        sequence_id: UUID,
        campaign_id: UUID,
        position: int,
        kind: str,
        email_subject: str | None,
        email_body_html: str | None,
        email_variable_schema: dict[str, Any] | None,
        wait_duration_minutes: int | None,
        source_template_version_id: UUID | None,
        email_preheader: str | None = None,
        email_same_thread: bool = False,
    ) -> RowMapping:
        existing = self.list_steps_ordered(
            workspace_id=workspace_id, sequence_id=sequence_id
        )
        target_position = max(1, min(position, len(existing) + 1))

        shift_ordering = [
            (UUID(str(row["id"])), row["position"] + 1)
            for row in existing
            if row["position"] >= target_position
        ]
        self._apply_full_reorder(
            workspace_id=workspace_id, sequence_id=sequence_id, ordering=shift_ordering
        )

        step_id = uuid.uuid4()
        row = (
            self.session.execute(
                text(
                    """
                    INSERT INTO sequence_steps
                        (id, workspace_id, sequence_id, campaign_id, position, kind,
                         email_subject, email_body_html, email_preheader,
                         email_variable_schema,
                         wait_duration_minutes, source_template_version_id,
                         email_same_thread)
                    VALUES
                        (:id, :workspace_id, :sequence_id, :campaign_id, :position,
                         :kind,
                         :email_subject, :email_body_html, :email_preheader,
                         CAST(:email_variable_schema AS jsonb),
                         :wait_duration_minutes, :source_template_version_id,
                         :email_same_thread)
                    RETURNING *
                    """
                ),
                {
                    "id": str(step_id),
                    "workspace_id": str(workspace_id),
                    "sequence_id": str(sequence_id),
                    "campaign_id": str(campaign_id),
                    "position": target_position,
                    "kind": kind,
                    "email_subject": email_subject,
                    "email_body_html": email_body_html,
                    "email_preheader": email_preheader,
                    "email_same_thread": email_same_thread,
                    "email_variable_schema": (
                        json.dumps(email_variable_schema)
                        if email_variable_schema is not None
                        else None
                    ),
                    "wait_duration_minutes": wait_duration_minutes,
                    "source_template_version_id": (
                        str(source_template_version_id)
                        if source_template_version_id
                        else None
                    ),
                },
            )
            .mappings()
            .one()
        )
        return row

    def update_step(
        self,
        *,
        workspace_id: UUID,
        step_id: UUID,
        expected_version: int,
        email_subject: str | None,
        email_body_html: str | None,
        email_variable_schema: dict[str, Any] | None,
        email_variable_schema_provided: bool,
        wait_duration_minutes: int | None,
        source_template_version_id: UUID | None,
        source_template_version_id_provided: bool,
        email_preheader: str | None = None,
        email_preheader_provided: bool = False,
        email_same_thread: bool | None = None,
    ) -> RowMapping | None:
        existing = self.get_step(
            workspace_id=workspace_id, step_id=step_id, for_update=True
        )
        if existing is None:
            return None
        if existing["version"] != expected_version:
            raise AppError(
                "conflict",
                "Step was modified by another user. Please refresh and try again.",
                status_code=409,
            )

        if existing["kind"] == "EMAIL":
            new_subject = (
                email_subject
                if email_subject is not None
                else existing["email_subject"]
            )
            new_body = (
                email_body_html
                if email_body_html is not None
                else existing["email_body_html"]
            )
            new_schema = (
                email_variable_schema
                if email_variable_schema_provided
                else existing["email_variable_schema"]
            )
            new_template_ref = (
                source_template_version_id
                if source_template_version_id_provided
                else existing["source_template_version_id"]
            )
            new_preheader = (
                email_preheader
                if email_preheader_provided
                else existing["email_preheader"]
            )
            new_same_thread = (
                email_same_thread
                if email_same_thread is not None
                else existing["email_same_thread"]
            )
            row = (
                self.session.execute(
                    text(
                        """
                        UPDATE sequence_steps
                        SET email_subject = :email_subject,
                            email_body_html = :email_body_html,
                            email_preheader = :email_preheader,
                            email_same_thread = :email_same_thread,
                            email_variable_schema =
                                CAST(:email_variable_schema AS jsonb),
                            source_template_version_id = :source_template_version_id
                        WHERE workspace_id = :workspace_id AND id = :step_id
                          AND version = :expected_version
                        RETURNING *
                        """
                    ),
                    {
                        "workspace_id": str(workspace_id),
                        "step_id": str(step_id),
                        "expected_version": expected_version,
                        "email_subject": new_subject,
                        "email_body_html": new_body,
                        "email_preheader": new_preheader,
                        "email_same_thread": new_same_thread,
                        "email_variable_schema": (
                            json.dumps(new_schema) if new_schema is not None else None
                        ),
                        "source_template_version_id": (
                            str(new_template_ref) if new_template_ref else None
                        ),
                    },
                )
                .mappings()
                .first()
            )
        else:
            new_wait = (
                wait_duration_minutes
                if wait_duration_minutes is not None
                else existing["wait_duration_minutes"]
            )
            row = (
                self.session.execute(
                    text(
                        """
                        UPDATE sequence_steps
                        SET wait_duration_minutes = :wait_duration_minutes
                        WHERE workspace_id = :workspace_id AND id = :step_id
                          AND version = :expected_version
                        RETURNING *
                        """
                    ),
                    {
                        "workspace_id": str(workspace_id),
                        "step_id": str(step_id),
                        "expected_version": expected_version,
                        "wait_duration_minutes": new_wait,
                    },
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AppError(
                "conflict",
                "Step was concurrently updated. Please refresh and try again.",
                status_code=409,
            )
        return row

    def delete_step(
        self, *, workspace_id: UUID, sequence_id: UUID, step_id: UUID
    ) -> bool:
        existing = self.list_steps_ordered(
            workspace_id=workspace_id, sequence_id=sequence_id
        )
        if not any(str(row["id"]) == str(step_id) for row in existing):
            return False
        self.session.execute(
            text(
                """
                DELETE FROM sequence_steps
                WHERE workspace_id = :workspace_id AND id = :step_id
                """
            ),
            {"workspace_id": str(workspace_id), "step_id": str(step_id)},
        )
        remaining = [row for row in existing if str(row["id"]) != str(step_id)]
        ordering = [
            (UUID(str(row["id"])), index + 1) for index, row in enumerate(remaining)
        ]
        self._apply_full_reorder(
            workspace_id=workspace_id, sequence_id=sequence_id, ordering=ordering
        )
        return True

    def reorder_steps(
        self,
        *,
        workspace_id: UUID,
        sequence_id: UUID,
        ordering: list[tuple[UUID, int]],
    ) -> list[RowMapping]:
        self._apply_full_reorder(
            workspace_id=workspace_id, sequence_id=sequence_id, ordering=ordering
        )
        return self.list_steps_ordered(
            workspace_id=workspace_id, sequence_id=sequence_id
        )

    # -------------------------------------------------------------------
    # Settings versions
    # -------------------------------------------------------------------

    def create_settings_version(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        timezone: str,
        weekday_set: int,
        window_start_local: Any,
        window_end_local: Any,
        daily_limit: int | None,
    ) -> RowMapping:
        next_revision = self.session.execute(
            text(
                """
                    SELECT COALESCE(MAX(revision), 0) + 1
                    FROM campaign_settings_versions
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                    """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        ).scalar_one()
        settings_id = uuid.uuid4()
        row = (
            self.session.execute(
                text(
                    """
                    INSERT INTO campaign_settings_versions
                        (id, workspace_id, campaign_id, revision, timezone, weekday_set,
                         window_start_local, window_end_local, daily_limit)
                    VALUES
                        (:id, :workspace_id, :campaign_id, :revision, :timezone,
                         :weekday_set, :window_start_local, :window_end_local,
                         :daily_limit)
                    RETURNING *
                    """
                ),
                {
                    "id": str(settings_id),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "revision": next_revision,
                    "timezone": timezone,
                    "weekday_set": weekday_set,
                    "window_start_local": window_start_local,
                    "window_end_local": window_end_local,
                    "daily_limit": daily_limit,
                },
            )
            .mappings()
            .one()
        )
        self.set_current_settings_id(
            workspace_id=workspace_id, campaign_id=campaign_id, settings_id=settings_id
        )
        return row

    def get_settings_version(
        self, *, workspace_id: UUID, settings_id: UUID
    ) -> RowMapping | None:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_settings_versions
                    WHERE workspace_id = :workspace_id AND id = :settings_id
                    """
                ),
                {"workspace_id": str(workspace_id), "settings_id": str(settings_id)},
            )
            .mappings()
            .first()
        )
        return row

    def list_settings_versions(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> Sequence[RowMapping]:
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_settings_versions
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                    ORDER BY revision DESC
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .all()
        )
        return rows

    # -------------------------------------------------------------------
    # Mailbox assignment
    # -------------------------------------------------------------------

    def list_campaign_mailboxes(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> Sequence[RowMapping]:
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT cm.mailbox_id, cm.active, cm.allocation_position,
                           m.provider, m.original_address, m.sender_display_name,
                           m.connection_state, m.health_state, m.policy_state,
                           m.policy_reason, m.circuit_state, m.blocked_until,
                           m.pending_safety_count
                    FROM campaign_mailboxes cm
                    JOIN mailboxes m
                      ON m.workspace_id = cm.workspace_id AND m.id = cm.mailbox_id
                    WHERE cm.workspace_id = :workspace_id
                      AND cm.campaign_id = :campaign_id
                    ORDER BY cm.allocation_position ASC
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .all()
        )
        return rows

    def insert_campaign_mailbox(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        mailbox_id: UUID,
        allocation_position: int,
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO campaign_mailboxes
                    (workspace_id, campaign_id, mailbox_id, active, allocation_position)
                VALUES
                    (:workspace_id, :campaign_id, :mailbox_id, true,
                     :allocation_position)
                ON CONFLICT (workspace_id, campaign_id, mailbox_id) DO NOTHING
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "mailbox_id": str(mailbox_id),
                "allocation_position": allocation_position,
            },
        )

    def delete_campaign_mailbox(
        self, *, workspace_id: UUID, campaign_id: UUID, mailbox_id: UUID
    ) -> None:
        self.session.execute(
            text(
                """
                DELETE FROM campaign_mailboxes
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND mailbox_id = :mailbox_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "mailbox_id": str(mailbox_id),
            },
        )

    def delete_all_campaign_mailboxes(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> None:
        self.session.execute(
            text(
                """
                DELETE FROM campaign_mailboxes
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        )

    # -------------------------------------------------------------------
    # Audience (app_api-permitted parts only -- capture itself is owned by
    # CampaignWorkerRepository under the app_worker_general role)
    # -------------------------------------------------------------------

    def get_existing_list_ids(
        self, *, workspace_id: UUID, list_ids: Sequence[UUID]
    ) -> dict[UUID, int]:
        if not list_ids:
            return {}
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT id, membership_revision FROM lead_lists
                    WHERE workspace_id = :workspace_id AND id = ANY(:ids)
                      AND archived_at IS NULL
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "ids": [str(i) for i in list_ids],
                },
            )
            .mappings()
            .all()
        )
        return {
            UUID(str(row["id"])): int(row["membership_revision"]) for row in rows
        }

    def get_existing_lead_ids(
        self, *, workspace_id: UUID, lead_ids: Sequence[UUID]
    ) -> set[UUID]:
        if not lead_ids:
            return set()
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT id FROM leads
                    WHERE workspace_id = :workspace_id AND id = ANY(:ids)
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "ids": [str(i) for i in lead_ids],
                },
            )
            .mappings()
            .all()
        )
        return {UUID(str(row["id"])) for row in rows}

    def count_candidate_leads(
        self,
        *,
        workspace_id: UUID,
        list_ids: Sequence[UUID],
        lead_ids: Sequence[UUID],
    ) -> int:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT COUNT(DISTINCT lead_id) AS candidate_count
                    FROM (
                        SELECT id AS lead_id FROM leads
                        WHERE workspace_id = :workspace_id AND id = ANY(:lead_ids)
                        UNION
                        SELECT llm.lead_id FROM lead_list_memberships llm
                        WHERE llm.workspace_id = :workspace_id
                          AND llm.list_id = ANY(:list_ids)
                    ) candidates
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "lead_ids": [str(i) for i in lead_ids] or [],
                    "list_ids": [str(i) for i in list_ids] or [],
                },
            )
            .mappings()
            .one()
        )
        return int(row["candidate_count"])

    def get_audience(
        self, *, workspace_id: UUID, campaign_id: UUID, audience_id: UUID
    ) -> RowMapping | None:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_audiences
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
            .mappings()
            .first()
        )
        return row

    def get_latest_audience(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> RowMapping | None:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_audiences
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                    ORDER BY revision DESC
                    LIMIT 1
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .first()
        )
        return row

    def insert_campaign_audience(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        selection_manifest: dict[str, Any],
        kind: str = "ACTIVATION",
    ) -> RowMapping:
        next_revision = self.session.execute(
            text(
                """
                    SELECT COALESCE(MAX(revision), 0) + 1
                    FROM campaign_audiences
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                    """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        ).scalar_one()
        audience_id = uuid.uuid4()
        # The `kind` column exists from migration 0043. Only a SUPPLEMENT names it,
        # so choosing an audience for an ordinary draft keeps working on a database
        # that has not been migrated yet (the default is ACTIVATION).
        kind_column = ", kind" if kind != "ACTIVATION" else ""
        kind_value = ", :kind" if kind != "ACTIVATION" else ""
        row = (
            self.session.execute(
                text(
                    f"""
                    INSERT INTO campaign_audiences
                        (id, workspace_id, campaign_id, revision, status,
                         selection_manifest{kind_column})
                    VALUES
                        (:id, :workspace_id, :campaign_id, :revision, 'CAPTURING',
                         CAST(:selection_manifest AS jsonb){kind_value})
                    RETURNING *
                    """  # noqa: S608 -- both fragments are fixed constants above
                ),
                {
                    "id": str(audience_id),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "revision": next_revision,
                    "selection_manifest": json.dumps(selection_manifest),
                    "kind": kind,
                },
            )
            .mappings()
            .one()
        )
        return row

    def insert_audience_capture_source(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        audience_id: UUID,
        list_id: UUID,
        captured_list_revision: int,
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO audience_capture_sources
                    (workspace_id, campaign_id, audience_id, list_id,
                     captured_list_revision, gate_held)
                VALUES
                    (:workspace_id, :campaign_id, :audience_id, :list_id,
                     :captured_list_revision, true)
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "audience_id": str(audience_id),
                "list_id": str(list_id),
                "captured_list_revision": captured_list_revision,
            },
        )

    def insert_capture_planning_job(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        audience_id: UUID,
        total_count: int,
    ) -> RowMapping:
        job_id = uuid.uuid4()
        row = (
            self.session.execute(
                text(
                    """
                    INSERT INTO campaign_planning_jobs
                        (id, workspace_id, campaign_id, audience_id, phase, state,
                         total_count, next_due_at)
                    VALUES
                        (:id, :workspace_id, :campaign_id, :audience_id, 'CAPTURE',
                         'PENDING', :total_count, pg_catalog.transaction_timestamp())
                    RETURNING *
                    """
                ),
                {
                    "id": str(job_id),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "audience_id": str(audience_id),
                    "total_count": total_count,
                },
            )
            .mappings()
            .one()
        )
        return row

    def get_capture_job_for_audience(
        self, *, workspace_id: UUID, audience_id: UUID
    ) -> RowMapping | None:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_planning_jobs
                    WHERE workspace_id = :workspace_id AND audience_id = :audience_id
                      AND phase = 'CAPTURE'
                    """
                ),
                {"workspace_id": str(workspace_id), "audience_id": str(audience_id)},
            )
            .mappings()
            .first()
        )
        return row

    def get_audience_member_counts(
        self, *, workspace_id: UUID, audience_id: UUID
    ) -> dict[str, int]:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE eligibility_status = 'ACCEPTED')
                            AS accepted,
                        COUNT(*) FILTER (WHERE eligibility_status = 'EXCLUDED')
                            AS excluded
                    FROM campaign_audience_members
                    WHERE workspace_id = :workspace_id AND audience_id = :audience_id
                    """
                ),
                {"workspace_id": str(workspace_id), "audience_id": str(audience_id)},
            )
            .mappings()
            .one()
        )
        return {"accepted": int(row["accepted"]), "excluded": int(row["excluded"])}

    def get_audience_exclusion_reason_counts(
        self, *, workspace_id: UUID, audience_id: UUID
    ) -> dict[str, int]:
        """Counts EXCLUDED members grouped by why, for the UI's "why were N
        leads excluded" breakdown. Only reasons that actually get a row here
        (archived_lead, suppressed) are meaningful; a lead with no resolvable
        address is filtered out before insert (see workers/campaigns.py) and
        is never counted here -- the caller derives that bucket as a residual
        against total_candidates instead."""
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT exclusion_reason, COUNT(*) AS n
                    FROM campaign_audience_members
                    WHERE workspace_id = :workspace_id AND audience_id = :audience_id
                      AND eligibility_status = 'EXCLUDED'
                    GROUP BY exclusion_reason
                    """
                ),
                {"workspace_id": str(workspace_id), "audience_id": str(audience_id)},
            )
            .mappings()
            .all()
        )
        return {str(row["exclusion_reason"]): int(row["n"]) for row in rows}

    # -------------------------------------------------------------------
    # Step attachments (campaign_step_attachments, migration 0025)
    # -------------------------------------------------------------------

    def list_step_attachments(
        self, *, workspace_id: UUID, step_id: UUID
    ) -> list[RowMapping]:
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_step_attachments
                    WHERE workspace_id = :workspace_id AND step_id = :step_id
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"workspace_id": str(workspace_id), "step_id": str(step_id)},
            )
            .mappings()
            .all()
        )
        return list(rows)

    def list_sequence_attachments(
        self, *, workspace_id: UUID, sequence_id: UUID
    ) -> list[RowMapping]:
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_step_attachments
                    WHERE workspace_id = :workspace_id AND sequence_id = :sequence_id
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"workspace_id": str(workspace_id), "sequence_id": str(sequence_id)},
            )
            .mappings()
            .all()
        )
        return list(rows)

    def get_attachment(
        self, *, workspace_id: UUID, step_id: UUID, attachment_id: UUID
    ) -> RowMapping | None:
        return (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_step_attachments
                    WHERE workspace_id = :workspace_id AND step_id = :step_id
                      AND id = :attachment_id
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "step_id": str(step_id),
                    "attachment_id": str(attachment_id),
                },
            )
            .mappings()
            .first()
        )

    def find_attachment_by_content(
        self, *, workspace_id: UUID, step_id: UUID, sha256: str, disposition: str
    ) -> RowMapping | None:
        return (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_step_attachments
                    WHERE workspace_id = :workspace_id AND step_id = :step_id
                      AND sha256 = :sha256 AND disposition = :disposition
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "step_id": str(step_id),
                    "sha256": sha256,
                    "disposition": disposition,
                },
            )
            .mappings()
            .first()
        )

    def insert_attachment(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        sequence_id: UUID,
        step_id: UUID,
        disposition: str,
        content_id: str,
        storage_key: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        created_by: UUID | None,
    ) -> RowMapping | None:
        """None when the same file (sha256 + disposition) is already attached to
        this step -- the unique key makes a repeated/raced upload a no-op."""
        return (
            self.session.execute(
                text(
                    """
                    INSERT INTO campaign_step_attachments
                        (id, workspace_id, campaign_id, sequence_id, step_id,
                         disposition, content_id, storage_key, filename,
                         content_type, size_bytes, sha256, created_by)
                    VALUES
                        (:id, :workspace_id, :campaign_id, :sequence_id, :step_id,
                         :disposition, :content_id, :storage_key, :filename,
                         :content_type, :size_bytes, :sha256, :created_by)
                    ON CONFLICT DO NOTHING
                    RETURNING *
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "sequence_id": str(sequence_id),
                    "step_id": str(step_id),
                    "disposition": disposition,
                    "content_id": content_id,
                    "storage_key": storage_key,
                    "filename": filename,
                    "content_type": content_type,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "created_by": str(created_by) if created_by else None,
                },
            )
            .mappings()
            .first()
        )

    def delete_attachment(self, *, workspace_id: UUID, attachment_id: UUID) -> bool:
        row = self.session.execute(
            text(
                """
                DELETE FROM campaign_step_attachments
                WHERE workspace_id = :workspace_id AND id = :attachment_id
                RETURNING id
                """
            ),
            {"workspace_id": str(workspace_id), "attachment_id": str(attachment_id)},
        ).first()
        return row is not None

    def count_storage_key_references(
        self, *, workspace_id: UUID, storage_key: str
    ) -> int:
        return int(
            self.session.execute(
                text(
                    """
                    SELECT count(*) FROM campaign_step_attachments
                    WHERE workspace_id = :workspace_id AND storage_key = :storage_key
                    """
                ),
                {"workspace_id": str(workspace_id), "storage_key": storage_key},
            ).scalar_one()
        )

    def copy_step_attachments(
        self,
        *,
        workspace_id: UUID,
        from_step_id: UUID,
        to_campaign_id: UUID,
        to_sequence_id: UUID,
        to_step_id: UUID,
    ) -> None:
        """Copy attachment rows to another step. The rows share the storage
        object and keep their content ids, so the copied body's cid references
        stay valid; deleting one copy leaves the object while others use it."""
        self.session.execute(
            text(
                """
                INSERT INTO campaign_step_attachments
                    (id, workspace_id, campaign_id, sequence_id, step_id,
                     disposition, content_id, storage_key, filename, content_type,
                     size_bytes, sha256, created_by)
                SELECT pg_catalog.gen_random_uuid(), workspace_id,
                       :to_campaign_id, :to_sequence_id, :to_step_id,
                       disposition, content_id, storage_key, filename,
                       content_type, size_bytes, sha256, created_by
                FROM campaign_step_attachments
                WHERE workspace_id = :workspace_id AND step_id = :from_step_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "from_step_id": str(from_step_id),
                "to_campaign_id": str(to_campaign_id),
                "to_sequence_id": str(to_sequence_id),
                "to_step_id": str(to_step_id),
            },
        )

    def list_accepted_audience_members(
        self,
        *,
        workspace_id: UUID,
        audience_id: UUID,
        after_ordinal: int | None,
        limit: int,
    ) -> list[RowMapping]:
        """ACCEPTED members in capture order, keyset-paginated on the unique
        (workspace, audience, capture_ordinal) key. Read-only; used by the
        sequence preview."""
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT id, lead_id, capture_ordinal, frozen_variables
                    FROM campaign_audience_members
                    WHERE workspace_id = :workspace_id
                      AND audience_id = :audience_id
                      AND eligibility_status = 'ACCEPTED'
                      AND (CAST(:after_ordinal AS bigint) IS NULL
                           OR capture_ordinal > CAST(:after_ordinal AS bigint))
                    ORDER BY capture_ordinal ASC
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

    def get_accepted_audience_member(
        self, *, workspace_id: UUID, campaign_id: UUID, member_id: UUID
    ) -> RowMapping | None:
        """One accepted member of THIS campaign (workspace + campaign scoped, so
        another tenant's or campaign's member id resolves to None)."""
        row = (
            self.session.execute(
                text(
                    """
                    SELECT id, lead_id, frozen_variables
                    FROM campaign_audience_members
                    WHERE workspace_id = :workspace_id
                      AND campaign_id = :campaign_id
                      AND id = :member_id
                      AND eligibility_status = 'ACCEPTED'
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "member_id": str(member_id),
                },
            )
            .mappings()
            .first()
        )
        return row

    # -------------------------------------------------------------------
    # Activation (app_api-permitted parts only -- ENROLL/RENDER planning
    # itself is owned by CampaignWorkerRepository under app_worker_general)
    # -------------------------------------------------------------------

    def freeze_sequence(
        self, *, workspace_id: UUID, sequence_id: UUID, content_digest: str
    ) -> RowMapping | None:
        row = (
            self.session.execute(
                text(
                    """
                    UPDATE campaign_sequences
                    SET status = 'FROZEN',
                        frozen_at = pg_catalog.transaction_timestamp(),
                        content_digest = :content_digest
                    WHERE workspace_id = :workspace_id AND id = :sequence_id
                      AND status = 'DRAFT'
                    RETURNING *
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "sequence_id": str(sequence_id),
                    "content_digest": content_digest,
                },
            )
            .mappings()
            .first()
        )
        return row

    def activate_campaign(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        expected_version: int,
        activation_id: UUID,
        activated_sequence_id: UUID,
        activated_audience_id: UUID,
        start_at: Any,
        target_status: str,
        actor_id: UUID,
    ) -> RowMapping | None:
        """Caller must already hold a FOR UPDATE lock on this campaign and
        have validated status == 'DRAFT' -- this only guards on version,
        exactly like update_campaign/archive_campaign."""
        row = (
            self.session.execute(
                text(
                    """
                    UPDATE campaigns
                    SET activation_id = :activation_id,
                        activated_sequence_id = :activated_sequence_id,
                        activated_audience_id = :activated_audience_id,
                        start_at = :start_at,
                        status = :target_status,
                        planning_status = 'PENDING'
                    WHERE workspace_id = :workspace_id AND id = :campaign_id
                      AND version = :expected_version
                    RETURNING *
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "expected_version": expected_version,
                    "activation_id": str(activation_id),
                    "activated_sequence_id": str(activated_sequence_id),
                    "activated_audience_id": str(activated_audience_id),
                    "start_at": start_at,
                    "target_status": target_status,
                },
            )
            .mappings()
            .first()
        )
        if row is not None:
            self._record_audit_event(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="campaign.activate",
                target_id=campaign_id,
                after_state={
                    "activation_id": str(activation_id),
                    "status": target_status,
                },
            )
        return row

    def pause_campaign(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        expected_version: int,
        actor_id: UUID,
        previous_status: str,
    ) -> RowMapping | None:
        """Caller must already hold a FOR UPDATE lock and have validated that
        the campaign is RUNNING/SCHEDULED. The status guard in the WHERE clause
        is belt-and-braces: a no-row result means the state moved underneath."""
        paused_at_sql = (
            ", paused_at = COALESCE(paused_at, pg_catalog.transaction_timestamp())"
            if campaigns_have_pause_columns(self.session)
            else ""
        )
        row = (
            self.session.execute(
                text(
                    f"""
                    UPDATE campaigns
                    SET status = 'PAUSED'{paused_at_sql}
                    WHERE workspace_id = :workspace_id AND id = :campaign_id
                      AND version = :expected_version
                      AND status IN ('RUNNING', 'SCHEDULED')
                    RETURNING *
                    """  # noqa: S608 -- paused_at_sql is a fixed constant above
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "expected_version": expected_version,
                },
            )
            .mappings()
            .first()
        )
        if row is not None:
            self._record_audit_event(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="campaign.pause",
                target_id=campaign_id,
                before_state={"status": previous_status},
                after_state={"status": "PAUSED"},
            )
        return row

    def resume_campaign(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        expected_version: int,
        target_status: str,
        actor_id: UUID,
    ) -> RowMapping | None:
        """Flip PAUSED -> RUNNING/SCHEDULED and bump schedule_generation in ONE
        statement (ADR-0020). The bump is the resume fence: the scheduler only
        claims messages whose schedule_generation equals the campaign's, so no
        pre-existing unsent message can be claimed until the reconcile pass has
        re-validated and re-stamped it. Caller holds the FOR UPDATE lock."""
        pause_metadata_sql = (
            """,
                        paused_total_seconds = paused_total_seconds + COALESCE(
                            GREATEST(
                                pg_catalog.floor(pg_catalog.date_part(
                                    'epoch',
                                    pg_catalog.transaction_timestamp() - paused_at
                                )),
                                0
                            )::bigint,
                            0
                        ),
                        paused_at = NULL,
                        last_resumed_at = pg_catalog.transaction_timestamp()"""
            if campaigns_have_pause_columns(self.session)
            else ""
        )
        row = (
            self.session.execute(
                text(
                    f"""
                    UPDATE campaigns
                    SET status = :target_status,
                        schedule_generation = schedule_generation + 1
                        {pause_metadata_sql}
                    WHERE workspace_id = :workspace_id AND id = :campaign_id
                      AND version = :expected_version
                      AND status = 'PAUSED'
                    RETURNING *
                    """  # noqa: S608 -- pause_metadata_sql is a fixed constant above
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "expected_version": expected_version,
                    "target_status": target_status,
                },
            )
            .mappings()
            .first()
        )
        if row is not None:
            self._record_audit_event(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="campaign.resume",
                target_id=campaign_id,
                before_state={"status": "PAUSED"},
                after_state={
                    "status": target_status,
                    "schedule_generation": int(row["schedule_generation"]),
                },
            )
        return row

    def database_now(self) -> Any:
        """The database clock. SCHEDULED campaigns start by this clock (the scheduler's
        due-start), so resume must decide RUNNING-vs-SCHEDULED by the same one."""
        return self.session.execute(
            text("SELECT pg_catalog.transaction_timestamp()")
        ).scalar_one()

    def count_outstanding_attempts(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> int:
        """Messages that are already past the send gate (claimed, being sent or
        of unknown outcome). A pause cannot recall them (CAMPAIGN_STATE_MACHINE:
        'Pause response includes outstanding attempt count')."""
        value = self.session.execute(
            text(
                """
                SELECT count(*) FROM messages
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND purpose = 'CAMPAIGN'
                  AND status IN ('QUEUED', 'SENDING', 'UNKNOWN_OUTCOME')
                """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        ).scalar()
        return int(value or 0)

    def insert_planning_job(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        activation_id: UUID,
        sequence_id: UUID,
        phase: str,
        total_count: int,
        audience_id: UUID | None = None,
    ) -> RowMapping:
        job_id = uuid.uuid4()
        row = (
            self.session.execute(
                text(
                    """
                    INSERT INTO campaign_planning_jobs
                        (id, workspace_id, campaign_id, activation_id, sequence_id,
                         phase, state, total_count, next_due_at, audience_id)
                    VALUES
                        (:id, :workspace_id, :campaign_id, :activation_id, :sequence_id,
                         :phase, 'PENDING', :total_count,
                         pg_catalog.transaction_timestamp(),
                         CAST(:audience_id AS uuid))
                    RETURNING *
                    """
                ),
                {
                    "id": str(job_id),
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "activation_id": str(activation_id),
                    "sequence_id": str(sequence_id),
                    "phase": phase,
                    "total_count": total_count,
                    "audience_id": str(audience_id) if audience_id else None,
                },
            )
            .mappings()
            .one()
        )
        return row

    def get_planning_jobs(
        self, *, workspace_id: UUID, campaign_id: UUID, activation_id: UUID
    ) -> Sequence[RowMapping]:
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_planning_jobs
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                      AND activation_id = :activation_id
                      AND audience_id IS NULL
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "activation_id": str(activation_id),
                },
            )
            .mappings()
            .all()
        )
        return rows

    def list_supplement_audiences(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> Sequence[RowMapping]:
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM campaign_audiences
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                      AND kind = 'SUPPLEMENT'
                    ORDER BY revision ASC
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .all()
        )
        return rows

    def list_supplement_jobs(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> Sequence[RowMapping]:
        """ENROLL/RENDER jobs of supplement audiences (ADR-0020)."""
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT audience_id, phase, state, processed_count, error_reason
                    FROM campaign_planning_jobs
                    WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                      AND phase IN ('ENROLL', 'RENDER') AND audience_id IS NOT NULL
                    """
                ),
                {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
            )
            .mappings()
            .all()
        )
        return rows

    def count_unfinished_supplement_jobs(
        self, *, workspace_id: UUID, campaign_id: UUID
    ) -> int:
        """Supplement jobs still running. Resume waits for them: a half-enrolled
        supplement must not start sending."""
        value = self.session.execute(
            text(
                """
                SELECT count(*) FROM campaign_planning_jobs
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND phase IN ('ENROLL', 'RENDER') AND audience_id IS NOT NULL
                  AND state IN ('PENDING', 'PROCESSING')
                """
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        ).scalar()
        return int(value or 0)

    def count_failed_planning_jobs(
        self, *, workspace_id: UUID, campaign_id: UUID, supplements_only: bool = False
    ) -> int:
        """ENROLL/RENDER jobs that failed. Their leads' first emails may never be
        created or rendered, so resume refuses until someone retries them."""
        value = self.session.execute(
            text(
                f"""
                SELECT count(*) FROM campaign_planning_jobs
                WHERE workspace_id = :workspace_id AND campaign_id = :campaign_id
                  AND phase IN ('ENROLL', 'RENDER') AND state = 'FAILED'
                  {"AND audience_id IS NOT NULL" if supplements_only else ""}
                """  # noqa: S608 -- the optional clause is a fixed constant
            ),
            {"workspace_id": str(workspace_id), "campaign_id": str(campaign_id)},
        ).scalar()
        return int(value or 0)

    def get_command_receipt(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        operation: str,
        request_key: str,
    ) -> RowMapping | None:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT * FROM command_receipts
                    WHERE workspace_id = :workspace_id AND actor_id = :actor_id
                      AND operation = :operation AND request_key = :request_key
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "actor_id": str(actor_id),
                    "operation": operation,
                    "request_key": request_key,
                },
            )
            .mappings()
            .first()
        )
        return row

    def insert_pending_command_receipt(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        operation: str,
        request_key: str,
        payload_hash: str,
        resource_type: str,
        resource_id: UUID,
    ) -> RowMapping:
        """May raise sqlalchemy.exc.IntegrityError on
        command_receipts_identity_key if a truly simultaneous request with
        the same idempotency key is inserting concurrently -- callers must
        catch that and re-SELECT via get_command_receipt instead of treating
        it as a generic failure."""
        receipt_id = uuid.uuid4()
        row = (
            self.session.execute(
                text(
                    """
                    INSERT INTO command_receipts
                        (id, workspace_id, actor_id, operation, request_key,
                         payload_hash, resource_type, resource_id, status, expires_at)
                    VALUES
                        (:id, :workspace_id, :actor_id, :operation, :request_key,
                         :payload_hash, :resource_type, :resource_id, 'PENDING',
                         pg_catalog.transaction_timestamp() + interval '7 days')
                    RETURNING *
                    """
                ),
                {
                    "id": str(receipt_id),
                    "workspace_id": str(workspace_id),
                    "actor_id": str(actor_id),
                    "operation": operation,
                    "request_key": request_key,
                    "payload_hash": payload_hash,
                    "resource_type": resource_type,
                    "resource_id": str(resource_id),
                },
            )
            .mappings()
            .one()
        )
        return row

    def complete_command_receipt(
        self, *, workspace_id: UUID, receipt_id: UUID, response_version: int
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE command_receipts
                SET status = 'COMPLETED', response_version = :response_version
                WHERE workspace_id = :workspace_id AND id = :receipt_id
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "receipt_id": str(receipt_id),
                "response_version": response_version,
            },
        )

    # -------------------------------------------------------------------
    # Audit
    # -------------------------------------------------------------------

    def record_campaign_audit(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        campaign_id: UUID,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
    ) -> None:
        """Audit row for a user action on a campaign made outside this
        repository (paused-edit services). Ids and field names only."""
        self._record_audit_event(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=action,
            target_id=campaign_id,
            before_state=before_state,
            after_state=after_state,
        )

    def _record_audit_event(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        action: str,
        target_id: UUID,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO audit_events
                    (workspace_id, actor_kind, actor_id, action, target_type,
                     target_id, before_state, after_state)
                VALUES
                    (:workspace_id, 'USER', :actor_id, :action, 'campaign',
                     :target_id,
                     CAST(:before_state AS jsonb),
                     CAST(:after_state AS jsonb))
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "actor_id": str(actor_id),
                "action": action,
                "target_id": str(target_id),
                "before_state": json.dumps(before_state) if before_state else None,
                "after_state": json.dumps(after_state) if after_state else None,
            },
        )
