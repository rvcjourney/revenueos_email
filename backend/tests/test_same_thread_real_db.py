"""Follow-ups in the same thread (ADR-0021 / migration 0048) against a REAL
PostgreSQL (pgserver).

Proves, with the real CHECKs, column grants, guard triggers and foreign keys:
* an EMAIL step needs a subject unless it is threaded; a WAIT is never threaded;
* who may write the new columns (app_api the flag, app_worker_general the parent);
* the flag of a step leads already reached cannot change while paused, and nothing
  changes while running;
* the parent link is immutable once rendered and can only point at a message of
  the same workspace and the same mailbox;
* progression stores the parent and the "Re:" subject, and the send worker reads
  the reply headers and the Gmail thread back;
* erasure and every delete command still work with threaded messages present.
No email is sent and nothing touches the production-linked Supabase project.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.errors import AppError
from app.db.context import set_transaction_context
from app.modules.campaigns.pacing import MailboxPacing
from app.modules.campaigns.progression import ProgressionService
from app.modules.campaigns.schemas import (
    SequenceStepCreateIn,
    SequenceStepReorderEntry,
    SequenceStepsReorderIn,
)
from app.modules.campaigns.worker_repository import CampaignWorkerRepository
from app.modules.sending.repository import SendingRepository
from tests.support.pause_support import add_message, draft_world, enroll, world
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, _insert, seed_world
from tests.test_campaign_paused_edits_real_db import edit, freeze, step_row, update_step
from tests.test_erasure_real_db import archive, call, count, scalar, sent_world
from tests.test_hard_delete_real_db import age_attempts, disconnect

pytestmark = pytest.mark.filterwarnings("ignore")


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
def producer(monkeypatch):
    monkeypatch.setattr("app.services.task_dispatch.get_task_producer", lambda *a, **k: None)


def replica(su, sql: str, params: list) -> None:
    su.execute("SET session_replication_role = replica")
    su.execute(sql, params)
    su.execute("SET session_replication_role = origin")


def thread_step2(su, w: World) -> None:
    """Make step 2 threaded the way a DRAFT edit would have, before activation."""
    replica(su, "UPDATE public.sequence_steps SET email_same_thread=true, email_subject=NULL WHERE id=%s", [w.step2])


def sent_parent(su, w: World, i: int = 0, *, rfc: str | None = "<parent@acme.test>", thread: str | None = "gm-thread-1"):
    """Lead i's first email, SENT, with its Message-ID and an ACCEPTED attempt."""
    at = datetime.now(UTC) - timedelta(days=3)
    message = add_message(su, w, i, status="SENT", due_at=at, anchor_at=at)
    replica(su, "UPDATE public.messages SET rfc_message_id=%s WHERE id=%s", [rfc, message])
    replica(
        su,
        """INSERT INTO public.message_attempts
           (id, workspace_id, message_id, mailbox_id, ordinal, invocation_owner, credential_generation,
            authorization_deadline, dispatch_generation, evidence_state, started_at, completed_at,
            provider_request_id, provider_thread_ref)
           VALUES (%s,%s,%s,%s,1,'t',1,%s,1,'ACCEPTED',%s,%s,'req-1',%s)""",
        [uuid.uuid4(), w.ws, message, w.mailbox_id, at + timedelta(minutes=5), at, at, thread],
    )
    return message


def progress(session_factory, w: World, now: datetime) -> None:
    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        ProgressionService(
            CampaignWorkerRepository(session),
            is_suppressed=lambda ws, address_id: False,
            default_pacing=MailboxPacing(daily_cap=50, min_spacing_seconds=60),
        ).advance_batch(workspace_id=w.ws, campaign_id=w.campaign_id, limit=100, now=now)
        session.commit()
    finally:
        session.close()


def follow_up_of(su, enrollment, step) -> dict:
    cur = su.execute(
        "SELECT id, reply_to_message_id, content_subject, mailbox_id, status FROM public.messages "
        "WHERE enrollment_id=%s AND step_id=%s",
        [enrollment, step],
    )
    return dict(zip([d.name for d in cur.description], cur.fetchone(), strict=True))


