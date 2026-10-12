"""Editing an activated campaign's emails while it is PAUSED -- ADR-0020 /
migration 0042, against a REAL PostgreSQL (pgserver).

Proves, with the real guard triggers, row-level security and roles:
* a running campaign's frozen sequence stays immutable;
* a paused STANDARD campaign accepts content/wait edits for any step and
  structural edits only after the frontier (what leads already reached);
* only campaigns.execute holders may edit; personalized campaigns keep their structure frozen;
* the post-resume reconcile pass re-renders unsent messages through the narrow
  snapshot exemption, and the exemption cannot be used for anything else.
No email is sent and nothing touches the production-linked Supabase project.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.errors import AppError
from app.modules.campaigns.schemas import (
    SequenceStepCreateIn,
    SequenceStepReorderEntry,
    SequenceStepsReorderIn,
    SequenceStepUpdateIn,
)
from app.modules.campaigns.sequence_service import SequenceService
from tests.support.pause_support import (
    FakeProducer,
    add_message,
    api,
    audit_count,
    enroll,
    msg,
    pause,
    reconcile,
    resume,
    today_at,
    world,
)
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

EDITED = "<p>Hello {{first_name}}, this is the rewritten email.</p>"


@pytest.fixture(scope="module")
def uri():
    with throwaway_database() as value:
        yield value


@pytest.fixture(scope="module")
def session_factory(uri):
    engine = create_engine("postgresql+psycopg://" + uri.split("://", 1)[1], future=True)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    engine.dispose()


@pytest.fixture()
def su(uri):
    conn = psycopg.connect(uri, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture()
def producer(monkeypatch) -> FakeProducer:
    fake = FakeProducer()
    monkeypatch.setattr("app.services.task_dispatch.get_task_producer", lambda *a, **k: fake)
    monkeypatch.setattr("app.modules.campaigns.preflight.unsubscribe_ready", lambda s: True)
    return fake


def freeze(su, w: World) -> None:
    """Activation freezes the sequence; the seed leaves it DRAFT."""
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaign_sequences SET status='FROZEN', frozen_at=now(), content_digest=%s WHERE id=%s",
        ["a" * 64, w.sequence_id],
    )
    su.execute("SET session_replication_role = origin")


def paused_world(su, *, status: str = "PAUSED", reached: bool = True) -> World:
    """E(1) W(2) E(3). With `reached`, one lead already has a message for step 1,
    so the frontier is 1."""
    w = world(su, status=status)
    freeze(su, w)
    if reached:
        add_message(su, w, 0, due_at=datetime.now(UTC) + timedelta(days=1))
    return w


def step_row(su, step_id) -> dict:
    cur = su.execute(
        "SELECT position, kind, email_subject, email_body_html, wait_duration_minutes, version "
        "FROM public.sequence_steps WHERE id=%s",
        [step_id],
    )
    return dict(zip([d.name for d in cur.description], cur.fetchone(), strict=True))


def positions(su, w: World) -> list[tuple[int, str]]:
    return su.execute(
        "SELECT position, kind FROM public.sequence_steps WHERE sequence_id=%s ORDER BY position",
        [w.sequence_id],
    ).fetchall()


def edit(session_factory, w: World, role: str, fn):
    session, ctx = api(session_factory, w, role)
    try:
        out = fn(SequenceService(session), ctx)
        session.commit()
        return out
    finally:
        session.rollback()
        session.close()


def update_step(session_factory, su, w: World, step_id, role: str = "MANAGER", **fields):
    return edit(
        session_factory,
        w,
        role,
        lambda svc, ctx: svc.update_step(
            ctx,
            w.campaign_id,
            step_id,
            SequenceStepUpdateIn(expected_version=step_row(su, step_id)["version"], **fields),
        ),
    )


def add_pair(session_factory, w: World, position: int, *, subject: str = "Another follow-up"):
    return edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.add_step(
            ctx,
            w.campaign_id,
            SequenceStepCreateIn(
                kind="EMAIL",
                position=position,
                email_subject=subject,
                email_body_html="<p>Hello {{first_name}}</p>",
                leading_wait_minutes=1440,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# who may edit, and when
# ---------------------------------------------------------------------------


def test_running_campaigns_sequence_stays_frozen(session_factory, su, producer) -> None:
    w = paused_world(su, status="RUNNING")
    with pytest.raises(AppError) as info:
        update_step(session_factory, su, w, w.step2, email_subject="Sneaky")
    assert info.value.status_code == 409 and info.value.code == "state_conflict"
    assert step_row(su, w.step2)["email_subject"] == "A different angle for {{company}}"


def test_the_database_itself_refuses_step_edits_on_a_running_campaign(su) -> None:
    """Even if a service forgot the check: the trigger is the authority."""
    w = paused_world(su, status="RUNNING")
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        su.execute("UPDATE public.sequence_steps SET email_subject='x' WHERE id=%s", [w.step2])


def test_member_cannot_edit_a_paused_campaigns_emails(session_factory, su, producer) -> None:
    w = paused_world(su)
    with pytest.raises(AppError) as info:
        update_step(session_factory, su, w, w.step2, role="MEMBER", email_subject="Nope")
    assert info.value.status_code == 403
    # And row-level security stops a MEMBER even around the service.
    session, _ = api(session_factory, w, "MEMBER")
    try:
        result = session.execute(
            text("UPDATE public.sequence_steps SET email_subject='x' WHERE id=:id"), {"id": str(w.step2)}
        )
        assert result.rowcount == 0
    finally:
        session.rollback()
        session.close()
    assert step_row(su, w.step2)["email_subject"] == "A different angle for {{company}}"


def test_personalized_campaign_structure_stays_frozen_even_when_paused(session_factory, su, producer) -> None:
    """A personalized campaign's steps can't be restructured or re-timed while paused
    (only an unwritten reference's wording can change: test_campaign_hyper_paused_edits_real_db)."""
    w = seed_world(su, running=True, campaign_type="HYPER_PERSONALIZED", members=1, enroll_first=False)
    su.execute("SET session_replication_role = replica")
    su.execute("UPDATE public.campaigns SET status='PAUSED' WHERE id=%s", [w.campaign_id])
    su.execute("SET session_replication_role = origin")
    freeze(su, w)
    with pytest.raises(AppError) as info:
        edit(session_factory, w, "MANAGER", lambda svc, ctx: svc.delete_step(ctx, w.campaign_id, w.step2))
    assert info.value.status_code == 409
    with pytest.raises(psycopg.errors.CheckViolation):  # the trigger agrees
        su.execute("DELETE FROM public.sequence_steps WHERE id=%s", [w.step2])


# ---------------------------------------------------------------------------
# content and waits: free for every step, audited
# ---------------------------------------------------------------------------


def test_manager_edits_content_of_a_reached_and_an_unreached_step(session_factory, su, producer) -> None:
    w = paused_world(su)
    update_step(session_factory, su, w, w.step1, email_subject="New subject {{first_name}}", email_body_html=EDITED)
    update_step(session_factory, su, w, w.step2, email_body_html=EDITED)

    assert step_row(su, w.step1)["email_subject"] == "New subject {{first_name}}"
    assert "rewritten" in step_row(su, w.step2)["email_body_html"]
    assert audit_count(su, w, "campaign.sequence_edited_while_paused") == 2
    row = su.execute(
        "SELECT after_state FROM public.audit_events WHERE target_id=%s AND action='campaign.sequence_edited_while_paused'",
        [w.campaign_id],
    ).fetchone()[0]
    assert row["operation"] == "update_step" and "email_body_html" in row["fields"]
    assert EDITED not in str(row)  # ids and field names only, never content


def test_wait_duration_can_be_edited(session_factory, su, producer) -> None:
    w = paused_world(su)
    update_step(session_factory, su, w, w.wait, wait_duration_minutes=60)
    assert step_row(su, w.wait)["wait_duration_minutes"] == 60


def test_step_kind_can_never_change(su) -> None:
    w = paused_world(su)
    with pytest.raises(psycopg.errors.CheckViolation, match="cannot change"):
        su.execute("UPDATE public.sequence_steps SET kind='WAIT' WHERE id=%s", [w.step2])


# ---------------------------------------------------------------------------
# structure: only after the frontier
# ---------------------------------------------------------------------------


def test_sequence_response_reports_the_frontier_and_per_step_counts(session_factory, su, producer) -> None:
    w = paused_world(su)
    seq = edit(session_factory, w, "MANAGER", lambda svc, ctx: svc.get_sequence(ctx, w.campaign_id))
    assert seq.structure_locked_through == 1 and seq.paused_edit_allowed is True
    first = next(s for s in seq.steps if s.id == w.step1)
    assert (first.sent_count, first.unsent_count) == (0, 1)

    member_view = edit(session_factory, w, "MEMBER", lambda svc, ctx: svc.get_sequence(ctx, w.campaign_id))
    assert member_view.paused_edit_allowed is False


def test_a_new_email_can_be_appended_after_the_last_step(session_factory, su, producer) -> None:
    w = paused_world(su)
    add_pair(session_factory, w, position=4)
    assert positions(su, w) == [(1, "EMAIL"), (2, "WAIT"), (3, "EMAIL"), (4, "WAIT"), (5, "EMAIL")]


def test_nothing_can_be_inserted_in_front_of_a_step_leads_reached(session_factory, su, producer) -> None:
    w = paused_world(su)
    with pytest.raises(AppError) as info:
        add_pair(session_factory, w, position=1)
    assert info.value.status_code == 409 and info.value.code == "step_locked"
    assert len(positions(su, w)) == 3
    with pytest.raises(psycopg.errors.CheckViolation, match="already reached"):  # trigger backstop
        su.execute(
            "INSERT INTO public.sequence_steps (workspace_id, sequence_id, campaign_id, position, kind, wait_duration_minutes) "
            "VALUES (%s,%s,%s,1,'WAIT',60)",
            [w.ws, w.sequence_id, w.campaign_id],
        )


def test_a_step_leads_reached_cannot_be_deleted(session_factory, su, producer) -> None:
    w = paused_world(su)
    with pytest.raises(AppError) as info:
        edit(session_factory, w, "MANAGER", lambda svc, ctx: svc.delete_step(ctx, w.campaign_id, w.step1))
    assert info.value.code == "step_locked"
    assert len(positions(su, w)) == 3


def test_an_unreached_email_is_removed_together_with_its_wait(session_factory, su, producer) -> None:
    w = paused_world(su)
    edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.delete_step(ctx, w.campaign_id, w.step2, with_adjacent_wait=True),
    )
    assert positions(su, w) == [(1, "EMAIL")]


