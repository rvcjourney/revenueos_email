from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.safety.thresholds import (
    BOUNCE_CRITICAL_PERCENT,
    BOUNCE_WARNING_PERCENT,
    COMPLAINT_CRITICAL_PERCENT,
    bounce_level,
    complaints_critical,
    rate_percent,
)
from app.modules.tracking.pixel import open_tracking_ready

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


def _pct(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 2) if denominator > 0 else 0.0


class AnalyticsRepository:
    """Authoritative repository for analytical queries and deliverability metrics.

    Open and bounce metrics are message-level and come only from
    ``message_events`` through ``_message_event_counts``; every view (campaign,
    step, workspace, deliverability) uses that one definition:

    - sent            SENT messages
    - bounced         distinct sent messages with a BOUNCED event
    - delivered (est.) sent - bounced. No provider reports delivery, so this is
                      "accepted and not reported undeliverable"
    - opened          distinct delivered messages with a human-like OPENED hit
                      (scanner/prefetch hits are stored but never counted)
    - bounce rate     bounced / sent
    - open rate       opened / delivered (est.)
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def _message_event_counts(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID | None = None,
        mailbox_id: UUID | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        by_step: bool = False,
    ) -> list[dict[str, Any]]:
        """Bounce/open counts over SENT messages (one row, or one per step)."""
        filters = ["m.workspace_id = :ws", "m.status = 'SENT'"]
        params: dict[str, Any] = {"ws": str(workspace_id)}
        if campaign_id:
            filters.append("m.campaign_id = :cid")
            params["cid"] = str(campaign_id)
        if mailbox_id:
            filters.append("m.mailbox_id = :mid")
            params["mid"] = str(mailbox_id)
        if start is not None and end is not None:
            filters.append("m.accepted_at >= :start AND m.accepted_at <= :end")
            params["start"] = start
            params["end"] = end
        group_col = "m.step_id AS step_id," if by_step else ""
        group_by = "GROUP BY m.step_id" if by_step else ""
        rows = (
            self.session.execute(
                text(
                    f"""
                    SELECT {group_col}
                        COUNT(CASE WHEN b.id IS NOT NULL THEN 1 END) AS bounced,
                        COUNT(CASE WHEN b.bounce_type = 'HARD' THEN 1 END) AS hard_bounced,
                        COUNT(CASE WHEN b.bounce_type = 'SOFT' THEN 1 END) AS soft_bounced,
                        COUNT(CASE WHEN b.id IS NULL AND o.id IS NOT NULL THEN 1 END) AS opened,
                        COALESCE(SUM(CASE WHEN b.id IS NULL THEN o.qualified_count END), 0)
                            AS total_opens
                    FROM public.messages m
                    LEFT JOIN public.message_events b
                      ON b.workspace_id = m.workspace_id AND b.message_id = m.id
                     AND b.kind = 'BOUNCED'
                    LEFT JOIN public.message_events o
                      ON o.workspace_id = m.workspace_id AND o.message_id = m.id
                     AND o.kind = 'OPENED' AND o.qualified_at IS NOT NULL
                    WHERE {" AND ".join(filters)}
                    {group_by}
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _tracking_supported() -> bool:
        return open_tracking_ready(Settings.current())

    def get_campaign_analytics(
        self, workspace_id: UUID, campaign_id: UUID
    ) -> dict[str, Any] | None:
        """Fetch authoritative campaign performance metrics."""
        _safe_set_role(self.session, "app_api")
        _safe_set_workspace(self.session, workspace_id)

        # 1. Verify campaign exists in this workspace
        camp_row = (
            self.session.execute(
                text(
                    """
                SELECT id, name, status
                FROM public.campaigns
                WHERE workspace_id = :ws AND id = :cid
                """
                ),
                {"ws": str(workspace_id), "cid": str(campaign_id)},
            )
            .mappings()
            .first()
        )

        if not camp_row:
            return None

        # 2. Recipient enrollment counts
        enr_row = (
            self.session.execute(
                text(
                    """
                SELECT
                    COUNT(DISTINCT address_id) AS total_recipients,
                    COUNT(id) AS enrolled
                FROM public.campaign_enrollments
                WHERE workspace_id = :ws AND campaign_id = :cid
                """
                ),
                {"ws": str(workspace_id), "cid": str(campaign_id)},
            )
            .mappings()
            .first()
        )

        total_recipients = enr_row["total_recipients"] if enr_row else 0
        enrolled = enr_row["enrolled"] if enr_row else 0

        # 3. Outbound message counts & unique recipients contacted
        msg_row = (
            self.session.execute(
                text(
                    """
                SELECT
                    COUNT(CASE WHEN status = 'SENT' THEN 1 END) AS sent,
                    COUNT(CASE WHEN status IN ('SCHEDULED', 'RETRY_SCHEDULED', 'QUEUED') THEN 1 END) AS scheduled,
                    COUNT(CASE WHEN status = 'FAILED' THEN 1 END) AS failed,
                    COUNT(CASE WHEN status = 'CANCELLED' THEN 1 END) AS cancelled,
                    COUNT(CASE WHEN status = 'SKIPPED' THEN 1 END) AS skipped,
                    COUNT(CASE WHEN status IN ('PLANNED', 'SCHEDULED', 'QUEUED', 'RETRY_SCHEDULED') THEN 1 END) AS remaining,
                    COUNT(DISTINCT CASE WHEN status = 'SENT' THEN address_id END) AS unique_recipients_contacted
                FROM public.messages
                WHERE workspace_id = :ws AND campaign_id = :cid
                """
                ),
                {"ws": str(workspace_id), "cid": str(campaign_id)},
            )
            .mappings()
            .first()
        )

        sent = msg_row["sent"] if msg_row else 0
        scheduled = msg_row["scheduled"] if msg_row else 0
        failed = msg_row["failed"] if msg_row else 0
        cancelled = msg_row["cancelled"] if msg_row else 0
        skipped = msg_row["skipped"] if msg_row else 0
        remaining = msg_row["remaining"] if msg_row else 0
        unique_recipients_contacted = (
            msg_row["unique_recipients_contacted"] if msg_row else 0
        )

        # 4. Deduplicated outcomes for this campaign
        outcomes_row = (
            self.session.execute(
                text(
                    """
                SELECT
                    COUNT(DISTINCT CASE WHEN ro.kind = 'COMPLAINT' THEN ro.enrollment_id END) AS complained,
                    COUNT(DISTINCT CASE WHEN ro.kind = 'UNSUBSCRIBED' THEN ro.enrollment_id END) AS unsubscribed,
                    COUNT(DISTINCT CASE WHEN ro.kind = 'REPLIED' THEN ro.enrollment_id END) AS replied,
                    COUNT(DISTINCT CASE WHEN ro.kind = 'REPLIED' THEN e.address_id END) AS unique_recipients_replied
                FROM public.recipient_outcomes ro
                JOIN public.campaign_enrollments e
                  ON e.workspace_id = ro.workspace_id AND e.id = ro.enrollment_id
                WHERE ro.workspace_id = :ws AND e.campaign_id = :cid
                """
                ),
                {"ws": str(workspace_id), "cid": str(campaign_id)},
            )
            .mappings()
            .first()
        )

        events = self._message_event_counts(
            workspace_id=workspace_id, campaign_id=campaign_id
        )[0]
        bounced = events["bounced"]
        delivered_estimated = max(sent - bounced, 0)
        opened = events["opened"]
        complained = outcomes_row["complained"] if outcomes_row else 0
        unsubscribed = outcomes_row["unsubscribed"] if outcomes_row else 0
        replied = outcomes_row["replied"] if outcomes_row else 0
        unique_recipients_replied = (
            outcomes_row["unique_recipients_replied"] if outcomes_row else 0
        )

        # Rates (with division-by-zero protection)
        bounce_rate = _pct(bounced, sent)
        open_rate = _pct(opened, delivered_estimated)
        complaint_rate = round((complained / sent) * 100, 2) if sent > 0 else 0.0
        unsubscribe_rate = round((unsubscribed / sent) * 100, 2) if sent > 0 else 0.0
        reply_rate = round((replied / sent) * 100, 2) if sent > 0 else 0.0
        failure_rate = (
            round((failed / (sent + failed)) * 100, 2) if (sent + failed) > 0 else 0.0
        )

        return {
            "campaign_id": UUID(str(camp_row["id"])),
            "campaign_name": camp_row["name"],
            "campaign_status": camp_row["status"],
            "total_recipients": total_recipients,
            "enrolled": enrolled,
            "scheduled": scheduled,
            "sent": sent,
            "failed": failed,
            "cancelled": cancelled,
            "skipped": skipped,
            "remaining": remaining,
            "bounced": bounced,
            "hard_bounced": events["hard_bounced"],
            "soft_bounced": events["soft_bounced"],
            "delivered_estimated": delivered_estimated,
            "opened": opened,
            "total_opens": int(events["total_opens"] or 0),
            "open_rate": open_rate,
            "complained": complained,
            "unsubscribed": unsubscribed,
            "replied": replied,
            "unique_recipients_contacted": unique_recipients_contacted,
            "unique_recipients_replied": unique_recipients_replied,
            "bounce_rate": bounce_rate,
            "complaint_rate": complaint_rate,
            "unsubscribe_rate": unsubscribe_rate,
            "reply_rate": reply_rate,
            "failure_rate": failure_rate,
            "open_tracking_supported": self._tracking_supported(),
            "click_tracking_supported": False,
            "delivery_confirmation_supported": False,
        }

    def get_sequence_analytics(
        self, workspace_id: UUID, campaign_id: UUID
    ) -> list[dict[str, Any]] | None:
        """Fetch step-level performance with authoritative reply attribution."""
        _safe_set_role(self.session, "app_api")
        _safe_set_workspace(self.session, workspace_id)

        # Check campaign exists
        camp = self.session.execute(
            text(
                "SELECT id FROM public.campaigns WHERE workspace_id = :ws AND id = :cid"
            ),
            {"ws": str(workspace_id), "cid": str(campaign_id)},
        ).first()
        if not camp:
            return None

        # Fetch sequence steps
        step_rows = (
            self.session.execute(
                text(
                    """
                SELECT s.id, s.position, s.kind, s.email_subject
                FROM public.sequence_steps s
                JOIN public.campaign_sequences seq
                  ON seq.workspace_id = s.workspace_id AND seq.id = s.sequence_id
                WHERE seq.workspace_id = :ws AND seq.campaign_id = :cid
                ORDER BY s.position ASC
                """
                ),
                {"ws": str(workspace_id), "cid": str(campaign_id)},
            )
            .mappings()
            .all()
        )

        if not step_rows:
            return []

        # Message counts by step
        msg_counts = (
            self.session.execute(
                text(
                    """
                SELECT
                    step_id,
                    COUNT(CASE WHEN status IN ('SCHEDULED', 'RETRY_SCHEDULED', 'QUEUED') THEN 1 END) AS scheduled,
                    COUNT(CASE WHEN status = 'SENT' THEN 1 END) AS sent,
                    COUNT(CASE WHEN status = 'FAILED' THEN 1 END) AS failed,
                    COUNT(CASE WHEN status = 'CANCELLED' THEN 1 END) AS cancelled
                FROM public.messages
                WHERE workspace_id = :ws AND campaign_id = :cid AND step_id IS NOT NULL
                GROUP BY step_id
                """
                ),
                {"ws": str(workspace_id), "cid": str(campaign_id)},
            )
            .mappings()
            .all()
        )
        msg_by_step = {str(r["step_id"]): r for r in msg_counts}

        # Reply attribution per step (via inbound_outreach_links outbound_message_id)
        reply_counts = (
            self.session.execute(
                text(
                    """
                SELECT
                    m.step_id,
                    COUNT(DISTINCT l.enrollment_id) AS replied
                FROM public.inbound_outreach_links l
                JOIN public.messages m
                  ON m.workspace_id = l.workspace_id AND m.id = l.outbound_message_id
                JOIN public.inbound_messages im
                  ON im.workspace_id = l.workspace_id AND im.id = l.inbound_message_id
                WHERE l.workspace_id = :ws AND l.campaign_id = :cid AND l.status = 'CONFIRMED'
                  AND COALESCE(im.classification, 'HUMAN_REPLY') NOT IN
                      ('OUT_OF_OFFICE', 'AUTOMATED', 'BOUNCE')
                  AND m.step_id IS NOT NULL
                GROUP BY m.step_id
                """
                ),
                {"ws": str(workspace_id), "cid": str(campaign_id)},
            )
            .mappings()
            .all()
        )
        reply_by_step = {str(r["step_id"]): r["replied"] for r in reply_counts}

        # Outcomes (bounces / unsubscribes) per step
        outcomes_counts = (
            self.session.execute(
                text(
                    """
                SELECT
                    m.step_id,
                    COUNT(DISTINCT CASE WHEN ro.kind = 'UNSUBSCRIBED' THEN ro.enrollment_id END) AS unsubscribed
                FROM public.recipient_outcomes ro
                JOIN public.messages m
                  ON m.workspace_id = ro.workspace_id AND m.enrollment_id = ro.enrollment_id
                WHERE ro.workspace_id = :ws AND m.campaign_id = :cid AND m.status = 'SENT'
                  AND m.step_id IS NOT NULL
                GROUP BY m.step_id
                """
                ),
                {"ws": str(workspace_id), "cid": str(campaign_id)},
            )
            .mappings()
            .all()
        )
        outcomes_by_step = {str(r["step_id"]): r for r in outcomes_counts}
        events_by_step = {
            str(r["step_id"]): r
            for r in self._message_event_counts(
                workspace_id=workspace_id, campaign_id=campaign_id, by_step=True
            )
            if r["step_id"] is not None
        }

        results: list[dict[str, Any]] = []
        for s in step_rows:
            s_id = str(s["id"])
            m_stat = msg_by_step.get(s_id, {})
            o_stat = outcomes_by_step.get(s_id, {})

            sent = m_stat.get("sent", 0)
            scheduled = m_stat.get("scheduled", 0)
            failed = m_stat.get("failed", 0)
            cancelled = m_stat.get("cancelled", 0)
            replied = reply_by_step.get(s_id, 0)
            e_stat = events_by_step.get(s_id, {})
            bounced = e_stat.get("bounced", 0)
            opened = e_stat.get("opened", 0)
            unsubscribed = o_stat.get("unsubscribed", 0) if o_stat else 0

            reply_rate = round((replied / sent) * 100, 2) if sent > 0 else 0.0

            results.append(
                {
                    "step_id": UUID(s_id),
                    "position": s["position"],
                    "kind": s["kind"],
                    "subject": s.get("email_subject"),
                    "scheduled": scheduled,
                    "sent": sent,
                    "failed": failed,
                    "cancelled": cancelled,
                    "bounced": bounced,
                    "opened": opened,
                    "replied": replied,
                    "unsubscribed": unsubscribed,
                    "bounce_rate": _pct(bounced, sent),
                    "open_rate": _pct(opened, max(sent - bounced, 0)),
                    "reply_rate": reply_rate,
                }
            )

        return results

    def get_workspace_overview(
        self,
        workspace_id: UUID,
        start_date: datetime,
        end_date: datetime,
        timezone_name: str = "UTC",
        campaign_id: UUID | None = None,
        mailbox_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Aggregate workspace performance over date window with zero-filled trend."""
        _safe_set_role(self.session, "app_api")
        _safe_set_workspace(self.session, workspace_id)

        # Build dynamic query filters
        filters = ["workspace_id = :ws"]
        params: dict[str, Any] = {
            "ws": str(workspace_id),
            "start": start_date,
            "end": end_date,
        }
        if campaign_id:
            filters.append("campaign_id = :cid")
            params["cid"] = str(campaign_id)
        if mailbox_id:
            filters.append("mailbox_id = :mid")
            params["mid"] = str(mailbox_id)

        where_clause = " AND ".join(filters)

        # 1. Total sent & unique prospects
        sent_query = text(
            f"""
            SELECT
                COUNT(*) AS emails_sent,
                COUNT(DISTINCT address_id) AS prospects_contacted
            FROM public.messages
            WHERE {where_clause}
              AND status = 'SENT'
              AND accepted_at >= :start AND accepted_at <= :end
            """
        )
        sent_row = self.session.execute(sent_query, params).mappings().first()
        emails_sent = sent_row["emails_sent"] if sent_row else 0
        prospects_contacted = sent_row["prospects_contacted"] if sent_row else 0

        # 2. Failed sends
        failed_query = text(
            f"""
            SELECT COUNT(*) AS failed_sends
            FROM public.messages
            WHERE {where_clause}
              AND status = 'FAILED'
              AND updated_at >= :start AND updated_at <= :end
            """
        )
        failed_row = self.session.execute(failed_query, params).mappings().first()
        failed_sends = failed_row["failed_sends"] if failed_row else 0

        # 3. Outcomes (replies, bounces, unsubscribes, complaints) in window
        # To respect campaign_id / mailbox_id filters if supplied:
        outcome_joins = ""
        outcome_filters = [
            "ro.workspace_id = :ws",
            "ro.occurred_at >= :start",
            "ro.occurred_at <= :end",
        ]
        if campaign_id:
            outcome_joins += " JOIN public.campaign_enrollments e ON e.workspace_id = ro.workspace_id AND e.id = ro.enrollment_id"
            outcome_filters.append("e.campaign_id = :cid")
        if mailbox_id:
            # Outcomes belong to an enrollment; a mailbox filter means "the
            # enrollment was sent from that mailbox" (not "the outcome arrived
            # through it", which excludes bounces that have no inbound message).
            outcome_joins += (
                " JOIN public.campaign_enrollments em ON em.workspace_id = ro.workspace_id"
                " AND em.id = ro.enrollment_id"
                " AND EXISTS (SELECT 1 FROM public.messages mm"
                " WHERE mm.workspace_id = ro.workspace_id AND mm.enrollment_id = em.id"
                " AND mm.mailbox_id = :mid)"
            )

        outcome_where = " AND ".join(outcome_filters)
        outcome_query = text(
            f"""
            SELECT
                COUNT(DISTINCT CASE WHEN ro.kind = 'REPLIED' THEN ro.enrollment_id END) AS replies,
                COUNT(DISTINCT CASE WHEN ro.kind = 'UNSUBSCRIBED' THEN ro.enrollment_id END) AS unsubscribes,
                COUNT(DISTINCT CASE WHEN ro.kind = 'COMPLAINT' THEN ro.enrollment_id END) AS complaints
            FROM public.recipient_outcomes ro
            {outcome_joins}
            WHERE {outcome_where}
            """
        )
        outcome_row = self.session.execute(outcome_query, params).mappings().first()
        replies = outcome_row["replies"] if outcome_row else 0
        window_events = self._message_event_counts(
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            mailbox_id=mailbox_id,
            start=start_date,
            end=end_date,
        )[0]
        bounces = window_events["bounced"]
        opens = window_events["opened"]
        delivered_estimated = max(emails_sent - bounces, 0)
        unsubscribes = outcome_row["unsubscribes"] if outcome_row else 0
        complaints = outcome_row["complaints"] if outcome_row else 0

        # Rates
        reply_rate = round((replies / emails_sent) * 100, 2) if emails_sent > 0 else 0.0
        bounce_rate = _pct(bounces, emails_sent)
        open_rate = _pct(opens, delivered_estimated)
        complaint_rate = (
            round((complaints / emails_sent) * 100, 2) if emails_sent > 0 else 0.0
        )
        unsubscribe_rate = (
            round((unsubscribes / emails_sent) * 100, 2) if emails_sent > 0 else 0.0
        )

        # 4. Daily Trend Buckets (zero-filled across entire calendar span)
        # Fetch raw daily points for sent
        sent_daily_query = text(
            f"""
            SELECT accepted_at
            FROM public.messages
            WHERE {where_clause}
              AND status = 'SENT'
              AND accepted_at >= :start AND accepted_at <= :end
            """
        )
        sent_dates = self.session.execute(sent_daily_query, params).scalars().all()

        reply_daily_query = text(
            f"""
            SELECT ro.occurred_at
            FROM public.recipient_outcomes ro
            {outcome_joins}
            WHERE {outcome_where} AND ro.kind = 'REPLIED'
            """
        )
        reply_dates = self.session.execute(reply_daily_query, params).scalars().all()

        bounce_filters = [
            "me.workspace_id = :ws",
            "me.kind = 'BOUNCED'",
            "me.first_occurred_at >= :start",
            "me.first_occurred_at <= :end",
        ]
        if campaign_id:
            bounce_filters.append("bm.campaign_id = :cid")
        if mailbox_id:
            bounce_filters.append("bm.mailbox_id = :mid")
        bounce_daily_query = text(
            f"""
            SELECT me.first_occurred_at
            FROM public.message_events me
            JOIN public.messages bm
              ON bm.workspace_id = me.workspace_id AND bm.id = me.message_id
            WHERE {" AND ".join(bounce_filters)}
            """
        )
        bounce_dates = self.session.execute(bounce_daily_query, params).scalars().all()

        # Aggregate counts by calendar date string
        sent_counts_by_day: dict[str, int] = defaultdict(int)
        for dt in sent_dates:
            if dt:
                d_str = (
                    dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
                )
                sent_counts_by_day[d_str] += 1

        reply_counts_by_day: dict[str, int] = defaultdict(int)
        for dt in reply_dates:
            if dt:
                d_str = (
                    dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
                )
                reply_counts_by_day[d_str] += 1

        bounce_counts_by_day: dict[str, int] = defaultdict(int)
        for dt in bounce_dates:
            if dt:
                d_str = (
                    dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
                )
                bounce_counts_by_day[d_str] += 1

        # Build zero-filled trend series
        trend: list[dict[str, Any]] = []
        curr = start_date.date() if isinstance(start_date, datetime) else start_date
        target_end = end_date.date() if isinstance(end_date, datetime) else end_date

        while curr <= target_end:
            d_str = curr.strftime("%Y-%m-%d")
            trend.append(
                {
                    "date": d_str,
                    "sent": sent_counts_by_day.get(d_str, 0),
                    "replies": reply_counts_by_day.get(d_str, 0),
                    "bounces": bounce_counts_by_day.get(d_str, 0),
                }
            )
            curr += timedelta(days=1)

        # 5. Top Campaigns (up to 5 by sent count)
        top_campaigns_query = text(
            """
            SELECT
                c.id AS campaign_id,
                c.name,
                c.status,
                COUNT(m.id) AS sent
            FROM public.campaigns c
            LEFT JOIN public.messages m
              ON m.workspace_id = c.workspace_id AND m.campaign_id = c.id
              AND m.status = 'SENT' AND m.accepted_at >= :start AND m.accepted_at <= :end
            WHERE c.workspace_id = :ws
            GROUP BY c.id, c.name, c.status
            ORDER BY sent DESC
            LIMIT 5
            """
        )
        top_camp_rows = (
            self.session.execute(
                top_campaigns_query,
                {"ws": str(workspace_id), "start": start_date, "end": end_date},
            )
            .mappings()
            .all()
        )

        top_campaigns: list[dict[str, Any]] = []
        for cr in top_camp_rows:
            c_sent = cr["sent"]
            cid = str(cr["campaign_id"])
            # Get outcomes for this specific campaign in window
            c_out = (
                self.session.execute(
                    text(
                        """
                    SELECT
                        COUNT(DISTINCT CASE WHEN ro.kind = 'REPLIED' THEN ro.enrollment_id END) AS replies
                    FROM public.recipient_outcomes ro
                    JOIN public.campaign_enrollments e
                      ON e.workspace_id = ro.workspace_id AND e.id = ro.enrollment_id
                    WHERE ro.workspace_id = :ws AND e.campaign_id = :cid
                      AND ro.occurred_at >= :start AND ro.occurred_at <= :end
                    """
                    ),
                    {
                        "ws": str(workspace_id),
                        "cid": cid,
                        "start": start_date,
                        "end": end_date,
                    },
                )
                .mappings()
                .first()
            )
            c_rep = c_out["replies"] if c_out else 0
            c_ev = self._message_event_counts(
                workspace_id=workspace_id,
                campaign_id=UUID(cid),
                start=start_date,
                end=end_date,
            )[0]
            c_bnc = c_ev["bounced"]
            top_campaigns.append(
                {
                    "campaign_id": UUID(cid),
                    "name": cr["name"],
                    "status": cr["status"],
                    "sent": c_sent,
                    "replies": c_rep,
                    "reply_rate": round((c_rep / c_sent) * 100, 2)
                    if c_sent > 0
                    else 0.0,
                    "bounces": c_bnc,
                    "bounce_rate": _pct(c_bnc, c_sent),
                    "opens": c_ev["opened"],
                    "open_rate": _pct(c_ev["opened"], max(c_sent - c_bnc, 0)),
                }
            )

        # 6. Top Mailboxes (up to 5 by sent count)
        top_mb_rows = (
            self.session.execute(
                text(
                    """
                SELECT
                    mb.id AS mailbox_id,
                    mb.original_address,
                    mb.provider,
                    COUNT(m.id) AS sent
                FROM public.mailboxes mb
                LEFT JOIN public.messages m
                  ON m.workspace_id = mb.workspace_id AND m.mailbox_id = mb.id
                  AND m.status = 'SENT' AND m.accepted_at >= :start AND m.accepted_at <= :end
                WHERE mb.workspace_id = :ws
                GROUP BY mb.id, mb.original_address, mb.provider
                ORDER BY sent DESC
                LIMIT 5
                """
                ),
                {"ws": str(workspace_id), "start": start_date, "end": end_date},
            )
            .mappings()
            .all()
        )

        top_mailboxes: list[dict[str, Any]] = []
        for mbr in top_mb_rows:
            mb_sent = mbr["sent"]
            mbid = str(mbr["mailbox_id"])
            mb_out = (
                self.session.execute(
                    text(
                        """
                    SELECT
                        COUNT(DISTINCT CASE WHEN ro.kind = 'REPLIED' THEN ro.enrollment_id END) AS replies
                    FROM public.recipient_outcomes ro
                    JOIN public.messages m
                      ON m.workspace_id = ro.workspace_id AND m.enrollment_id = ro.enrollment_id
                    WHERE ro.workspace_id = :ws AND m.mailbox_id = :mid
                      AND ro.occurred_at >= :start AND ro.occurred_at <= :end
                    """
                    ),
                    {
                        "ws": str(workspace_id),
                        "mid": mbid,
                        "start": start_date,
                        "end": end_date,
                    },
                )
                .mappings()
                .first()
            )
            mb_rep = mb_out["replies"] if mb_out else 0
            mb_ev = self._message_event_counts(
                workspace_id=workspace_id,
                mailbox_id=UUID(mbid),
                start=start_date,
                end=end_date,
            )[0]
            mb_bnc = mb_ev["bounced"]
            top_mailboxes.append(
                {
                    "mailbox_id": UUID(mbid),
                    "email_address": mbr["original_address"],
                    "provider": mbr["provider"],
                    "sent": mb_sent,
                    "replies": mb_rep,
                    "reply_rate": round((mb_rep / mb_sent) * 100, 2)
                    if mb_sent > 0
                    else 0.0,
                    "bounces": mb_bnc,
                    "bounce_rate": _pct(mb_bnc, mb_sent),
                    "opens": mb_ev["opened"],
                    "open_rate": _pct(mb_ev["opened"], max(mb_sent - mb_bnc, 0)),
                }
            )

        return {
            "workspace_id": workspace_id,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "timezone": timezone_name,
            "prospects_contacted": prospects_contacted,
            "emails_sent": emails_sent,
            "replies": replies,
            "bounces": bounces,
            "opens": opens,
            "delivered_estimated": delivered_estimated,
            "unsubscribes": unsubscribes,
            "complaints": complaints,
            "failed_sends": failed_sends,
            "reply_rate": reply_rate,
            "bounce_rate": bounce_rate,
            "open_rate": open_rate,
            "complaint_rate": complaint_rate,
            "unsubscribe_rate": unsubscribe_rate,
            "open_tracking_supported": self._tracking_supported(),
            "click_tracking_supported": False,
            "delivery_confirmation_supported": False,
            "trend": trend,
            "top_campaigns": top_campaigns,
            "top_mailboxes": top_mailboxes,
        }

    def get_deliverability_overview(
        self, workspace_id: UUID, start_date: datetime, end_date: datetime
    ) -> dict[str, Any]:
        """Fetch workspace deliverability health, warnings, mailbox operational stats, and failure breakdown."""
        _safe_set_role(self.session, "app_api")
        _safe_set_workspace(self.session, workspace_id)

        # 1. Mailboxes
        mb_rows = (
            self.session.execute(
                text(
                    """
                SELECT id, original_address, provider, connection_state, health_state, policy_state
                FROM public.mailboxes
                WHERE workspace_id = :ws
                ORDER BY original_address ASC
                """
                ),
                {"ws": str(workspace_id)},
            )
            .mappings()
            .all()
        )

        # 2. Active safety holds
        holds = (
            self.session.execute(
                text(
                    """
                SELECT id, target_kind, target_mailbox_id, reason
                FROM public.safety_holds
                WHERE workspace_id = :ws AND status = 'ACTIVE'
                """
                ),
                {"ws": str(workspace_id)},
            )
            .mappings()
            .all()
        )

        holds_by_mailbox: dict[str, int] = defaultdict(int)
        workspace_holds_count = 0
        for h in holds:
            if h["target_kind"] == "MAILBOX" and h["target_mailbox_id"]:
                holds_by_mailbox[str(h["target_mailbox_id"])] += 1
            else:
                workspace_holds_count += 1

        # 3. Mailbox operational activity in date window
        mailbox_items: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        total_ws_sent = 0
        total_ws_bounces = 0
        total_ws_complaints = 0
        total_ws_failures = 0

        for mb in mb_rows:
            mid = str(mb["id"])

            # Sent count
            sent_count = (
                self.session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM public.messages
                        WHERE workspace_id = :ws AND mailbox_id = :mid
                          AND status = 'SENT' AND accepted_at >= :start AND accepted_at <= :end
                        """
                    ),
                    {
                        "ws": str(workspace_id),
                        "mid": mid,
                        "start": start_date,
                        "end": end_date,
                    },
                ).scalar()
                or 0
            )

            # Failure count (attempts rejected)
            failure_count = (
                self.session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM public.message_attempts
                        WHERE workspace_id = :ws AND mailbox_id = :mid
                          AND evidence_state = 'REJECTED' AND started_at >= :start AND started_at <= :end
                        """
                    ),
                    {
                        "ws": str(workspace_id),
                        "mid": mid,
                        "start": start_date,
                        "end": end_date,
                    },
                ).scalar()
                or 0
            )

            # Bounces & complaints (bounces are message-level, see class docs)
            outcomes = (
                self.session.execute(
                    text(
                        """
                    SELECT
                        COUNT(DISTINCT CASE WHEN ro.kind = 'COMPLAINT' THEN ro.enrollment_id END) AS complaints
                    FROM public.recipient_outcomes ro
                    JOIN public.messages m
                      ON m.workspace_id = ro.workspace_id AND m.enrollment_id = ro.enrollment_id
                    WHERE ro.workspace_id = :ws AND m.mailbox_id = :mid
                      AND ro.occurred_at >= :start AND ro.occurred_at <= :end
                    """
                    ),
                    {
                        "ws": str(workspace_id),
                        "mid": mid,
                        "start": start_date,
                        "end": end_date,
                    },
                )
                .mappings()
                .first()
            )

            bounce_count = self._message_event_counts(
                workspace_id=workspace_id,
                mailbox_id=UUID(mid),
                start=start_date,
                end=end_date,
            )[0]["bounced"]
            complaint_count = outcomes["complaints"] if outcomes else 0

            bounce_rate = rate_percent(bounce_count, sent_count)
            complaint_rate = rate_percent(complaint_count, sent_count)
            bounce_severity = bounce_level(bounce_rate, sent_count)

            active_holds = holds_by_mailbox.get(mid, 0)

            total_ws_sent += sent_count
            total_ws_bounces += bounce_count
            total_ws_complaints += complaint_count
            total_ws_failures += failure_count

            # Mailbox warnings
            mb_warnings: list[dict[str, Any]] = []
            health_status = "HEALTHY"

            if mb["connection_state"] != "CONNECTED":
                health_status = "DISCONNECTED"
                w = {
                    "code": "MAILBOX_CONNECTION_ISSUE",
                    "level": "CRITICAL",
                    "title": "Mailbox Disconnected",
                    "message": f"Mailbox {mb['original_address']} is in {mb['connection_state']} state. Reconnection required.",
                    "mailbox_id": UUID(mid),
                }
                mb_warnings.append(w)
                warnings.append(w)
            elif mb["health_state"] == "PAUSED_ERROR":
                health_status = "CRITICAL"
                w = {
                    "code": "MAILBOX_PAUSED_ERROR",
                    "level": "CRITICAL",
                    "title": "Mailbox Sending Paused",
                    "message": f"Mailbox {mb['original_address']} has paused sending due to operational errors.",
                    "mailbox_id": UUID(mid),
                }
                mb_warnings.append(w)
                warnings.append(w)

            if bounce_severity == "CRITICAL":
                health_status = (
                    "CRITICAL" if health_status != "DISCONNECTED" else health_status
                )
                w = {
                    "code": "HIGH_BOUNCE_RATE",
                    "level": "CRITICAL",
                    "title": "Critical Bounce Rate",
                    "message": f"Bounce rate on {mb['original_address']} is {bounce_rate}% (> {BOUNCE_CRITICAL_PERCENT}% threshold). Sending reputation at risk.",
                    "metric_value": bounce_rate,
                    "threshold": BOUNCE_CRITICAL_PERCENT,
                    "mailbox_id": UUID(mid),
                }
                mb_warnings.append(w)
                warnings.append(w)
            elif bounce_severity == "WARNING":
                if health_status == "HEALTHY":
                    health_status = "WARNING"
                w = {
                    "code": "ELEVATED_BOUNCE_RATE",
                    "level": "WARNING",
                    "title": "Elevated Bounce Rate",
                    "message": f"Bounce rate on {mb['original_address']} is {bounce_rate}% (> {BOUNCE_WARNING_PERCENT}% warning threshold).",
                    "metric_value": bounce_rate,
                    "threshold": BOUNCE_WARNING_PERCENT,
                    "mailbox_id": UUID(mid),
                }
                mb_warnings.append(w)
                warnings.append(w)

            if complaints_critical(complaint_rate, sent_count):
                health_status = (
                    "CRITICAL" if health_status != "DISCONNECTED" else health_status
                )
                w = {
                    "code": "HIGH_COMPLAINT_RATE",
                    "level": "CRITICAL",
                    "title": "High Spam Complaint Rate",
                    "message": f"Complaint rate on {mb['original_address']} is {complaint_rate}% (> {COMPLAINT_CRITICAL_PERCENT}% threshold). ESP suspension risk.",
                    "metric_value": complaint_rate,
                    "threshold": COMPLAINT_CRITICAL_PERCENT,
                    "mailbox_id": UUID(mid),
                }
                mb_warnings.append(w)
                warnings.append(w)

            if active_holds > 0:
                if health_status == "HEALTHY":
                    health_status = "WARNING"
                w = {
                    "code": "MAILBOX_SAFETY_HOLD",
                    "level": "WARNING",
                    "title": "Active Safety Hold",
                    "message": f"Mailbox {mb['original_address']} has {active_holds} active safety hold(s).",
                    "mailbox_id": UUID(mid),
                }
                mb_warnings.append(w)
                warnings.append(w)

            mailbox_items.append(
                {
                    "mailbox_id": UUID(mid),
                    "email_address": mb["original_address"],
                    "provider": mb["provider"],
                    "connection_state": mb["connection_state"],
                    "health_state": mb["health_state"],
                    "policy_state": mb["policy_state"],
                    "health_status": health_status,
                    "sent_count": sent_count,
                    "bounce_count": bounce_count,
                    "bounce_rate": bounce_rate,
                    "complaint_count": complaint_count,
                    "complaint_rate": complaint_rate,
                    "failure_count": failure_count,
                    "active_safety_holds_count": active_holds,
                    "warnings": mb_warnings,
                }
            )

        # Workspace active safety holds warning
        if workspace_holds_count > 0:
            warnings.append(
                {
                    "code": "WORKSPACE_SAFETY_HOLD",
                    "level": "CRITICAL",
                    "title": "Workspace Safety Hold Active",
                    "message": f"Workspace has {workspace_holds_count} global safety hold(s) active.",
                }
            )

        # 4. Failure breakdown by category
        failure_rows = (
            self.session.execute(
                text(
                    """
                SELECT error_category, COUNT(*) AS count
                FROM public.message_attempts
                WHERE workspace_id = :ws
                  AND evidence_state = 'REJECTED'
                  AND error_category IS NOT NULL
                  AND started_at >= :start AND started_at <= :end
                GROUP BY error_category
                ORDER BY count DESC
                """
                ),
                {"ws": str(workspace_id), "start": start_date, "end": end_date},
            )
            .mappings()
            .all()
        )

        category_descriptions = {
            "RATE_LIMIT": "Provider sending rate limit reached or throttled",
            "TEMPORARY_PROVIDER_ERROR": "Transient connection or provider server error",
            "AUTH_FAILURE": "Authentication or OAuth credential expiration",
            "PERMANENT_RECIPIENT_FAILURE": "Invalid, nonexistent, or disabled recipient address",
            "POLICY_REJECTION": "Provider spam filter or policy bounce",
            "NETWORK_ERROR": "Socket timeout or DNS resolution failure",
            "CONFIGURATION_FAILURE": "Misconfigured mailbox port or protocol settings",
        }

        failure_breakdown = [
            {
                "category": r["error_category"],
                "count": r["count"],
                "description": category_descriptions.get(
                    r["error_category"], "Operational provider error"
                ),
            }
            for r in failure_rows
        ]

        # Overall health classification
        ws_bounce_rate = (
            round((total_ws_bounces / total_ws_sent) * 100, 2)
            if total_ws_sent > 0
            else 0.0
        )
        ws_complaint_rate = (
            round((total_ws_complaints / total_ws_sent) * 100, 2)
            if total_ws_sent > 0
            else 0.0
        )
        ws_failure_rate = (
            round((total_ws_failures / (total_ws_sent + total_ws_failures)) * 100, 2)
            if (total_ws_sent + total_ws_failures) > 0
            else 0.0
        )

        overall_health = "HEALTHY"
        if any(w["level"] == "CRITICAL" for w in warnings):
            overall_health = "ACTION_REQUIRED"
        elif any(w["level"] == "WARNING" for w in warnings):
            overall_health = "NEEDS_ATTENTION"

        return {
            "workspace_id": workspace_id,
            "overall_health": overall_health,
            "total_sent": total_ws_sent,
            "bounce_rate": ws_bounce_rate,
            "complaint_rate": ws_complaint_rate,
            "failure_rate": ws_failure_rate,
            "active_safety_holds": len(holds),
            "warnings": warnings,
            "mailboxes": mailbox_items,
            "failure_breakdown": failure_breakdown,
        }