# ---------------------------------------------------------------------------
# columns, CHECKs and grants
# ---------------------------------------------------------------------------


def test_an_email_step_needs_a_subject_unless_it_is_threaded(su) -> None:
    w = draft_world(su)
    base = dict(workspace_id=w.ws, sequence_id=w.sequence_id, campaign_id=w.campaign_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(su, "sequence_steps", id=uuid.uuid4(), position=4, kind="EMAIL", email_body_html="<p>x</p>", **base)
    _insert(su, "sequence_steps", id=uuid.uuid4(), position=4, kind="EMAIL", email_body_html="<p>x</p>",
            email_same_thread=True, **base)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(su, "sequence_steps", id=uuid.uuid4(), position=5, kind="WAIT", wait_duration_minutes=60,
                email_same_thread=True, **base)
    assert scalar(su, "SELECT email_same_thread FROM public.sequence_steps WHERE id=%s", w.step1) is False


@pytest.mark.parametrize(
    ("role", "table", "column", "privilege", "allowed"),
    [
        ("app_api", "sequence_steps", "email_same_thread", "INSERT", True),
        ("app_api", "sequence_steps", "email_same_thread", "UPDATE", True),
        ("app_worker_general", "sequence_steps", "email_same_thread", "UPDATE", False),
        ("app_worker_general", "sequence_steps", "email_same_thread", "SELECT", True),
        ("app_worker_send", "sequence_steps", "email_same_thread", "SELECT", True),
        ("app_worker_general", "messages", "reply_to_message_id", "INSERT", True),
        ("app_worker_general", "messages", "reply_to_message_id", "UPDATE", False),
        ("app_worker_send", "messages", "reply_to_message_id", "SELECT", True),
        ("app_worker_send", "messages", "reply_to_message_id", "UPDATE", False),
        ("app_scheduler", "messages", "reply_to_message_id", "UPDATE", False),
        ("app_worker_sync", "messages", "reply_to_message_id", "UPDATE", False),
        ("app_api", "messages", "reply_to_message_id", "INSERT", False),
        ("app_api", "messages", "reply_to_message_id", "SELECT", True),
    ],
)
def test_column_grants(su, role, table, column, privilege, allowed) -> None:
    assert scalar(su, "SELECT has_column_privilege(%s, %s, %s, %s)", role, f"public.{table}", column, privilege) is allowed


# ---------------------------------------------------------------------------
# the editor's rules, through the real service and database
# ---------------------------------------------------------------------------


def test_a_draft_follow_up_can_be_threaded_and_then_not_moved_first(session_factory, su, producer) -> None:
    w = draft_world(su)
    out = update_step(session_factory, su, w, w.step2, email_same_thread=True)
    assert out.email_same_thread is True
    with pytest.raises(AppError) as info:
        edit(session_factory, w, "MANAGER", lambda svc, ctx: svc.reorder_steps(ctx, w.campaign_id, SequenceStepsReorderIn(steps=[
            SequenceStepReorderEntry(step_id=w.step2, position=1),
            SequenceStepReorderEntry(step_id=w.wait, position=2),
            SequenceStepReorderEntry(step_id=w.step1, position=3),
        ])))
    assert info.value.code == "same_thread_requires_previous_email"
    assert step_row(su, w.step2)["position"] == 3  # rolled back


def test_personalized_campaigns_accept_the_flag(session_factory, su, producer) -> None:
    w = seed_world(su, running=False, enroll_first=False, members=1)
    out = edit(session_factory, w, "MANAGER", lambda svc, ctx: svc.add_step(ctx, w.campaign_id, SequenceStepCreateIn(
        kind="EMAIL", position=4, email_body_html="<p>x</p>", email_same_thread=True, leading_wait_minutes=60,
    )))
    assert out.email_same_thread is True and out.email_subject is None
    assert count(su, "sequence_steps", "sequence_id=%s", w.sequence_id) == 5


# ---------------------------------------------------------------------------
# app_guard_frozen_sequence_steps (0048)
# ---------------------------------------------------------------------------


def test_paused_flag_change_only_after_the_frontier(session_factory, su, producer) -> None:
    w = world(su, status="PAUSED")
    freeze(su, w)
    add_message(su, w, 0, due_at=datetime.now(UTC) + timedelta(days=1))  # frontier = 1
    out = update_step(session_factory, su, w, w.step2, email_same_thread=True)
    assert out.email_same_thread is True
    with pytest.raises(psycopg.errors.CheckViolation, match="reached"):
        su.execute("UPDATE public.sequence_steps SET email_same_thread=true WHERE id=%s", [w.step1])
    # content of a reached step is still free (ADR-0020)
    su.execute("UPDATE public.sequence_steps SET email_body_html='<p>new</p>' WHERE id=%s", [w.step1])


def test_running_campaign_flag_is_immutable(su) -> None:
    w = world(su, status="RUNNING")
    freeze(su, w)
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        su.execute("UPDATE public.sequence_steps SET email_same_thread=true WHERE id=%s", [w.step2])


# ---------------------------------------------------------------------------
# progression -> message -> send context
# ---------------------------------------------------------------------------


def test_progression_threads_the_follow_up_and_the_sender_reads_it_back(session_factory, su) -> None:
    w = world(su)
    freeze(su, w)
    thread_step2(su, w)
    parent = sent_parent(su, w)
    enrollment = scalar(su, "SELECT enrollment_id FROM public.messages WHERE id=%s", parent)
    parent_subject = scalar(su, "SELECT content_subject FROM public.messages WHERE id=%s", parent)

    progress(session_factory, w, datetime.now(UTC))

    child = follow_up_of(su, enrollment, w.step2)
    assert child["reply_to_message_id"] == parent
    assert child["content_subject"] == f"Re: {parent_subject}"

    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        ctx = SendingRepository(session).load_message_for_send(workspace_id=w.ws, message_id=child["id"])
    finally:
        session.rollback()
        session.close()
    assert ctx is not None
    assert ctx.reply_to_message_id == parent
    assert ctx.thread_in_reply_to == "<parent@acme.test>"
    assert ctx.thread_references == ("<parent@acme.test>",)
    assert ctx.thread_provider_id == "gm-thread-1"


def _child_of(su, w: World, parent, step, *, rfc: str | None) -> uuid.UUID:
    """A message of the same enrollment for `step`, linked to `parent` at insert."""
    child = uuid.uuid4()
    replica(
        su,
        "INSERT INTO public.messages (id, workspace_id, purpose, campaign_id, enrollment_id, sequence_id, step_id, "
        "mailbox_id, address_id, status, reply_to_message_id, rfc_message_id) "
        "SELECT %s, workspace_id, purpose, campaign_id, enrollment_id, sequence_id, %s, mailbox_id, address_id, "
        "'PLANNED', id, %s FROM public.messages WHERE id=%s",
        [child, step, rfc, parent],
    )
    return child


def test_references_walk_every_ancestor_oldest_first(session_factory, su) -> None:
    w = world(su)
    step3 = uuid.uuid4()
    replica(
        su,
        "INSERT INTO public.sequence_steps (id, workspace_id, sequence_id, campaign_id, position, kind, "
        "wait_duration_minutes) VALUES (%s,%s,%s,%s,4,'WAIT',60)",
        [uuid.uuid4(), w.ws, w.sequence_id, w.campaign_id],
    )
    replica(
        su,
        "INSERT INTO public.sequence_steps (id, workspace_id, sequence_id, campaign_id, position, kind, "
        "email_body_html, email_same_thread) VALUES (%s,%s,%s,%s,5,'EMAIL','<p>3</p>',true)",
        [step3, w.ws, w.sequence_id, w.campaign_id],
    )
    freeze(su, w)
    first = sent_parent(su, w, rfc="<one@acme.test>", thread="t-1")
    second = _child_of(su, w, first, w.step2, rfc="<two@acme.test>")
    third = _child_of(su, w, second, step3, rfc=None)
    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        ctx = SendingRepository(session).load_message_for_send(workspace_id=w.ws, message_id=third)
    finally:
        session.rollback()
        session.close()
    assert ctx.thread_in_reply_to == "<two@acme.test>"
    assert ctx.thread_references == ("<one@acme.test>", "<two@acme.test>")
    assert ctx.thread_provider_id is None  # `second` has no accepted attempt


def test_a_parent_without_a_message_id_gives_no_reply_headers(session_factory, su) -> None:
    w = world(su)
    freeze(su, w)
    thread_step2(su, w)
    parent = sent_parent(su, w, rfc=None, thread=None)
    enrollment = scalar(su, "SELECT enrollment_id FROM public.messages WHERE id=%s", parent)
    progress(session_factory, w, datetime.now(UTC))
    child = follow_up_of(su, enrollment, w.step2)
    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        ctx = SendingRepository(session).load_message_for_send(workspace_id=w.ws, message_id=child["id"])
    finally:
        session.rollback()
        session.close()
    assert ctx.reply_to_message_id == parent
    assert (ctx.thread_in_reply_to, ctx.thread_references, ctx.thread_provider_id) == (None, (), None)


# ---------------------------------------------------------------------------
# the parent link: immutable, same workspace, same mailbox
# ---------------------------------------------------------------------------


def _threaded_child(session_factory, su) -> tuple[World, uuid.UUID, dict]:
    w = world(su)
    freeze(su, w)
    thread_step2(su, w)
    parent = sent_parent(su, w)
    enrollment = scalar(su, "SELECT enrollment_id FROM public.messages WHERE id=%s", parent)
    progress(session_factory, w, datetime.now(UTC))
    return w, parent, follow_up_of(su, enrollment, w.step2)


def test_the_parent_is_immutable_once_rendered(session_factory, su) -> None:
    _, _, child = _threaded_child(session_factory, su)
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        su.execute("UPDATE public.messages SET reply_to_message_id=NULL WHERE id=%s", [child["id"]])


def test_workers_cannot_rewrite_the_parent(session_factory, su, uri) -> None:
    w, _, child = _threaded_child(session_factory, su)
    with psycopg.connect(uri) as conn:
        conn.execute("SET LOCAL ROLE app_worker_general")
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", [str(w.ws)])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE public.messages SET reply_to_message_id=NULL WHERE id=%s", [child["id"]])