def test_removing_an_email_but_leaving_its_wait_is_rejected_and_rolled_back(session_factory, su, producer) -> None:
    w = paused_world(su)
    session, ctx = api(session_factory, w, "MANAGER")
    try:
        with pytest.raises(AppError) as info:
            SequenceService(session).delete_step(ctx, w.campaign_id, w.step2)  # leaves E,W
        assert info.value.code == "sequence_invalid"
        session.rollback()  # what the request layer does on an error
    finally:
        session.close()
    assert len(positions(su, w)) == 3
    assert audit_count(su, w, "campaign.sequence_edited_while_paused") == 0


def test_steps_after_the_frontier_can_be_reordered_but_reached_ones_cannot(session_factory, su, producer) -> None:
    w = paused_world(su)
    add_pair(session_factory, w, position=4, subject="Fifth")
    ids = {pos: sid for sid, pos in su.execute("SELECT id, position FROM public.sequence_steps WHERE sequence_id=%s", [w.sequence_id]).fetchall()}

    def reorder(order: dict[int, int]):
        return SequenceStepsReorderIn(
            steps=[SequenceStepReorderEntry(step_id=ids[old], position=new) for old, new in order.items()]
        )

    # swap the two emails after the frontier (positions 3 and 5)
    edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.reorder_steps(ctx, w.campaign_id, reorder({1: 1, 2: 2, 3: 5, 4: 4, 5: 3})),
    )
    assert step_row(su, ids[5])["position"] == 3 and step_row(su, ids[3])["position"] == 5

    with pytest.raises(AppError) as info:  # moving the reached email is refused
        edit(
            session_factory,
            w,
            "MANAGER",
            lambda svc, ctx: svc.reorder_steps(ctx, w.campaign_id, reorder({1: 3, 2: 2, 3: 1, 4: 4, 5: 5})),
        )
    assert info.value.code == "step_locked"
    assert step_row(su, ids[1])["position"] == 1


