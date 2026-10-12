from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.modules.analytics.schemas import (
    CampaignActivityItem,
    CampaignActivityPage,
    CampaignActivityStatus,
    CampaignActivityType,
)
from app.modules.analytics.service import _parse_date_bounds

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
_PREVIEW_CHARS = 300

# One row per fact, from tables the analytics already read. Every branch is
# scoped to the workspace and campaign; nothing here reads message bodies,
# provider references or user agents. Columns are the same in every branch:
#   row_key (unique, orders ties), kind, status, occurred_at, email, lead_id,
#   message_id, subject, step_position, d1..d3 (raw detail facts), open_count.
_FEED_SQL = """
    SELECT 'SENT:' || CAST(m.id AS TEXT) AS row_key, 'SENT' AS kind,
           'SUCCESS' AS status, m.accepted_at AS occurred_at,
           COALESCE(m.frozen_destination, ra.canonical_address) AS email,
           CAST(e.lead_id AS TEXT) AS lead_id, CAST(m.id AS TEXT) AS message_id,
           m.content_subject AS subject, st.position AS step_position,
           CAST(NULL AS TEXT) AS d1, CAST(NULL AS TEXT) AS d2,
           CAST(NULL AS TEXT) AS d3, CAST(NULL AS INTEGER) AS open_count
    FROM public.messages m
    LEFT JOIN public.campaign_enrollments e
      ON e.workspace_id = m.workspace_id AND e.id = m.enrollment_id
    LEFT JOIN public.recipient_addresses ra
      ON ra.workspace_id = m.workspace_id AND ra.id = m.address_id
    LEFT JOIN public.sequence_steps st
      ON st.workspace_id = m.workspace_id AND st.id = m.step_id
    WHERE m.workspace_id = :ws AND m.campaign_id = :cid
      AND m.status = 'SENT' AND m.accepted_at IS NOT NULL

    UNION ALL

    SELECT 'OPENED:' || CAST(me.id AS TEXT), 'OPENED', 'SUCCESS', me.qualified_at,
           COALESCE(m.frozen_destination, ra.canonical_address),
           CAST(e.lead_id AS TEXT), CAST(m.id AS TEXT),
           m.content_subject, st.position,
           CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
           me.qualified_count
    FROM public.message_events me
    JOIN public.messages m
      ON m.workspace_id = me.workspace_id AND m.id = me.message_id
    LEFT JOIN public.campaign_enrollments e
      ON e.workspace_id = m.workspace_id AND e.id = m.enrollment_id
    LEFT JOIN public.recipient_addresses ra
      ON ra.workspace_id = m.workspace_id AND ra.id = m.address_id
    LEFT JOIN public.sequence_steps st
      ON st.workspace_id = m.workspace_id AND st.id = m.step_id
    WHERE me.workspace_id = :ws AND m.campaign_id = :cid
      AND me.kind = 'OPENED' AND me.qualified_at IS NOT NULL

    UNION ALL

    SELECT 'BOUNCED:' || CAST(me.id AS TEXT), 'BOUNCED',
           CASE WHEN me.bounce_type = 'SOFT' THEN 'WARNING' ELSE 'ERROR' END,
           me.first_occurred_at,
           COALESCE(m.frozen_destination, ra.canonical_address),
           CAST(e.lead_id AS TEXT), CAST(m.id AS TEXT),
           m.content_subject, st.position,
           me.bounce_type, me.bounce_code, me.detail, CAST(NULL AS INTEGER)
    FROM public.message_events me
    JOIN public.messages m
      ON m.workspace_id = me.workspace_id AND m.id = me.message_id
    LEFT JOIN public.campaign_enrollments e
      ON e.workspace_id = m.workspace_id AND e.id = m.enrollment_id
    LEFT JOIN public.recipient_addresses ra
      ON ra.workspace_id = m.workspace_id AND ra.id = m.address_id
    LEFT JOIN public.sequence_steps st
      ON st.workspace_id = m.workspace_id AND st.id = m.step_id
    WHERE me.workspace_id = :ws AND m.campaign_id = :cid AND me.kind = 'BOUNCED'

    UNION ALL

    SELECT 'REPLIED:' || CAST(im.id AS TEXT), 'REPLIED',
           CASE WHEN COALESCE(im.classification, 'HUMAN_REPLY') = 'HUMAN_REPLY'
                THEN 'SUCCESS' ELSE 'INFO' END,
           im.received_at,
           COALESCE(om.frozen_destination, ra.canonical_address),
           CAST(e.lead_id AS TEXT), CAST(om.id AS TEXT),
           im.subject, st.position,
           SUBSTR(im.content_text, 1, 300), im.classification, CAST(NULL AS TEXT),
           CAST(NULL AS INTEGER)
    FROM public.inbound_outreach_links l
    JOIN public.inbound_messages im
      ON im.workspace_id = l.workspace_id AND im.id = l.inbound_message_id
    JOIN public.messages om
      ON om.workspace_id = l.workspace_id AND om.id = l.outbound_message_id
    LEFT JOIN public.campaign_enrollments e
      ON e.workspace_id = l.workspace_id AND e.id = l.enrollment_id
    LEFT JOIN public.recipient_addresses ra
      ON ra.workspace_id = om.workspace_id AND ra.id = om.address_id
    LEFT JOIN public.sequence_steps st
      ON st.workspace_id = om.workspace_id AND st.id = om.step_id
    WHERE l.workspace_id = :ws AND l.campaign_id = :cid
      AND l.status = 'CONFIRMED'
      AND COALESCE(im.classification, 'HUMAN_REPLY') <> 'BOUNCE'
      AND NOT EXISTS (
          SELECT 1 FROM public.inbound_outreach_links l2
          WHERE l2.workspace_id = l.workspace_id
            AND l2.inbound_message_id = l.inbound_message_id
            AND l2.campaign_id = l.campaign_id
            AND l2.status = 'CONFIRMED' AND l2.id > l.id
      )

    UNION ALL

    SELECT CASE WHEN ro.kind = 'COMPLAINT' THEN 'COMPLAINED:' ELSE 'UNSUBSCRIBED:' END
               || CAST(ro.id AS TEXT),
           CASE WHEN ro.kind = 'COMPLAINT' THEN 'COMPLAINED' ELSE 'UNSUBSCRIBED' END,
           CASE WHEN ro.kind = 'COMPLAINT' THEN 'ERROR' ELSE 'INFO' END,
           ro.occurred_at, e.frozen_destination,
           CAST(e.lead_id AS TEXT), CAST(NULL AS TEXT),
           CAST(NULL AS TEXT), CAST(NULL AS INTEGER),
           CAST(NULL AS TEXT), CAST(NULL AS TEXT), CAST(NULL AS TEXT),
           CAST(NULL AS INTEGER)
    FROM public.recipient_outcomes ro
    JOIN public.campaign_enrollments e
      ON e.workspace_id = ro.workspace_id AND e.id = ro.enrollment_id
    WHERE ro.workspace_id = :ws AND e.campaign_id = :cid
      AND ro.kind IN ('UNSUBSCRIBED', 'COMPLAINT')

    UNION ALL

    SELECT 'FAILED:' || CAST(m.id AS TEXT), 'FAILED',
           CASE WHEN m.status = 'FAILED' THEN 'ERROR' ELSE 'WARNING' END,
           COALESCE(a.completed_at, m.updated_at),
           COALESCE(m.frozen_destination, ra.canonical_address),
           CAST(e.lead_id AS TEXT), CAST(m.id AS TEXT),
           m.content_subject, st.position,
           a.error_category, a.error_code, m.terminal_reason,
           CAST(NULL AS INTEGER)
    FROM public.messages m
    LEFT JOIN public.message_attempts a
      ON a.workspace_id = m.workspace_id AND a.message_id = m.id
     AND a.ordinal = (
         SELECT MAX(a2.ordinal) FROM public.message_attempts a2
         WHERE a2.workspace_id = m.workspace_id AND a2.message_id = m.id
           AND a2.evidence_state = 'REJECTED')
    LEFT JOIN public.campaign_enrollments e
      ON e.workspace_id = m.workspace_id AND e.id = m.enrollment_id
    LEFT JOIN public.recipient_addresses ra
      ON ra.workspace_id = m.workspace_id AND ra.id = m.address_id
    LEFT JOIN public.sequence_steps st
      ON st.workspace_id = m.workspace_id AND st.id = m.step_id
    WHERE m.workspace_id = :ws AND m.campaign_id = :cid
      AND m.status IN ('FAILED', 'RETRY_SCHEDULED')
"""