def _planned_message(su, w: World, i: int, *, mailbox=None) -> uuid.UUID:
    enrollment = enroll(su, w, i)
    message = uuid.uuid4()
    replica(
        su,
        "INSERT INTO public.messages (id, workspace_id, purpose, campaign_id, enrollment_id, sequence_id, step_id, "
        "mailbox_id, address_id, status) VALUES (%s,%s,'CAMPAIGN',%s,%s,%s,%s,%s,%s,'PLANNED')",
        [message, w.ws, w.campaign_id, enrollment, w.sequence_id, w.step2, mailbox or w.mailbox_id, w.address_ids[i]],
    )
    return message


def test_the_parent_must_share_the_mailbox(su) -> None:
    w = world(su)
    parent = sent_parent(su, w)
    other_mailbox = uuid.uuid4()
    replica(su, "INSERT INTO public.mailboxes (id, workspace_id, provider, original_address) VALUES (%s,%s,'SMTP','b@acme.test')",
            [other_mailbox, w.ws])
    child = _planned_message(su, w, 1, mailbox=other_mailbox)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        su.execute("UPDATE public.messages SET reply_to_message_id=%s WHERE id=%s", [parent, child])


def test_the_parent_must_be_in_the_same_workspace(su) -> None:
    mine, theirs = world(su), world(su)
    parent = sent_parent(su, mine)
    child = _planned_message(su, theirs, 0)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        su.execute("UPDATE public.messages SET reply_to_message_id=%s WHERE id=%s", [parent, child])


