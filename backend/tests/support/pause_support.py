"""Shared helpers for the pause/resume real-database tests (ADR-0020).

Not a test module. Fixtures (`uri`, `session_factory`, `su`, `producer`) stay in
each test file so every file owns its own throwaway database.
"""

# ruff: noqa: E501
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.db.context import set_transaction_context
from app.modules.campaigns.activation_service import CampaignActivationService
from app.modules.campaigns.message_rendering import render_step_content
from app.modules.campaigns.pacing import MailboxPacing
from app.modules.campaigns.reconcile import ReconcileService
from app.modules.campaigns.schemas import PauseIn, ResumeIn
from app.modules.campaigns.worker_repository import CampaignWorkerRepository
from app.modules.scheduler.repository import SchedulerRepository
from tests.support.real_seed import (
    FOLLOW_UP_BODY,
    FROZEN,
    REF_BODY,
    REF_SUBJECT,
    World,
    _insert,
    seed_world,
)

DEFAULT_PACING = MailboxPacing(daily_cap=50, min_spacing_seconds=60)


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, dict]] = []

    def send_task(self, name: str, kwargs: dict | None = None, **options) -> None:
        self.sent.append((name, kwargs or {}, options))


def world(su, *, status: str = "RUNNING", members: int = 3) -> World:
    """A STANDARD campaign with one assigned mailbox, no enrollments."""
    w = seed_world(su, running=True, campaign_type="STANDARD", members=members, enroll_first=False, config={})
    su.execute("SET session_replication_role = replica")  # config is frozen after activation
    # A mailbox that can send: the scheduler no longer claims for one that cannot.
    su.execute(
        "UPDATE public.mailboxes SET connection_state='CONNECTED', health_state='HEALTHY', "
        "connected_generation=current_connection_generation, "
        "provider_account_id='acct-'||id::text WHERE id=%s",
        [w.mailbox_id],
    )
    su.execute(
        "INSERT INTO public.campaign_mailboxes (workspace_id, campaign_id, mailbox_id) VALUES (%s,%s,%s)",
        [w.ws, w.campaign_id, w.mailbox_id],
    )
    if status != "RUNNING":
        su.execute("UPDATE public.campaigns SET status=%s WHERE id=%s", [status, w.campaign_id])
    su.execute("SET session_replication_role = origin")
    return w


def force_status(su, w: World, status: str) -> None:
    """Put an activated campaign in `status`, satisfying the shape CHECKs."""
    su.execute("SET session_replication_role = replica")
    if status == "ARCHIVED":
        su.execute(
            "UPDATE public.campaigns SET status='ARCHIVED', previous_status='COMPLETED', "
            "archived_at=now() WHERE id=%s",
            [w.campaign_id],
        )
    else:
        su.execute("UPDATE public.campaigns SET status=%s WHERE id=%s", [status, w.campaign_id])
    su.execute("SET session_replication_role = origin")


def draft_world(su) -> World:
    w = seed_world(su, running=False, campaign_type="STANDARD", members=1, enroll_first=False, config={})
    su.execute(
        "INSERT INTO public.campaign_mailboxes (workspace_id, campaign_id, mailbox_id) VALUES (%s,%s,%s)",
        [w.ws, w.campaign_id, w.mailbox_id],
    )
    return w


def api(session_factory, w: World, role: str = "MANAGER") -> tuple[Session, WorkspaceContext]:
    user = {"OWNER": w.owner, "MANAGER": w.manager, "MEMBER": w.member}[role]
    session = session_factory()
    session.execute(text("SET LOCAL ROLE app_api"))
    set_transaction_context(session, user_id=user, workspace_id=w.ws)
    return session, WorkspaceContext(workspace_id=w.ws, user_id=user, role_code=role)


def camp(su, w: World) -> dict:
    cur = su.execute(
        "SELECT status, version, schedule_generation, paused_at, paused_total_seconds, last_resumed_at "
        "FROM public.campaigns WHERE id=%s",
        [w.campaign_id],
    )
    row = cur.fetchone()
    return dict(zip([d.name for d in cur.description], row, strict=True))


def pause(session_factory, su, w: World, *, expected: int | None = None):
    session, ctx = api(session_factory, w)
    try:
        out = CampaignActivationService(session).pause(
            ctx, w.campaign_id, PauseIn(expected_version=expected or camp(su, w)["version"])
        )
        session.commit()
        return out
    finally:
        session.close()


