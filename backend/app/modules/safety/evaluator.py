"""Automatic protection: hold a mailbox whose bounce rate has become dangerous.

Every few minutes reply sync visits each connected mailbox and is where bounces
(delivery failure notices) are first seen, so this runs at the end of that visit.
If the mailbox's bounce rate over the last seven days is critical (see
thresholds.py) a safety hold is placed on that mailbox alone. The send path
already refuses to send while a mailbox has an active hold, so nothing else is
needed to stop it. Nothing here releases a hold: a person does that, after looking
at why the list bounced.

Only bounces are acted on. Spam complaints need a feedback loop that no provider
integration provides yet, so there is nothing to measure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.context import enter_worker_scope
from app.modules.safety.thresholds import Level, bounce_level, rate_percent

logger = logging.getLogger(__name__)

EVALUATION_WINDOW = timedelta(days=7)
AUTO_BOUNCE_PREFIX = "auto-bounce:"


@dataclass(frozen=True)
class SafetyEvaluation:
    sent: int
    bounced: int
    bounce_rate: float
    level: Level | None
    # True when this evaluation placed a new hold.
    held: bool


def auto_bounce_identity(mailbox_id: UUID, when: datetime) -> str:
    """One hold per mailbox per UTC day: a repeat the same day changes nothing."""
    return f"{AUTO_BOUNCE_PREFIX}{mailbox_id}:{when.astimezone(UTC):%Y-%m-%d}"


def hold_reason(bounced: int, sent: int, rate: float) -> str:
    return (
        f"Bounce rate {rate}% over the last 7 days ({bounced} of {sent} emails). "
        "Sending from this mailbox is paused until a person releases it."
    )


class MailboxSafetyEvaluator:
    def __init__(self, session: Session) -> None:
        # Imported here because reply sync imports this module: a module-level
        # import would be circular.
        from app.modules.replies.repository import ReplyRepository

        self.session = session
        self.replies = ReplyRepository(session)

    def _counts(
        self, *, workspace_id: UUID, mailbox_id: UUID, since: datetime
    ) -> tuple[int, int]:
        """Emails accepted since `since`, and how many of them bounced.

        A bounce is a BOUNCED event on a SENT message, the same definition the
        deliverability dashboard uses.
        """
        enter_worker_scope(
            self.session, workspace_id=workspace_id, role_name="app_worker_general"
        )
        row = (
            self.session.execute(
                text(
                    """
                    SELECT COUNT(*) AS sent,
                           COUNT(CASE WHEN b.id IS NOT NULL THEN 1 END) AS bounced
                    FROM public.messages m
                    LEFT JOIN public.message_events b
                      ON b.workspace_id = m.workspace_id AND b.message_id = m.id
                     AND b.kind = 'BOUNCED'
                    WHERE m.workspace_id = :ws AND m.mailbox_id = :mid
                      AND m.status = 'SENT' AND m.accepted_at >= :since
                    """
                ),
                {"ws": str(workspace_id), "mid": str(mailbox_id), "since": since},
            )
            .mappings()
            .one()
        )
        return int(row["sent"] or 0), int(row["bounced"] or 0)

    def _already_held(self, *, workspace_id: UUID, mailbox_id: UUID) -> bool:
        """An earlier automatic hold on this mailbox is still waiting for a person."""
        enter_worker_scope(
            self.session, workspace_id=workspace_id, role_name="app_worker_sync"
        )
        found = self.session.execute(
            text(
                """
                SELECT 1 FROM public.safety_holds
                WHERE workspace_id = :ws AND target_mailbox_id = :mid
                  AND status = 'ACTIVE' AND source_work_identity LIKE :prefix
                LIMIT 1
                """
            ),
            {
                "ws": str(workspace_id),
                "mid": str(mailbox_id),
                "prefix": f"{AUTO_BOUNCE_PREFIX}{mailbox_id}:%",
            },
        ).first()
        return found is not None

    def evaluate(
        self,
        *,
        workspace_id: UUID,
        mailbox_id: UUID,
        now: datetime | None = None,
    ) -> SafetyEvaluation:
        """Look at one mailbox and hold it if its bounce rate is critical.

        Runs in the caller's transaction; the caller commits.
        """
        now = now or datetime.now(UTC)
        sent, bounced = self._counts(
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            since=now - EVALUATION_WINDOW,
        )
        rate = rate_percent(bounced, sent)
        level = bounce_level(rate, sent)
        if level != "CRITICAL":
            return SafetyEvaluation(sent, bounced, rate, level, held=False)

        if self._already_held(workspace_id=workspace_id, mailbox_id=mailbox_id):
            return SafetyEvaluation(sent, bounced, rate, level, held=False)

        inserted = self.replies.insert_safety_hold(
            workspace_id=workspace_id,
            mailbox_id=mailbox_id,
            source_identity=auto_bounce_identity(mailbox_id, now),
            reason=hold_reason(bounced, sent, rate),
        )
        if inserted:
            logger.warning(
                "mailbox_auto_held",
                extra={
                    "workspace_id": str(workspace_id),
                    "mailbox_id": str(mailbox_id),
                    "bounce_rate": rate,
                    "sent": sent,
                    "bounced": bounced,
                },
            )
        return SafetyEvaluation(sent, bounced, rate, level, held=bool(inserted))