def test_a_message_cannot_reply_to_itself(su) -> None:
    w = world(su)
    child = _planned_message(su, w, 0)
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute("UPDATE public.messages SET reply_to_message_id=id WHERE id=%s", [child])


def test_a_parent_cannot_be_deleted_from_under_its_reply(su) -> None:
    w = world(su)
    parent = sent_parent(su, w)
    child = _planned_message(su, w, 1)
    su.execute("UPDATE public.messages SET reply_to_message_id=%s WHERE id=%s", [parent, child])
    su.execute("DELETE FROM public.message_attempts WHERE message_id=%s", [parent])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        su.execute("DELETE FROM public.messages WHERE id=%s", [parent])


# ---------------------------------------------------------------------------
# erasure and deletes keep working with threaded messages present
# ---------------------------------------------------------------------------


def threaded_sent_world(su, *, members: int = 2) -> World:
    """sent_world, with its follow-up linked to the first email and rendered."""
    w = sent_world(su, members=members)
    su.execute("UPDATE public.messages SET reply_to_message_id=%s WHERE id=%s", [w.message1_id, w.follow_up_id])  # type: ignore[attr-defined]
    replica(
        su,
        """UPDATE public.messages SET status='SCHEDULED', rendered_at=now(), due_at=now()+interval '1 day',
                  anchor_at=now(), content_subject='Re: Quick idea for Target0', content_body_html='<p>bump</p>',
                  content_digest=%s, frozen_destination='lead0@target0.test', frozen_sender_address='sender@acme.test'
           WHERE id=%s""",
        ["b" * 64, w.follow_up_id],  # type: ignore[attr-defined]
    )
    return w


