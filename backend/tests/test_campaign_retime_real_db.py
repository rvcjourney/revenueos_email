"""Re-timing planned personalized follow-ups after a resume (ADR-0020 amendment 2),
against a REAL PostgreSQL (pgserver).

A wait edited while a personalized campaign is paused must reach follow-ups that were
planned with the old one: the post-resume task moves their due time and the time the
writer looks at them. The repository methods run under the real worker role, the real
writer then picks the moved follow-up up. No email is sent and no model is called (a
fake model is injected).
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.modules.campaigns.follow_up_timing import compute_due_at
from app.modules.personalization.budget import AllowAllLimiter
from app.modules.personalization.fake_model import FakeModel
from app.modules.personalization.jit import GenerationRunner, RunnerConfig
from app.modules.personalization.repository import GenerationDb
from app.modules.personalization.service import PersonalizationService
from tests.support.pause_support import enroll
from tests.support.personalization_fakes import FakeResearch
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import FROZEN, World, _insert, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

WINDOW = dict(
    timezone="UTC",
    weekday_set=127,
    window_start_local=time(9, 0),
    window_end_local=time(17, 0),
)
LONG = 3 * 24 * 60  # a three-day wait
LEAD = timedelta(hours=1)  # personalization_lead_time_seconds default


@pytest.fixture(scope="module")
def uri():
    with throwaway_database() as value:
        yield value


@pytest.fixture(scope="module")
def session_factory(uri):
    engine = create_engine(
        "postgresql+psycopg://" + uri.split("://", 1)[1], future=True
    )
    yield sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    engine.dispose()


@pytest.fixture()
def su(uri):
    conn = psycopg.connect(uri, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture()
def workers(monkeypatch, session_factory):
    import workers.campaigns as module

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        module.celery_app,
        "send_task",
        lambda name, kwargs=None, **o: sent.append((name, kwargs or {})),
    )
    module.sent = sent  # type: ignore[attr-defined]
    return module


def world(su, *, campaign_type: str = "HYPER_PERSONALIZED") -> World:
    """E(1) W(2) E(3), RUNNING at schedule generation 2 (it has been resumed once)."""
    w = seed_world(
        su, running=True, campaign_type=campaign_type, members=4, enroll_first=False
    )
    su.execute(
        "UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s",
        [w.campaign_id],
    )
    return w


def due(anchor: datetime, wait_minutes: int) -> datetime:
    return compute_due_at(anchor, wait_minutes, **WINDOW)


def planned_followup(
    su,
    w: World,
    i: int,
    *,
    anchor: datetime,
    planned_due: datetime,
    planned_at: datetime,
    attempt_count: int = 0,
    leased: bool = False,
    written: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Step 3 of lead i, as the planner left it: a PLANNED message and a PENDING job to
    look at it one lead time before `planned_due`. Returns (message id, generation id)."""
    enrollment = enroll(su, w, i)
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaign_enrollments SET next_step_id=%s, next_sequence_position=3 WHERE id=%s",
        [w.step2, enrollment],
    )
    message, generation = uuid.uuid4(), uuid.uuid4()
    extra = (
        dict(
            status="SCHEDULED",
            content_subject="Hi",
            content_body_html="<p>Hi</p>",
            content_digest="d" * 64,
            rendered_at=planned_at,
            frozen_destination=f"lead{i}@target{i}.test",
            frozen_sender_address="s@acme.test",
            schedule_generation=1,
        )
        if written
        else dict(status="PLANNED")
    )
    _insert(
        su,
        "messages",
        id=message,
        workspace_id=w.ws,
        purpose="CAMPAIGN",
        campaign_id=w.campaign_id,
        enrollment_id=enrollment,
        sequence_id=w.sequence_id,
        step_id=w.step2,
        mailbox_id=w.mailbox_id,
        address_id=w.address_ids[i],
        anchor_at=anchor,
        due_at=planned_due,
        created_at=planned_at,
        **extra,
    )
    _insert(
        su,
        "message_generations",
        id=generation,
        workspace_id=w.ws,
        campaign_id=w.campaign_id,
        sequence_id=w.sequence_id,
        step_id=w.step2,
        enrollment_id=enrollment,
        message_id=message,
        state="SUCCEEDED" if written else "PENDING",
        max_attempts=3,
        attempt_count=attempt_count,
        next_attempt_at=planned_due - LEAD,
        updated_at=planned_at,
        **(dict(completed_at=planned_at) if written else {}),
        **(
            dict(
                lease_owner="other",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            if leased
            else {}
        ),
    )
    su.execute("SET session_replication_role = origin")
    return message, generation


def edit_wait(su, w: World, minutes: int) -> None:
    """The edit a manager makes while the campaign is paused (the frozen-sequence guard is
    what the service/trigger tests cover; here only the stored result matters)."""
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.sequence_steps SET wait_duration_minutes=%s, updated_at=now() WHERE id=%s",
        [minutes, w.wait],
    )
    su.execute("SET session_replication_role = origin")


