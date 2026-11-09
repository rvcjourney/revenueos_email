"""A mailbox that cannot send must not cost claims or loop messages -- real PostgreSQL.

Before: the scheduler only skipped a mailbox in cooldown (`blocked_until`). A message
for a disconnected / restricted / degraded / breaker-open / held mailbox was claimed,
refused by the send worker, left QUEUED for the 5-minute claim lease and claimed again
-- for ever -- while filling the global claim budget that healthy mailboxes need.
Now the scheduler does not claim for such a mailbox at all, and a send worker that is
refused by the mailbox gate hands the message back instead of leaving it to the lease.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.modules.scheduler.repository import SchedulerRepository
from app.modules.scheduler.schemas import SendTaskPayload
from app.modules.sending.service import SendingService
from tests.support.pause_support import add_message, discoverable, msg, world
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World

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


def set_mailbox(su, w: World, assignments: str) -> None:
    """Change mailbox columns the way an operator/worker would (the identity guard
    forbids some of these on a live row, hence replica mode)."""
    su.execute("SET session_replication_role = replica")
    su.execute(f"UPDATE public.mailboxes SET {assignments} WHERE id=%s", [w.mailbox_id])
    su.execute("SET session_replication_role = origin")


UNHEALTHY = [
    pytest.param("connection_state='RECONNECT_REQUIRED', connected_generation=NULL", id="reconnect-required"),
    pytest.param("connection_state='DISCONNECTED', connected_generation=NULL", id="disconnected"),
    pytest.param("policy_state='RESTRICTED', policy_reason='test restriction'", id="restricted"),
    pytest.param("health_state='DEGRADED'", id="degraded"),
    pytest.param("circuit_state='OPEN'", id="breaker-open"),
    pytest.param("pending_safety_count=1", id="safety-hold"),
]


def claim(session_factory, w: World, message, at: datetime):
    session = session_factory()
    try:
        result = SchedulerRepository(session).claim_due_message(
            workspace_id=w.ws, message_id=message, authoritative_now=at
        )
        session.rollback()  # the test only reads the verdict
        return result
    finally:
        session.close()


@pytest.mark.parametrize("assignments", UNHEALTHY)
def test_the_scheduler_neither_discovers_nor_claims_for_a_mailbox_that_cannot_send(
    session_factory, su, assignments
) -> None:
    w = world(su)
    message = add_message(su, w, 0, due_at=datetime.now(UTC) - timedelta(hours=1))
    later = datetime.now(UTC) + timedelta(days=1)
    assert message in discoverable(session_factory, w, later)  # healthy: offered...
    assert claim(session_factory, w, message, later).claimed is True  # ...and claimable

    set_mailbox(su, w, assignments)

    assert message not in discoverable(session_factory, w, later)
    refused = claim(session_factory, w, message, later)
    assert refused.claimed is False and "Mailbox cannot send" in refused.reason


def test_unhealthy_mailboxes_do_not_starve_healthy_ones(session_factory, su) -> None:
    """The dead mailbox's old messages used to fill the discovery window."""
    dead, alive = world(su, members=1), world(su, members=1)
    set_mailbox(su, dead, "connection_state='DISCONNECTED', connected_generation=NULL")
    stuck = add_message(su, dead, 0, due_at=datetime.now(UTC) - timedelta(days=2))
    ready = add_message(su, alive, 0, due_at=datetime.now(UTC) - timedelta(hours=1))
    later = datetime.now(UTC) + timedelta(days=1)

    session = session_factory()
    try:
        found = {c.id for c in SchedulerRepository(session).find_due_messages(limit=500, authoritative_now=later)}
    finally:
        session.rollback()
        session.close()

    assert ready in found and stuck not in found


def _payload(w: World, message) -> SendTaskPayload:
    return SendTaskPayload(
        work_id=str(uuid.uuid4()),
        delivery_id=str(uuid.uuid4()),
        message_id=str(message),
        dispatch_id=str(uuid.uuid4()),
        resource_id=str(message),
        workspace_id=str(w.ws),
        dispatch_generation=1,
    )


def _send(session_factory, w: World, message):
    session = session_factory()
    try:
        limiter = MagicMock()
        outcome = SendingService(session, settings=Settings.current(), rate_limiter=limiter).execute(
            _payload(w, message)
        )
    finally:
        session.close()
    limiter.reserve.assert_not_called()  # refused before any capacity was reserved
    return outcome


def test_a_refused_message_is_handed_back_not_left_to_the_claim_lease(session_factory, su) -> None:
    w = world(su)
    message = add_message(su, w, 0, status="QUEUED", due_at=datetime.now(UTC) - timedelta(minutes=1))
    set_mailbox(su, w, "connection_state='DISCONNECTED', connected_generation=NULL")

    outcome = _send(session_factory, w, message)

    assert (outcome.outcome, outcome.reason) == ("DEFERRED", "mailbox_disconnected")
    after = msg(su, message)
    assert after["status"] == "SCHEDULED"  # released now, not after 5 minutes
    assert after["dispatch_generation"] == 2  # a late duplicate task is stale
    due = su.execute("SELECT due_at, claim_expires_at FROM public.messages WHERE id=%s", [message]).fetchone()
    wait = (due[0] - datetime.now(UTC)).total_seconds()
    assert 800 <= wait <= 1000 and due[1] is None  # ~15 minutes, like a held mailbox


def test_a_cooldown_is_waited_out_for_its_own_length(session_factory, su) -> None:
    w = world(su)
    message = add_message(su, w, 0, status="QUEUED", due_at=datetime.now(UTC) - timedelta(minutes=1))
    set_mailbox(su, w, "blocked_until = now() + interval '10 minutes'")

    outcome = _send(session_factory, w, message)

    assert outcome.reason == "mailbox_blocked"
    wait = (
        su.execute("SELECT due_at FROM public.messages WHERE id=%s", [message]).fetchone()[0] - datetime.now(UTC)
    ).total_seconds()
    assert 560 <= wait <= 700  # about the cooldown, clamped and lightly jittered


def test_once_the_mailbox_recovers_the_message_is_offered_again(session_factory, su) -> None:
    w = world(su)
    message = add_message(su, w, 0, status="QUEUED", due_at=datetime.now(UTC) - timedelta(minutes=1))
    set_mailbox(su, w, "circuit_state='OPEN'")
    _send(session_factory, w, message)  # handed back
    later = datetime.now(UTC) + timedelta(days=1)
    assert message not in discoverable(session_factory, w, later)

    set_mailbox(su, w, "circuit_state='CLOSED'")

    assert message in discoverable(session_factory, w, later)