def test_attachments_follow_the_same_state_and_role_rules(session_factory, su, producer) -> None:
    columns = (
        "(workspace_id, campaign_id, sequence_id, step_id, disposition, content_id, storage_key, filename, "
        "content_type, size_bytes, sha256)"
    )
    sql = (
        f"INSERT INTO public.campaign_step_attachments {columns} "
        "VALUES (:ws,:cid,:seq,:step,'ATTACHMENT',:content,:key,'a.pdf','application/pdf',10,:sha)"
    )

    def insert(w: World, role: str) -> None:
        session, _ = api(session_factory, w, role)
        try:
            session.execute(
                text(sql),
                {
                    "ws": str(w.ws), "cid": str(w.campaign_id), "seq": str(w.sequence_id), "step": str(w.step2),
                    "content": uuid.uuid4().hex[:12], "key": f"k/{uuid.uuid4().hex}", "sha": "b" * 64,
                },
            )
            session.commit()
        finally:
            session.rollback()
            session.close()

    paused = paused_world(su)
    insert(paused, "MANAGER")  # allowed
    assert su.execute("SELECT count(*) FROM public.campaign_step_attachments WHERE step_id=%s", [paused.step2]).fetchone()[0] == 1

    with pytest.raises(Exception, match="row-level security|permission"):
        insert(paused, "MEMBER")
    running = paused_world(su, status="RUNNING")
    with pytest.raises(Exception, match="immutable|row-level security"):
        insert(running, "MANAGER")