_FAILURE_LABELS = {
    "RATE_LIMIT": "Provider rate limit",
    "TEMPORARY_PROVIDER_ERROR": "Temporary provider error",
    "TRANSIENT_FAILURE": "Temporary failure",
    "AUTH_FAILURE": "Mailbox authentication failed",
    "PERMANENT_RECIPIENT_FAILURE": "Recipient address rejected",
    "POLICY_REJECTION": "Rejected by provider policy",
    "NETWORK_ERROR": "Network error",
    "CONFIGURATION_FAILURE": "Mailbox configuration problem",
}


def _as_utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return cast(datetime, value)


def _uuid(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


def encode_activity_cursor(occurred_at: datetime, row_key: str) -> str:
    payload = json.dumps(
        {"t": occurred_at.isoformat(), "k": row_key}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_activity_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        raw = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return _as_utc(datetime.fromisoformat(raw["t"])), str(raw["k"])
    except Exception as exc:
        raise AppError(
            "validation_error", "Cursor is invalid", status_code=422
        ) from exc


def _date_bounds(
    start_date: str | None, end_date: str | None
) -> tuple[datetime | None, datetime | None]:
    """Optional whole-day UTC window; unlike analytics, no 30-day default."""
    if not start_date and not end_date:
        return None, None
    if start_date and end_date:
        return _parse_date_bounds(start_date, end_date)
    if start_date:
        start_dt, end_dt = _parse_date_bounds(start_date, None)
        return start_dt, end_dt
    _, end_dt = _parse_date_bounds(None, end_date)
    return None, end_dt


def _like_pattern(term: str) -> str:
    escaped = (
        term.strip()
        .lower()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _detail(kind: str, status: str, row: Mapping[str, Any]) -> str | None:
    d1, d2, d3 = row["d1"], row["d2"], row["d3"]
    if kind == "SENT":
        return "Accepted by the mail provider. Delivery is not confirmed."
    if kind == "OPENED":
        count = int(row["open_count"] or 0)
        return f"Opened {count} time{'' if count == 1 else 's'}"
    if kind == "BOUNCED":
        label = {"HARD": "Hard bounce", "SOFT": "Soft bounce"}.get(
            str(d1 or ""), "Bounce"
        )
        parts = [label]
        if d2:
            parts.append(str(d2))
        if d3:
            parts.append(str(d3))
        return " · ".join(parts)
    if kind == "REPLIED":
        preview = " ".join(str(d1 or "").split())[:_PREVIEW_CHARS]
        if str(d2 or "HUMAN_REPLY") != "HUMAN_REPLY":
            return f"Automatic reply{': ' + preview if preview else ''}"
        return preview or None
    if kind == "UNSUBSCRIBED":
        return "Recipient unsubscribed"
    if kind == "COMPLAINED":
        return "Recipient reported the email as spam"
    if kind == "FAILED":
        parts = [_FAILURE_LABELS.get(str(d1), str(d1)) if d1 else "Send failed"]
        if d2:
            parts.append(str(d2))
        if d3:
            parts.append(str(d3))
        if status == CampaignActivityStatus.WARNING.value:
            parts.append("will retry")
        return " · ".join(parts)
    return None


class CampaignActivityService:
    """Chronological, de-duplicated history of what happened in one campaign.

    A read-only projection over persisted facts (messages, message events,
    linked replies, recipient outcomes). One entry per underlying fact: a
    message that is opened repeatedly is one entry with an open count.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_activity(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        *,
        activity_type: CampaignActivityType | None = None,
        status: CampaignActivityStatus | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        q: str | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> CampaignActivityPage:
        ws, cid = str(context.workspace_id), str(campaign_id)
        limit = max(1, min(limit, MAX_LIMIT))
        start_dt, end_dt = _date_bounds(start_date, end_date)
        cursor_pos = decode_activity_cursor(cursor)

        exists = self.session.execute(
            text(
                "SELECT 1 FROM public.campaigns WHERE workspace_id = :ws AND id = :cid"
            ),
            {"ws": ws, "cid": cid},
        ).first()
        if exists is None:
            raise AppError("not_found", "Campaign not found", status_code=404)

        params: dict[str, Any] = {"ws": ws, "cid": cid}
        filters: list[str] = []
        if status is not None:
            filters.append("feed.status = :status")
            params["status"] = status.value
        if start_dt is not None:
            filters.append("feed.occurred_at >= :start_dt")
            params["start_dt"] = start_dt
        if end_dt is not None:
            filters.append("feed.occurred_at <= :end_dt")
            params["end_dt"] = end_dt
        if q and q.strip():
            filters.append(
                "(LOWER(feed.email) LIKE :q ESCAPE '\\' "
                "OR LOWER(feed.subject) LIKE :q ESCAPE '\\')"
            )
            params["q"] = _like_pattern(q)

        page_filters = list(filters)
        page_params = dict(params)
        if activity_type is not None:
            page_filters.append("feed.kind = :kind")
            page_params["kind"] = activity_type.value
        if cursor_pos is not None:
            page_filters.append(
                "(feed.occurred_at < :cur_at OR "
                "(feed.occurred_at = :cur_at AND feed.row_key < :cur_key))"
            )
            page_params["cur_at"], page_params["cur_key"] = cursor_pos
        where = f"WHERE {' AND '.join(page_filters)}" if page_filters else ""
        rows = (
            self.session.execute(
                text(
                    f"SELECT * FROM ({_FEED_SQL}) feed {where} "
                    "ORDER BY feed.occurred_at DESC, feed.row_key DESC LIMIT :lim"
                ),
                {**page_params, "lim": limit + 1},
            )
            .mappings()
            .all()
        )

        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._item(dict(row)) for row in rows]
        next_cursor = (
            encode_activity_cursor(items[-1].occurred_at, str(rows[-1]["row_key"]))
            if has_more and items
            else None
        )

        counts: dict[str, int] | None = None
        if cursor_pos is None:
            count_where = f"WHERE {' AND '.join(filters)}" if filters else ""
            count_rows = self.session.execute(
                text(
                    f"SELECT feed.kind AS kind, COUNT(*) AS n FROM ({_FEED_SQL}) feed "
                    f"{count_where} GROUP BY feed.kind"
                ),
                params,
            ).all()
            counts = {t.value: 0 for t in CampaignActivityType}
            for kind, n in count_rows:
                counts[str(kind)] = int(n)

        return CampaignActivityPage(
            campaign_id=campaign_id,
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            counts_by_type=counts,
        )

    @staticmethod
    def _item(row: Mapping[str, Any]) -> CampaignActivityItem:
        kind, status = str(row["kind"]), str(row["status"])
        is_open = kind == CampaignActivityType.OPENED.value
        return CampaignActivityItem(
            id=str(row["row_key"]),
            type=CampaignActivityType(kind),
            status=CampaignActivityStatus(status),
            occurred_at=_as_utc(row["occurred_at"]),
            recipient_email=row["email"],
            lead_id=_uuid(row["lead_id"]),
            message_id=_uuid(row["message_id"]),
            subject=row["subject"],
            step_position=row["step_position"],
            detail=_detail(kind, status, row),
            open_count=int(row["open_count"] or 0) if is_open else None,
        )
