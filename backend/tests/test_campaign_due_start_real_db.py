"""SCHEDULED -> RUNNING at the campaign's start time -- real PostgreSQL.

Nothing used to start a campaign activated with a future start: it stayed SCHEDULED
for ever and every send gate (which needs RUNNING) refused its messages. The scheduler
now performs the documented "Due start" transition. The guard is the UPDATE's WHERE
clause under the row lock, so a stale start can never resume a paused campaign and two
schedulers racing start it once.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.modules.campaigns.schemas import PauseIn, ResumeIn
from app.modules.scheduler.repository import SchedulerRepository
from app.modules.scheduler.service import SchedulerService
from tests.support.pause_support import (
    FakeProducer,
    add_message,
    api,
    camp,
    discoverable,
    pause,
    reconcile,
    resume,
    today_at,
    world,
)
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


@pytest.fixture()
def producer(monkeypatch) -> FakeProducer:
    fake = FakeProducer()
    monkeypatch.setattr("app.services.task_dispatch.get_task_producer", lambda *a, **k: fake)
    monkeypatch.setattr("app.modules.campaigns.preflight.unsubscribe_ready", lambda s: True)
    return fake


def scheduled(su, *, starts_in: timedelta) -> World:
    """An activated campaign waiting for its start time."""
    w = world(su)
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaigns SET status='SCHEDULED', start_at = now() + %s WHERE id=%s",
        [starts_in, w.campaign_id],
    )
    su.execute("SET session_replication_role = origin")
    return w


def start_due(session_factory, limit: int = 50):
    session = session_factory()
    try:
        return SchedulerService(session, Settings.current()).start_due_campaigns(limit=limit)
    finally:
        session.close()


def mine(started, w: World):
    return [row for row in started if row[1] == w.campaign_id]


def status(su, w: World) -> str:
    return camp(su, w)["status"]


def test_a_campaign_whose_start_time_has_come_starts_exactly_once(session_factory, su) -> None:
    w = scheduled(su, starts_in=timedelta(minutes=-1))
    before = camp(su, w)

    assert mine(start_due(session_factory), w) == [(w.ws, w.campaign_id, 1)]
    assert mine(start_due(session_factory), w) == []  # nothing left to start

    after = camp(su, w)
    assert after["status"] == "RUNNING"
    assert after["schedule_generation"] == before["schedule_generation"] == 1  # no fence needed
    assert after["version"] == before["version"] + 1
    assert su.execute("SELECT previous_status FROM public.campaigns WHERE id=%s", [w.campaign_id]).fetchone()[0] == "SCHEDULED"
    row = su.execute(
        "SELECT actor_kind, actor_id, before_state->>'status', after_state->>'status' FROM public.audit_events "
        "WHERE target_id=%s AND action='campaign.start'",
        [w.campaign_id],
    ).fetchall()
    assert row == [("SYSTEM", None, "SCHEDULED", "RUNNING")]


def test_a_campaign_whose_start_is_in_the_future_stays_scheduled(session_factory, su) -> None:
    w = scheduled(su, starts_in=timedelta(hours=1))
    assert mine(start_due(session_factory), w) == []
    assert status(su, w) == "SCHEDULED"


@pytest.mark.parametrize("state", ["RUNNING", "PAUSED", "COMPLETED", "DRAFT"])
def test_only_scheduled_campaigns_are_ever_started(session_factory, su, state) -> None:
    w = world(su)
    su.execute("SET session_replication_role = replica")
    if state == "DRAFT":
        # (a draft cannot be due; the point is that nothing but SCHEDULED is touched)
        su.execute("UPDATE public.campaigns SET start_at = now() - interval '1 hour' WHERE id=%s", [w.campaign_id])
        state = "RUNNING"
    else:
        su.execute(
            "UPDATE public.campaigns SET status=%s, start_at = now() - interval '1 hour' WHERE id=%s",
            [state, w.campaign_id],
        )
    su.execute("SET session_replication_role = origin")
    before = camp(su, w)

    assert mine(start_due(session_factory), w) == []
    assert camp(su, w) == before


def test_a_stale_start_cannot_resume_a_paused_campaign(session_factory, su, producer) -> None:
    """The scheduler found it due, then the user paused it before the start ran."""
    w = scheduled(su, starts_in=timedelta(minutes=-1))
    session = session_factory()
    try:
        discovered = SchedulerRepository(session).find_due_scheduled_campaigns(limit=50)
        session.rollback()
    finally:
        session.close()
    assert (w.ws, w.campaign_id) in discovered

    pause(session_factory, su, w)  # commits first

    session = session_factory()
    try:
        assert SchedulerRepository(session).start_due_campaign(workspace_id=w.ws, campaign_id=w.campaign_id) is None
        session.commit()
    finally:
        session.close()
    assert status(su, w) == "PAUSED"


def test_two_schedulers_racing_start_a_campaign_once(session_factory, su) -> None:
    w = scheduled(su, starts_in=timedelta(minutes=-1))
    barrier = threading.Barrier(2)
    results: list = []

    def scheduler() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            generation = SchedulerRepository(session).start_due_campaign(workspace_id=w.ws, campaign_id=w.campaign_id)
            session.commit()
            results.append(generation)
        finally:
            session.close()

    threads = [threading.Thread(target=scheduler) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results, key=lambda r: r is None) == [1, None]
    assert su.execute(
        "SELECT count(*) FROM public.audit_events WHERE target_id=%s AND action='campaign.start'", [w.campaign_id]
    ).fetchone()[0] == 1


def test_a_started_campaigns_waiting_messages_become_claimable(session_factory, su) -> None:
    w = scheduled(su, starts_in=timedelta(minutes=-1))
    message = add_message(su, w, 0, due_at=datetime.now(UTC) - timedelta(minutes=5))
    later = datetime.now(UTC) + timedelta(days=1)
    assert message not in discoverable(session_factory, w, later)  # SCHEDULED campaigns never send

    start_due(session_factory)

    assert message in discoverable(session_factory, w, later)


@contextmanager
def _scope(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def test_resumed_into_scheduled_then_started_gets_its_old_messages_reconciled(
    session_factory, su, producer, monkeypatch
) -> None:
    """Paused before its start and resumed while the start is still in the future:
    the campaign returns to SCHEDULED with a bumped generation (and no reconcile --
    it is not RUNNING). When the start arrives the scheduler must run the reconcile,
    or the messages stamped with the old generation could never be claimed."""
    import workers.scheduler as scheduler_module

    w = scheduled(su, starts_in=timedelta(hours=2))
    now = today_at(10)
    message = add_message(su, w, 0, due_at=now + timedelta(hours=3))
    pause(session_factory, su, w)
    resumed = resume(session_factory, su, w)
    assert resumed.status == "SCHEDULED" and camp(su, w)["schedule_generation"] == 2
    assert not [t for t in producer.sent if t[0] == "campaigns.reconcile_resume_chunk"]  # nothing to reconcile yet

    su.execute("SET session_replication_role = replica")
    su.execute("UPDATE public.campaigns SET start_at = now() - interval '1 minute' WHERE id=%s", [w.campaign_id])
    su.execute("SET session_replication_role = origin")

    sent: list[tuple[str, dict]] = []
    import workers.celery_app as celery_module

    monkeypatch.setattr(celery_module.celery_app, "send_task", lambda name, kwargs=None, **o: sent.append((name, kwargs or {})))
    monkeypatch.setattr(scheduler_module, "session_scope", lambda settings: _scope(session_factory))
    runtime = scheduler_module.SchedulerRuntime(Settings.current())

    started = runtime._start_due_campaigns()

    assert started >= 1 and status(su, w) == "RUNNING"
    tasks = [kw for name, kw in sent if name == "campaigns.reconcile_resume_chunk" and kw["campaign_id"] == str(w.campaign_id)]
    assert tasks == [{"workspace_id": str(w.ws), "campaign_id": str(w.campaign_id), "generation": 2}]

    far = datetime.now(UTC) + timedelta(days=3)
    assert message not in discoverable(session_factory, w, far)  # fenced until reconciled
    reconcile(session_factory, w, 2, now)
    assert message in discoverable(session_factory, w, far)


def test_activation_in_the_future_then_start_is_the_whole_lifecycle(session_factory, su) -> None:
    """The case that was impossible before: a campaign activated for later actually runs."""
    w = scheduled(su, starts_in=timedelta(seconds=-1))
    assert status(su, w) == "SCHEDULED"
    start_due(session_factory)
    assert status(su, w) == "RUNNING"
    assert camp(su, w)["schedule_generation"] == 1 and PauseIn is not None and ResumeIn is not None and api is not None