def resume(session_factory, su, w: World, *, expected: int | None = None):
    session, ctx = api(session_factory, w)
    try:
        out = CampaignActivationService(session).resume(
            ctx, w.campaign_id, ResumeIn(expected_version=expected or camp(su, w)["version"])
        )
        session.commit()
        return out
    finally:
        session.close()


def audit_count(su, w: World, action: str) -> int:
    return su.execute(
        "SELECT count(*) FROM public.audit_events WHERE target_id=%s AND action=%s",
        [w.campaign_id, action],
    ).fetchone()[0]


def enroll(su, w: World, i: int) -> uuid.UUID:
    """An ACTIVE enrollment for audience member i, pointing at step 1."""
    su.execute("SET session_replication_role = replica")
    enrollment = uuid.uuid4()
    _insert(
        su,
        "campaign_enrollments",
        id=enrollment,
        workspace_id=w.ws,
        campaign_id=w.campaign_id,
        audience_id=w.audience_id,
        audience_member_id=w.member_ids[i],
        sequence_id=w.sequence_id,
        lead_id=w.lead_ids[i],
        address_id=w.address_ids[i],
        frozen_destination=f"lead{i}@target{i}.test",
        state="ACTIVE",
        next_step_id=w.step1,
        next_sequence_position=1,
        assigned_mailbox_id=w.mailbox_id,
        frozen_variables=FROZEN,
    )
    su.execute("SET session_replication_role = origin")
    return enrollment


def add_message(
    su,
    w: World,
    i: int,
    *,
    enrollment: uuid.UUID | None = None,
    step: uuid.UUID | None = None,
    status: str = "SCHEDULED",
    due_at: datetime,
    anchor_at: datetime | None = None,
    generation: int = 1,
    next_retry_at: datetime | None = None,
) -> uuid.UUID:
    enrollment = enrollment or enroll(su, w, i)
    message = uuid.uuid4()
    extra: dict = {}
    if status in ("QUEUED",):
        extra.update(dispatch_origin="SCHEDULED", claim_expires_at=datetime.now(UTC) + timedelta(minutes=5))
    if status == "SENT":
        extra.update(accepted_at=anchor_at or due_at)
    if status == "RETRY_SCHEDULED":
        extra.update(retry_count=1, next_retry_at=next_retry_at or due_at)
    subject, body = (
        (REF_SUBJECT, REF_BODY)
        if (step or w.step1) == w.step1
        else ("A different angle for {{company}}", FOLLOW_UP_BODY)
    )
    # What the planner would have stored, so the digest matches the live step.
    rendered = render_step_content(
        subject=subject, body_html=body, frozen_variables=FROZEN, renderer_version=1
    )
    su.execute("SET session_replication_role = replica")
    _insert(
        su,
        "messages",
        id=message,
        workspace_id=w.ws,
        purpose="CAMPAIGN",
        campaign_id=w.campaign_id,
        enrollment_id=enrollment,
        sequence_id=w.sequence_id,
        step_id=step or w.step1,
        mailbox_id=w.mailbox_id,
        address_id=w.address_ids[i],
        status=status,
        content_subject=rendered.subject,
        content_body_html=rendered.body_html,
        content_digest=rendered.content_digest,
        rendered_at=datetime.now(UTC) - timedelta(days=3),
        frozen_destination=f"lead{i}@target{i}.test",
        frozen_sender_address="sender@acme.test",
        due_at=due_at,
        anchor_at=anchor_at or due_at,
        schedule_generation=generation,
        **extra,
    )
    su.execute("SET session_replication_role = origin")
    return message


def msg(su, message_id: uuid.UUID) -> dict:
    cur = su.execute(
        "SELECT status, due_at, schedule_generation, dispatch_generation FROM public.messages WHERE id=%s",
        [message_id],
    )
    return dict(zip([d.name for d in cur.description], cur.fetchone(), strict=True))


def reconcile(session_factory, w: World, generation: int, now: datetime, *, pacing: MailboxPacing | None = None, **kwargs):
    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        service = ReconcileService(CampaignWorkerRepository(session), default_pacing=pacing or DEFAULT_PACING)
        summary = service.reconcile_batch(
            workspace_id=w.ws, campaign_id=w.campaign_id, generation=generation, limit=500, now=now, **kwargs
        )
        session.commit()
        return summary
    finally:
        session.close()


def discoverable(session_factory, w: World, at: datetime) -> set[uuid.UUID]:
    session = session_factory()
    try:
        found = SchedulerRepository(session).find_due_messages(limit=500, authoritative_now=at)
        return {c.id for c in found if c.workspace_id == w.ws}
    finally:
        session.rollback()
        session.close()


def today_at(hour: int) -> datetime:
    return datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)


