"""Same-thread follow-ups of a personalized campaign (ADR-0021 slice 2) against a
REAL PostgreSQL (pgserver), with the real roles, grants, RLS and guards.

Proves: progression inserts the PLANNED follow-up with its parent as
app_worker_general; the just-in-time writer (fake model, app_worker_general)
writes the "Re:" subject through the snapshot guard; the send loader reads the
parent chain back; the "no subject" audit row can be written by the writer's role.
No email is sent, no model is called, nothing touches the production-linked project.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.context import set_transaction_context
from app.modules.campaigns.message_rendering import compute_content_digest
from app.modules.personalization.repository import GenerationDb
from app.modules.sending.repository import SendingRepository
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, seed_world
from tests.test_campaign_paused_edits_real_db import freeze
from tests.test_personalization_real_db import make_runner, run_chunk
from tests.test_same_thread_real_db import (
    follow_up_of,
    progress,
    replica,
    scalar,
    sent_parent,
    thread_step2,
)

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


def hyper_world(su) -> World:
    """A RUNNING personalized campaign with a sendable mailbox and step 2 threaded."""
    w = seed_world(su, running=True, campaign_type="HYPER_PERSONALIZED", members=2, enroll_first=False)
    replica(
        su,
        "UPDATE public.mailboxes SET connection_state='CONNECTED', health_state='HEALTHY', "
        "connected_generation=current_connection_generation, provider_account_id='acct-'||id::text WHERE id=%s",
        [w.mailbox_id],
    )
    replica(su, "INSERT INTO public.campaign_mailboxes (workspace_id, campaign_id, mailbox_id) VALUES (%s,%s,%s)",
            [w.ws, w.campaign_id, w.mailbox_id])
    freeze(su, w)
    thread_step2(su, w)
    return w


def make_due(su, message_id) -> None:
    """Move the follow-up to within one lead time and its job to now, as the
    passage of the wait would."""
    replica(su, "UPDATE public.messages SET due_at=now() + interval '30 minutes' WHERE id=%s", [message_id])
    replica(su, "UPDATE public.message_generations SET next_attempt_at=now() - interval '1 minute' WHERE message_id=%s",
            [message_id])


def planned_follow_up(session_factory, su) -> tuple[World, uuid.UUID, dict]:
    w = hyper_world(su)
    parent = sent_parent(su, w)
    enrollment = scalar(su, "SELECT enrollment_id FROM public.messages WHERE id=%s", parent)
    progress(session_factory, w, datetime.now(UTC))
    return w, parent, follow_up_of(su, enrollment, w.step2)


def test_progression_inserts_the_personalized_follow_up_with_its_parent(session_factory, su) -> None:
    w, parent, child = planned_follow_up(session_factory, su)
    assert child["reply_to_message_id"] == parent
    assert child["status"] == "PLANNED" and child["content_subject"] is None
    assert scalar(su, "SELECT state FROM public.message_generations WHERE message_id=%s", child["id"]) == "PENDING"


def test_the_writer_stores_the_re_subject_and_the_sender_reads_the_thread(session_factory, su) -> None:
    w, parent, child = planned_follow_up(session_factory, su)
    parent_subject = scalar(su, "SELECT content_subject FROM public.messages WHERE id=%s", parent)
    # Due now: the generation job is claimable.
    make_due(su, child["id"])

    runner, model = make_runner(session_factory, w)
    summary = run_chunk(runner, w)
    assert summary.generated + summary.fallback == 1, summary

    row = su.execute(
        "SELECT status, content_subject, content_body_html, content_digest, renderer_version, reply_to_message_id "
        "FROM public.messages WHERE id=%s", [child["id"]],
    ).fetchone()
    status, subject, body, digest, renderer_version, reply_to = row
    assert status == "SCHEDULED"
    assert subject == f"Re: {parent_subject}"
    assert digest == compute_content_digest(subject, body, renderer_version)
    assert reply_to == parent

    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        ctx = SendingRepository(session).load_message_for_send(workspace_id=w.ws, message_id=child["id"])
    finally:
        session.rollback()
        session.close()
    assert ctx is not None and ctx.reply_to_message_id == parent
    assert ctx.thread_in_reply_to == "<parent@acme.test>"
    assert ctx.thread_references == ("<parent@acme.test>",)
    assert ctx.thread_provider_id == "gm-thread-1"
    assert ctx.content_subject == subject


def test_the_writer_can_record_why_a_follow_up_was_not_threaded(session_factory, su) -> None:
    w, _, child = planned_follow_up(session_factory, su)
    with GenerationDb(session_factory, w.ws).transaction() as repo:
        repo.record_same_thread_unavailable(
            workspace_id=w.ws, message_id=child["id"], reason="previous_subject_missing"
        )
    row = su.execute(
        "SELECT action, target_type, reason FROM public.audit_events WHERE target_id=%s", [child["id"]]
    ).fetchone()
    assert row == ("message.same_thread_unavailable", "message", "previous_subject_missing")


def test_a_written_follow_up_cannot_lose_its_parent(session_factory, su) -> None:
    w, _, child = planned_follow_up(session_factory, su)
    make_due(su, child["id"])
    runner, _ = make_runner(session_factory, w)
    run_chunk(runner, w)
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        su.execute("UPDATE public.messages SET reply_to_message_id=NULL WHERE id=%s", [child["id"]])


def test_a_paused_personalized_step_leads_reached_keeps_its_switch(su) -> None:
    w = hyper_world(su)
    sent_parent(su, w)  # frontier = 1
    replica(su, "UPDATE public.campaigns SET status='PAUSED' WHERE id=%s", [w.campaign_id])
    with pytest.raises(psycopg.errors.CheckViolation, match="reached"):
        su.execute("UPDATE public.sequence_steps SET email_same_thread=true WHERE id=%s", [w.step1])
    # After the frontier: free (turning it off needs a subject again -- the CHECK).
    su.execute("UPDATE public.sequence_steps SET email_same_thread=false, email_subject='Back on its own' WHERE id=%s",
               [w.step2])
    assert scalar(su, "SELECT email_same_thread FROM public.sequence_steps WHERE id=%s", w.step2) is False


def test_reconcile_after_a_resume_never_rewrites_a_written_personalized_reply(session_factory, su) -> None:
    from tests.support.pause_support import reconcile

    w, _, child = planned_follow_up(session_factory, su)
    make_due(su, child["id"])
    runner, _ = make_runner(session_factory, w)
    run_chunk(runner, w)
    before = su.execute("SELECT content_subject, content_digest FROM public.messages WHERE id=%s", [child["id"]]).fetchone()
    # Resumed with an edited reference: the generation moved on.
    replica(su, "UPDATE public.sequence_steps SET email_body_html='<p>Edited {{first_name}}</p>' WHERE id=%s", [w.step2])
    replica(su, "UPDATE public.campaigns SET schedule_generation=2 WHERE id=%s", [w.campaign_id])
    reconcile(session_factory, w, 2, datetime.now(UTC))
    after = su.execute(
        "SELECT content_subject, content_digest, schedule_generation FROM public.messages WHERE id=%s", [child["id"]]
    ).fetchone()
    assert after[:2] == before and after[2] == 2