def state(su, message: uuid.UUID, generation: uuid.UUID) -> tuple:
    due_at, status = su.execute(
        "SELECT due_at, status FROM public.messages WHERE id=%s", [message]
    ).fetchone()
    next_attempt, updated = su.execute(
        "SELECT next_attempt_at, updated_at FROM public.message_generations WHERE id=%s",
        [generation],
    ).fetchone()
    return due_at, status, next_attempt, updated


def run_task(workers, w: World, *, generation: int = 2, **kw) -> None:
    workers.reconcile_resume_chunk.apply(
        kwargs=dict(
            workspace_id=str(w.ws),
            campaign_id=str(w.campaign_id),
            generation=generation,
            **kw,
        )
    ).get()


# ---------------------------------------------------------------------------
# the fix
# ---------------------------------------------------------------------------


def test_a_shortened_wait_moves_a_planned_follow_up_earlier(workers, su) -> None:
    w = world(su)
    anchor = datetime.now(UTC)
    message, generation = planned_followup(
        su,
        w,
        0,
        anchor=anchor,
        planned_due=due(anchor, LONG),
        planned_at=anchor - timedelta(days=1),
    )
    edit_wait(su, w, 60)

    run_task(workers, w)

    due_at, status, next_attempt, _ = state(su, message, generation)
    assert status == "PLANNED"
    assert due_at == due(anchor, 60)
    assert (
        next_attempt == due(anchor, 60) - LEAD
    )  # the writer looks at it one lead time before


def test_a_lengthened_wait_moves_a_planned_follow_up_later(workers, su) -> None:
    w = world(su)
    anchor = datetime.now(UTC)
    message, generation = planned_followup(
        su,
        w,
        0,
        anchor=anchor,
        planned_due=due(anchor, 60),
        planned_at=anchor - timedelta(days=1),
    )
    edit_wait(su, w, LONG)

    run_task(workers, w)

    due_at, _, next_attempt, _ = state(su, message, generation)
    assert due_at == due(anchor, LONG) and next_attempt == due(anchor, LONG) - LEAD


def test_a_follow_up_whose_wait_was_not_edited_keeps_its_spread_out_time(
    workers, su
) -> None:
    """An overdue follow-up is planned later than its wait implies; without an edit that slot
    must survive (the wait was last changed BEFORE it was planned)."""
    w = world(su)
    anchor = datetime.now(UTC) - timedelta(days=5)
    spread = datetime.now(UTC) + timedelta(days=1)
    message, generation = planned_followup(
        su,
        w,
        0,
        anchor=anchor,
        planned_due=spread,
        planned_at=datetime.now(UTC) + timedelta(hours=1),
    )

    run_task(workers, w)

    due_at, _, next_attempt, _ = state(su, message, generation)
    assert due_at == spread and next_attempt == spread - LEAD