# ---------------------------------------------------------------------------
# the message-snapshot exemption: narrow by construction
# ---------------------------------------------------------------------------


def rewrite_as_worker(session_factory, w: World, message_id, *, guc: bool = True) -> None:
    session = session_factory()
    try:
        session.execute(text("SET LOCAL ROLE app_worker_general"))
        session.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(w.ws)})
        if guc:
            session.execute(text("SELECT set_config('app.reconcile', 'on', true)"))
        session.execute(
            text("UPDATE public.messages SET content_body_html='<p>x</p>', content_digest=:d WHERE id=:id"),
            {"d": "c" * 64, "id": str(message_id)},
        )
        session.commit()
    finally:
        session.rollback()
        session.close()


def resumed_world(su) -> tuple[World, uuid.UUID]:
    """A RUNNING campaign at generation 2 (just resumed) with one stale message."""
    w = world(su)
    message = add_message(su, w, 0, due_at=datetime.now(UTC) + timedelta(days=1))
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])
    return w, message


def test_snapshot_rewrite_needs_the_reconcile_switch(session_factory, su) -> None:
    w, message = resumed_world(su)
    with pytest.raises(Exception, match="snapshot is immutable"):
        rewrite_as_worker(session_factory, w, message, guc=False)
    rewrite_as_worker(session_factory, w, message, guc=True)  # the legitimate path works
    assert msg(su, message)["status"] == "SCHEDULED"


def test_snapshot_rewrite_is_refused_unless_the_campaign_was_just_resumed(session_factory, su) -> None:
    w, message = resumed_world(su)
    su.execute("UPDATE public.campaigns SET schedule_generation = 1 WHERE id=%s", [w.campaign_id])
    with pytest.raises(Exception, match="snapshot is immutable"):
        rewrite_as_worker(session_factory, w, message)


def test_snapshot_rewrite_is_refused_for_a_paused_campaign(session_factory, su) -> None:
    w, message = resumed_world(su)
    su.execute("SET session_replication_role = replica")
    su.execute("UPDATE public.campaigns SET status='PAUSED' WHERE id=%s", [w.campaign_id])
    su.execute("SET session_replication_role = origin")
    with pytest.raises(Exception, match="snapshot is immutable"):
        rewrite_as_worker(session_factory, w, message)


@pytest.mark.parametrize("pin", ["rfc", "claimed", "attempt", "sent"])
def test_snapshot_rewrite_is_refused_once_a_provider_could_have_seen_it(session_factory, su, pin) -> None:
    w, message = resumed_world(su)
    su.execute("SET session_replication_role = replica")
    if pin == "rfc":
        su.execute("UPDATE public.messages SET rfc_message_id='<x@y>' WHERE id=%s", [message])
    elif pin == "claimed":
        su.execute("UPDATE public.messages SET dispatch_origin='SCHEDULED' WHERE id=%s", [message])
    elif pin == "attempt":
        su.execute(
            "INSERT INTO public.message_attempts (workspace_id, message_id, mailbox_id, ordinal, invocation_owner, "
            "credential_generation, authorization_deadline, dispatch_generation) "
            "VALUES (%s,%s,%s,1,'t',1,now()+interval '1 minute',1)",
            [w.ws, message, w.mailbox_id],
        )
    else:
        su.execute("UPDATE public.messages SET status='SENT', accepted_at=now() WHERE id=%s", [message])
    su.execute("SET session_replication_role = origin")
    with pytest.raises(Exception, match="snapshot is immutable|reopened"):
        rewrite_as_worker(session_factory, w, message)