def test_erasing_a_lead_redacts_a_threaded_follow_up(uri, su) -> None:
    w = threaded_sent_world(su)
    call(uri, "app_erase_lead", w, w.lead_ids[0])
    row = su.execute("SELECT status, content_subject, reply_to_message_id FROM public.messages WHERE id=%s",
                     [w.follow_up_id]).fetchone()  # type: ignore[attr-defined]
    assert row == ("SKIPPED", "[erased]", w.message1_id)


def test_erasing_a_campaign_with_threaded_messages(uri, su) -> None:
    w = threaded_sent_world(su)
    archive(su, w)
    call(uri, "app_erase_campaign", w, w.campaign_id)
    assert scalar(su, "SELECT content_subject FROM public.messages WHERE id=%s", w.follow_up_id) == "[erased]"  # type: ignore[attr-defined]


def test_hard_deleting_a_campaign_removes_parent_and_reply_together(uri, su) -> None:
    w = threaded_sent_world(su)
    replica(su, "UPDATE public.messages SET status='CANCELLED', terminal_reason='archived' WHERE id=%s", [w.follow_up_id])  # type: ignore[attr-defined]
    archive(su, w)
    age_attempts(su, w)
    out = call(uri, "app_delete_campaign", w, w.campaign_id)
    assert out["deleted"]["messages"] == 2
    assert count(su, "messages", "workspace_id=%s", w.ws) == 0


def test_hard_deleting_a_mailbox_removes_parent_and_reply_together(uri, su) -> None:
    w = threaded_sent_world(su)
    replica(su, "UPDATE public.messages SET status='CANCELLED', terminal_reason='archived' WHERE id=%s", [w.follow_up_id])  # type: ignore[attr-defined]
    archive(su, w)
    age_attempts(su, w)
    disconnect(su, w)
    out = call(uri, "app_delete_mailbox", w, w.mailbox_id)
    assert out["deleted"]["messages"] == 2


def test_deleting_the_workspace_with_threaded_messages(uri, su) -> None:
    w = threaded_sent_world(su)
    freeze(su, w)
    call(uri, "app_delete_workspace", w, "Acme Workspace")
    assert count(su, "messages", "workspace_id=%s", w.ws) == 0
    assert count(su, "workspaces", "id=%s", w.ws) == 0