def test_a_second_run_changes_nothing(workers, su) -> None:
    w = world(su)
    anchor = datetime.now(UTC)
    message, generation = planned_followup(
        su,
        w,
        0,
        anchor=anchor,
        planned_due=due(anchor, LONG),
        planned_at=anchor - timedelta(days=1),
    )
    edit_wait(su, w, 60)
    run_task(workers, w)
    first = state(su, message, generation)

    run_task(workers, w)
    run_task(workers, w)

    assert (
        state(su, message, generation) == first
    )  # incl. updated_at: it no longer matches


# ---------------------------------------------------------------------------
# what must not be touched
# ---------------------------------------------------------------------------


def test_jobs_in_flight_failed_or_written_are_left_alone(workers, su) -> None:
    """The re-time only touches a job nobody has started. (A WRITTEN message is re-timed from the
    current wait by the reconcile pass that runs first, as for any scheduled message; its finished
    job row is none of the re-time's business.)"""
    w = world(su)
    anchor = datetime.now(UTC)
    old, planned_due = anchor - timedelta(days=1), due(anchor, LONG)
    leased = planned_followup(
        su, w, 0, anchor=anchor, planned_due=planned_due, planned_at=old, leased=True
    )
    attempted = planned_followup(
        su,
        w,
        1,
        anchor=anchor,
        planned_due=planned_due,
        planned_at=old,
        attempt_count=1,
    )
    written = planned_followup(
        su, w, 2, anchor=anchor, planned_due=planned_due, planned_at=old, written=True
    )
    edit_wait(su, w, 60)
    before = {m: state(su, m, g)[2:] for m, g in (leased, attempted, written)}

    run_task(workers, w)

    for message, generation in (leased, attempted, written):
        assert (
            state(su, message, generation)[2:] == before[message]
        )  # job row: next attempt, updated_at
    for message, generation in (leased, attempted):
        assert state(su, message, generation)[0] == planned_due


def test_a_standard_campaign_is_never_retimed(workers, su) -> None:
    w = world(su, campaign_type="STANDARD")
    anchor = datetime.now(UTC)
    message, generation = planned_followup(
        su,
        w,
        0,
        anchor=anchor,
        planned_due=due(anchor, LONG),
        planned_at=anchor - timedelta(days=1),
    )
    edit_wait(su, w, 60)

    run_task(workers, w)

    assert state(su, message, generation)[0] == due(anchor, LONG)


def test_a_superseded_task_or_a_paused_campaign_does_nothing(workers, su) -> None:
    w = world(su)
    anchor = datetime.now(UTC)
    message, generation = planned_followup(
        su,
        w,
        0,
        anchor=anchor,
        planned_due=due(anchor, LONG),
        planned_at=anchor - timedelta(days=1),
    )
    edit_wait(su, w, 60)

    run_task(workers, w, generation=1)  # an older resume's task
    assert state(su, message, generation)[0] == due(anchor, LONG)

    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaigns SET status='PAUSED' WHERE id=%s", [w.campaign_id]
    )
    su.execute("SET session_replication_role = origin")
    run_task(workers, w)  # paused again before the task ran
    assert state(su, message, generation)[0] == due(anchor, LONG)


def test_another_workspaces_follow_ups_are_never_touched(workers, su) -> None:
    mine, other = world(su), world(su)
    anchor = datetime.now(UTC)
    theirs = planned_followup(
        su,
        other,
        0,
        anchor=anchor,
        planned_due=due(anchor, LONG),
        planned_at=anchor - timedelta(days=1),
    )
    edit_wait(su, other, 60)

    run_task(workers, mine)

    assert state(su, *theirs)[0] == due(anchor, LONG)


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------