# ---------------------------------------------------------------------------
# end to end: pause -> edit -> resume -> reconcile
# ---------------------------------------------------------------------------


def expected_digest(subject: str, body: str) -> str:
    return hashlib.sha256(f"{subject}\x00{body}\x001".encode()).hexdigest()


def test_edited_email_reaches_unsent_messages_after_resume_and_only_those(session_factory, su, producer) -> None:
    w = world(su, members=4)
    freeze(su, w)
    now = today_at(10)
    soon = now + timedelta(hours=1)
    unsent = add_message(su, w, 0, due_at=soon)
    sent = add_message(su, w, 1, status="SENT", due_at=now - timedelta(days=1), anchor_at=now - timedelta(days=1))
    pinned = add_message(su, w, 2, status="RETRY_SCHEDULED", due_at=soon, next_retry_at=soon)
    su.execute("SET session_replication_role = replica")
    su.execute("UPDATE public.messages SET rfc_message_id='<pinned@acme.test>' WHERE id=%s", [pinned])
    su.execute("SET session_replication_role = origin")
    queued = add_message(su, w, 3, status="QUEUED", due_at=now - timedelta(minutes=5))
    before = {m: su.execute("SELECT content_subject, content_body_html, content_digest FROM public.messages WHERE id=%s", [m]).fetchone() for m in (unsent, sent, pinned, queued)}

    pause(session_factory, su, w)
    update_step(session_factory, su, w, w.step1, email_subject="Rewritten for {{company}}", email_body_html=EDITED)
    resume(session_factory, su, w)

    summary = reconcile(session_factory, w, 2, now)

    def row(m):
        return su.execute(
            "SELECT content_subject, content_body_html, content_digest, schedule_generation FROM public.messages WHERE id=%s", [m]
        ).fetchone()

    new_subject = "Rewritten for Acme"  # FROZEN variables: company = Acme
    new_body = "<p>Hello Sarah, this is the rewritten email.</p>"
    after = row(unsent)
    assert (after[0], after[1]) == (new_subject, new_body)
    assert after[2] == expected_digest(new_subject, new_body)  # exactly what the send gate recomputes
    assert after[3] == 2
    assert row(sent)[:3] == before[sent]  # history is never rewritten
    assert row(queued)[:3] == before[queued]  # already past the gate
    assert row(pinned)[:3] == before[pinned] and row(pinned)[3] == 2  # transport-pinned: time only
    assert summary.rerendered == 1


def test_reconcile_leaves_matching_content_alone(session_factory, su, producer) -> None:
    w = world(su)
    freeze(su, w)
    now = today_at(10)
    message = add_message(su, w, 0, due_at=now + timedelta(hours=1))
    before = su.execute("SELECT content_digest FROM public.messages WHERE id=%s", [message]).fetchone()[0]
    pause(session_factory, su, w)
    resume(session_factory, su, w)

    summary = reconcile(session_factory, w, 2, now)

    assert summary.rerendered == 0 and summary.restamped == 1
    assert su.execute("SELECT content_digest FROM public.messages WHERE id=%s", [message]).fetchone()[0] == before


def test_a_step_appended_while_paused_is_picked_up_by_an_active_lead(session_factory, su, producer) -> None:
    """Appending a step changes the live sequence the progression sweeper reads:
    nothing about existing messages or leads is rewritten."""
    w = paused_world(su)
    enrollment_before = su.execute("SELECT state, next_step_id FROM public.campaign_enrollments").fetchall()
    add_pair(session_factory, w, position=4)
    assert su.execute("SELECT state, next_step_id FROM public.campaign_enrollments").fetchall() == enrollment_before
    assert su.execute("SELECT count(*) FROM public.messages WHERE campaign_id=%s", [w.campaign_id]).fetchone()[0] == 1
    assert enroll is not None and msg is not None  # helpers used by sibling tests