def test_a_large_campaign_is_retimed_in_batches(workers, su, monkeypatch) -> None:
    monkeypatch.setattr(workers, "BATCH_SIZE", 1)
    w = world(su)
    anchor = datetime.now(UTC)
    rows = [
        planned_followup(
            su,
            w,
            i,
            anchor=anchor,
            planned_due=due(anchor, LONG),
            planned_at=anchor - timedelta(days=1),
        )
        for i in range(3)
    ]
    edit_wait(su, w, 60)

    run_task(workers, w)

    assert [state(su, *row)[0] for row in rows] == [due(anchor, 60)] * 3


# ---------------------------------------------------------------------------
# the real writer
# ---------------------------------------------------------------------------


def make_runner(session_factory, w: World, model=None):
    model = model or FakeModel()
    runner = GenerationRunner(
        db=GenerationDb(session_factory, w.ws),
        service=PersonalizationService(
            model=model, research=FakeResearch(), min_facts=2
        ),
        rpm=AllowAllLimiter(),
        config=RunnerConfig(
            lease_seconds=300,
            chunk_size=10,
            max_transient_errors=3,
            daily_generation_cap=100,
        ),
    )
    return runner, model


def enrollment_variables(su, message: uuid.UUID) -> None:
    """The writer reads the lead's frozen variables from the enrollment."""
    su.execute(
        "UPDATE public.campaign_enrollments SET frozen_variables=%s::jsonb WHERE id="
        "(SELECT enrollment_id FROM public.messages WHERE id=%s)",
        [__import__("json").dumps(FROZEN), message],
    )


def test_before_the_fix_the_writer_would_never_have_looked_at_it_and_now_it_does(
    workers, session_factory, su
) -> None:
    w = world(su)
    anchor = datetime.now(UTC) - timedelta(days=2)
    message, generation = planned_followup(
        su,
        w,
        0,
        anchor=anchor,
        planned_due=due(anchor, LONG),
        planned_at=anchor + timedelta(hours=1),
    )
    edit_wait(su, w, 60)
    runner, model = make_runner(session_factory, w)

    # Without the re-time the job is days away: the writer finds nothing to do.
    summary = runner.run_chunk(
        workspace_id=w.ws, campaign_id=w.campaign_id, lease_owner="t"
    )
    assert summary.claimed == 0 and model.calls == 0

    run_task(workers, w)
    summary = runner.run_chunk(
        workspace_id=w.ws, campaign_id=w.campaign_id, lease_owner="t"
    )

    assert summary.generated == 1 and model.calls == 1
    due_at, status, _, _ = state(su, message, generation)
    assert status == "SCHEDULED"
    assert due_at >= datetime.now(UTC) - timedelta(
        minutes=1
    )  # a message is never due in the past


def test_the_writers_own_hold_back_still_covers_a_claim_that_beat_the_retime(
    session_factory, su
) -> None:
    """Backstop: the job was claimable (its old time had come) before any re-time ran, and the
    wait has since been LENGTHENED. The writer finds the new time is later, spends no model call,
    and puts the message and its job back for later."""
    w = world(su)
    anchor = datetime.now(UTC)
    planned_due = datetime.now(UTC) + timedelta(minutes=30)  # its lead time has begun
    message, generation = planned_followup(
        su,
        w,
        0,
        anchor=anchor,
        planned_due=planned_due,
        planned_at=anchor - timedelta(days=1),
    )
    edit_wait(su, w, LONG)
    runner, model = make_runner(session_factory, w)

    summary = runner.run_chunk(
        workspace_id=w.ws, campaign_id=w.campaign_id, lease_owner="t"
    )

    assert summary.deferred == 1 and summary.generated == 0 and model.calls == 0
    due_at, status, next_attempt, _ = state(su, message, generation)
    assert status == "PLANNED" and due_at == due(anchor, LONG)
    assert abs(next_attempt - (due(anchor, LONG) - LEAD)) < timedelta(seconds=30)
    assert su.execute(
        "SELECT attempt_count FROM public.message_generations WHERE id=%s", [generation]
    ).fetchone() == (0,)
