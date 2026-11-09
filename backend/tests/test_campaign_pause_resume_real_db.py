"""Campaign pause / resume against a REAL PostgreSQL (pgserver) -- ADR-0020.

Covers the state machine (idempotent repeats, illegal states, stale versions,
cross-tenant, permissions), the resume fence (`schedule_generation`), the
post-resume reconcile pass (spreading, never early, never touching in-flight
work) and pause/resume racing on two connections. Everything runs as the real
`app_api` / `app_worker_general` / `app_scheduler` roles, so RLS, grants and the
guard triggers are exercised for real. No email is sent and nothing touches the
production-linked Supabase project.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import WorkspaceContext, get_db, get_workspace_context
from app.core.errors import AppError
from app.db.context import set_transaction_context
from app.main import app
from app.modules.campaigns.activation_service import CampaignActivationService
from app.modules.campaigns.schemas import (
    CampaignSettingsCreateIn,
    CampaignUpdateIn,
    PauseIn,
    ResumeIn,
)
from app.modules.campaigns.service import CampaignService
from app.modules.campaigns.settings_service import CampaignSettingsService
from app.modules.campaigns.worker_repository import CampaignWorkerRepository
from app.modules.scheduler.repository import SchedulerRepository
from tests.support.pause_support import (
    FakeProducer,
    add_message,
    api,
    audit_count,
    camp,
    discoverable,
    draft_world,
    enroll,
    force_status,
    msg,
    pause,
    reconcile,
    resume,
    today_at,
    world,
)
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, _insert

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
    # Resume revalidation requires the unsubscribe settings; the test env has none.
    monkeypatch.setattr("app.modules.campaigns.preflight.unsubscribe_ready", lambda s: True)
    return fake


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------


def test_pause_running_campaign_records_metadata_and_audit(session_factory, su, producer) -> None:
    w = world(su)
    add_message(su, w, 0, status="QUEUED", due_at=datetime.now(UTC) - timedelta(minutes=1))
    before = camp(su, w)

    out = pause(session_factory, su, w)

    after = camp(su, w)
    assert out.status == "PAUSED" and after["status"] == "PAUSED"
    assert after["paused_at"] is not None
    assert after["version"] == before["version"] + 1
    assert out.outstanding_attempts == 1  # the QUEUED message cannot be recalled
    row = su.execute(
        "SELECT before_state->>'status', after_state->>'status' FROM public.audit_events "
        "WHERE target_id=%s AND action='campaign.pause'",
        [w.campaign_id],
    ).fetchone()
    assert row == ("RUNNING", "PAUSED")


def test_pause_twice_is_an_idempotent_noop(session_factory, su, producer) -> None:
    w = world(su)
    first = pause(session_factory, su, w)
    stale = first.version - 1  # a second tab still holding the pre-pause version
    again = pause(session_factory, su, w, expected=stale)

    assert again.status == "PAUSED"
    assert camp(su, w)["version"] == first.version  # nothing re-written
    assert audit_count(su, w, "campaign.pause") == 1


@pytest.mark.parametrize("status", ["DRAFT", "COMPLETED", "ARCHIVED"])
def test_pause_is_refused_outside_running_or_scheduled(session_factory, su, producer, status) -> None:
    if status == "DRAFT":
        w = draft_world(su)
    else:
        w = world(su)
        force_status(su, w, status)
    with pytest.raises(AppError) as info:
        pause(session_factory, su, w)
    assert info.value.status_code == 409 and info.value.code == "state_conflict"
    assert camp(su, w)["status"] == status


def test_pause_with_a_stale_version_conflicts(session_factory, su, producer) -> None:
    w = world(su)
    with pytest.raises(AppError) as info:
        pause(session_factory, su, w, expected=camp(su, w)["version"] + 5)
    assert info.value.status_code == 409 and info.value.code == "conflict"
    assert camp(su, w)["status"] == "RUNNING"


def test_resume_bumps_generation_clears_pause_and_dispatches_reconcile(session_factory, su, producer) -> None:
    w = world(su)
    pause(session_factory, su, w)
    su.execute(
        "UPDATE public.campaigns SET paused_at = now() - interval '10 minutes' WHERE id=%s",
        [w.campaign_id],
    )

    out = resume(session_factory, su, w)

    after = camp(su, w)
    assert out.status == "RUNNING" and after["status"] == "RUNNING"
    assert after["schedule_generation"] == 2
    assert after["paused_at"] is None and after["last_resumed_at"] is not None
    assert 590 <= after["paused_total_seconds"] <= 700
    assert audit_count(su, w, "campaign.resume") == 1
    tasks = [(n, k, o) for n, k, o in producer.sent if n == "campaigns.reconcile_resume_chunk"]
    assert len(tasks) == 2  # now, and once more after the claim lease
    assert tasks[0][1]["generation"] == 2 and tasks[0][1]["campaign_id"] == str(w.campaign_id)
    assert "countdown" in tasks[1][2] and "countdown" not in tasks[0][2]


def test_resume_twice_does_not_bump_the_generation_again(session_factory, su, producer) -> None:
    w = world(su)
    pause(session_factory, su, w)
    first = resume(session_factory, su, w)
    again = resume(session_factory, su, w, expected=first.version - 1)  # stale tab

    assert again.status == "RUNNING"
    assert camp(su, w)["schedule_generation"] == 2
    assert audit_count(su, w, "campaign.resume") == 1
    assert sum(1 for n, _, _ in producer.sent if n == "campaigns.reconcile_resume_chunk") == 2


@pytest.mark.parametrize("status", ["DRAFT", "COMPLETED", "ARCHIVED"])
def test_resume_is_refused_for_finished_or_unstarted_campaigns(session_factory, su, producer, status) -> None:
    if status == "DRAFT":
        w = draft_world(su)
    else:
        w = world(su)
        force_status(su, w, status)
    with pytest.raises(AppError) as info:
        resume(session_factory, su, w)
    assert info.value.status_code == 409 and info.value.code == "state_conflict"
    assert camp(su, w)["schedule_generation"] == 1
    assert not producer.sent


def test_resume_with_a_stale_version_conflicts_and_changes_nothing(session_factory, su, producer) -> None:
    w = world(su, status="PAUSED")
    with pytest.raises(AppError) as info:
        resume(session_factory, su, w, expected=camp(su, w)["version"] + 3)
    assert info.value.code == "conflict"
    assert camp(su, w)["status"] == "PAUSED" and camp(su, w)["schedule_generation"] == 1


def test_resume_is_blocked_when_no_mailbox_can_send(session_factory, su, producer) -> None:
    w = world(su, status="PAUSED")
    su.execute("SET session_replication_role = replica")  # the identity guard forbids this change
    su.execute(
        "UPDATE public.mailboxes SET connection_state='DISCONNECTED', connected_generation=NULL WHERE id=%s",
        [w.mailbox_id],
    )
    su.execute("SET session_replication_role = origin")
    with pytest.raises(AppError) as info:
        resume(session_factory, su, w)
    assert info.value.status_code == 422 and info.value.code == "resume_blocked"
    codes = {e["code"] for e in info.value.details["errors"]}
    assert "no_usable_mailboxes" in codes
    after = camp(su, w)
    assert after["status"] == "PAUSED" and after["schedule_generation"] == 1
    assert not producer.sent


def test_resume_is_blocked_while_the_only_mailbox_has_a_safety_hold(session_factory, su, producer) -> None:
    w = world(su, status="PAUSED")
    su.execute("UPDATE public.mailboxes SET pending_safety_count = 1 WHERE id=%s", [w.mailbox_id])
    with pytest.raises(AppError) as info:
        resume(session_factory, su, w)
    assert {e["code"] for e in info.value.details["errors"]} == {"no_usable_mailboxes"}


def test_cross_tenant_pause_and_resume_are_not_found(session_factory, su, producer) -> None:
    mine = world(su)
    theirs = world(su)
    session, ctx = api(session_factory, mine)
    try:
        with pytest.raises(AppError) as info:
            CampaignActivationService(session).pause(ctx, theirs.campaign_id, PauseIn(expected_version=1))
        assert info.value.status_code == 404
        session.rollback()
        with pytest.raises(AppError) as info:
            CampaignActivationService(session).resume(ctx, theirs.campaign_id, ResumeIn(expected_version=1))
        assert info.value.status_code == 404
    finally:
        session.close()
    assert camp(su, theirs)["status"] == "RUNNING"


# ---------------------------------------------------------------------------
# through the real routes (permission + response shape)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(session_factory):
    holder: dict[str, tuple[World, str]] = {}

    def override_get_db():
        w, role = holder["as"]
        session, _ = api(session_factory, w, role)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def override_ctx():
        w, role = holder["as"]
        user = {"OWNER": w.owner, "MANAGER": w.manager, "MEMBER": w.member}[role]
        return WorkspaceContext(workspace_id=w.ws, user_id=user, role_code=role)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_workspace_context] = override_ctx
    test_client = TestClient(app)
    test_client.holder = holder  # type: ignore[attr-defined]
    yield test_client
    app.dependency_overrides.clear()


def test_member_cannot_pause_or_resume_over_http(client, su, producer) -> None:
    w = world(su)
    client.holder["as"] = (w, "MEMBER")
    body = {"expected_version": camp(su, w)["version"]}
    base = f"/api/v1/workspaces/{w.ws}/campaigns/{w.campaign_id}"
    assert client.post(f"{base}/pause", json=body).status_code == 403
    assert client.post(f"{base}/resume", json=body).status_code == 403
    assert camp(su, w)["status"] == "RUNNING"


def test_manager_pause_resume_round_trip_over_http(client, su, producer) -> None:
    w = world(su)
    client.holder["as"] = (w, "MANAGER")
    base = f"/api/v1/workspaces/{w.ws}/campaigns/{w.campaign_id}"

    paused = client.post(f"{base}/pause", json={"expected_version": camp(su, w)["version"]})
    assert paused.status_code == 200, paused.text
    body = paused.json()
    assert body["status"] == "PAUSED" and body["paused_at"] is not None
    assert body["outstanding_attempts"] == 0

    resumed = client.post(f"{base}/resume", json={"expected_version": body["version"]})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING" and resumed.json()["paused_at"] is None
    assert resumed.json()["last_resumed_at"] is not None


# ---------------------------------------------------------------------------
# the fence + reconcile
# ---------------------------------------------------------------------------


def test_paused_campaign_work_is_never_discovered(session_factory, su, producer) -> None:
    w = world(su)
    due = datetime.now(UTC) - timedelta(hours=1)
    message = add_message(su, w, 0, due_at=due)
    assert message in discoverable(session_factory, w, datetime.now(UTC))

    pause(session_factory, su, w)

    assert message not in discoverable(session_factory, w, datetime.now(UTC))


def test_resume_fence_holds_old_messages_until_reconciled(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    due = now - timedelta(hours=1)
    message = add_message(su, w, 0, due_at=due)
    pause(session_factory, su, w)
    resume(session_factory, su, w)

    far_future = datetime.now(UTC) + timedelta(days=3)
    assert message not in discoverable(session_factory, w, far_future)  # fenced

    summary = reconcile(session_factory, w, 2, now)

    assert summary.restamped == 1
    assert msg(su, message)["schedule_generation"] == 2
    assert message in discoverable(session_factory, w, far_future)


def test_reconcile_spreads_overdue_messages_instead_of_releasing_a_burst(session_factory, su, producer) -> None:
    w = world(su, members=3)
    now = today_at(10)
    ids = [add_message(su, w, i, due_at=now - timedelta(hours=3 - i)) for i in range(3)]
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    summary = reconcile(session_factory, w, 2, now)

    assert (summary.spread, summary.restamped, summary.kept_future) == (3, 3, 0)
    due = [msg(su, m)["due_at"] for m in ids]
    assert all(d >= now for d in due)
    assert due[0] < due[1] < due[2]  # original order preserved
    assert (due[1] - due[0]).total_seconds() >= 60 and (due[2] - due[1]).total_seconds() >= 60
    assert all(d.hour < 17 for d in due)  # inside the 09:00-17:00 window


def test_reconcile_keeps_future_work_and_never_moves_it_earlier(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    future = now + timedelta(hours=2)
    message = add_message(su, w, 0, due_at=future)
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    summary = reconcile(session_factory, w, 2, now)

    assert (summary.kept_future, summary.spread) == (1, 0)
    assert msg(su, message)["due_at"] == future
    assert msg(su, message)["schedule_generation"] == 2


def test_followup_is_never_sent_earlier_than_its_wait(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    enrollment = enroll(su, w, 0)
    accepted = now - timedelta(hours=5)  # wait is 2880 min = 48 h
    message = add_message(
        su, w, 0, enrollment=enrollment, step=w.step2, due_at=now - timedelta(hours=1), anchor_at=accepted
    )
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    reconcile(session_factory, w, 2, now)

    assert msg(su, message)["due_at"] >= accepted + timedelta(minutes=2880)


def test_a_shortened_wait_is_honoured_after_resume(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    accepted = now - timedelta(hours=5)
    message = add_message(su, w, 0, step=w.step2, due_at=accepted + timedelta(minutes=2880), anchor_at=accepted)
    su.execute("SET session_replication_role = replica")
    su.execute("UPDATE public.sequence_steps SET wait_duration_minutes = 60 WHERE id=%s", [w.wait])
    su.execute("SET session_replication_role = origin")
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    summary = reconcile(session_factory, w, 2, now)

    new_due = msg(su, message)["due_at"]
    assert summary.spread == 1
    assert accepted + timedelta(minutes=60) <= new_due <= now + timedelta(hours=8)


def test_retry_scheduled_message_keeps_due_at_after_next_retry_at(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    retry_at = now + timedelta(hours=3)
    message = add_message(su, w, 0, status="RETRY_SCHEDULED", due_at=retry_at, next_retry_at=retry_at)
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    reconcile(session_factory, w, 2, now)

    assert msg(su, message)["due_at"] >= retry_at


def test_reconcile_never_touches_in_flight_messages(session_factory, su, producer) -> None:
    w = world(su, members=3)
    now = today_at(10)
    queued = add_message(su, w, 0, status="QUEUED", due_at=now - timedelta(hours=1))
    sending = add_message(su, w, 1, status="SENDING", due_at=now - timedelta(hours=1))
    unknown = add_message(su, w, 2, status="UNKNOWN_OUTCOME", due_at=now - timedelta(hours=1))
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])
    before = {m: msg(su, m) for m in (queued, sending, unknown)}

    summary = reconcile(session_factory, w, 2, now)

    assert summary.fetched == 0
    assert {m: msg(su, m) for m in before} == before


def test_reconcile_is_idempotent(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    message = add_message(su, w, 0, due_at=now - timedelta(hours=1))
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    reconcile(session_factory, w, 2, now)
    first = msg(su, message)
    again = reconcile(session_factory, w, 2, now + timedelta(minutes=5))

    assert again.fetched == 0
    assert msg(su, message) == first


def test_superseded_or_paused_reconcile_does_nothing(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    message = add_message(su, w, 0, due_at=now - timedelta(hours=1))
    su.execute("UPDATE public.campaigns SET schedule_generation = 3 WHERE id=%s", [w.campaign_id])

    stale_task = reconcile(session_factory, w, 2, now)  # a newer resume owns generation 3
    assert stale_task.active is False and msg(su, message)["schedule_generation"] == 1

    su.execute("UPDATE public.campaigns SET status='PAUSED' WHERE id=%s", [w.campaign_id])
    paused_task = reconcile(session_factory, w, 3, now)
    assert paused_task.active is False and msg(su, message)["schedule_generation"] == 1


def test_stale_generation_sweep_finds_unfinished_resumes_only(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    add_message(su, w, 0, due_at=now - timedelta(hours=1))
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    def age(interval: str) -> None:
        # Triggers keep updated_at current on every UPDATE, so age it with them off.
        su.execute("SET session_replication_role = replica")
        su.execute(f"UPDATE public.campaigns SET updated_at = now() - interval '{interval}' WHERE id=%s", [w.campaign_id])
        su.execute("SET session_replication_role = origin")

    def swept() -> list[tuple[uuid.UUID, uuid.UUID, int]]:
        session = session_factory()
        try:
            found = SchedulerRepository(session).find_campaigns_with_stale_generation(after_id=None, limit=1000)
            return [row for row in found if row[0] == w.ws]
        finally:
            session.rollback()
            session.close()

    assert swept() == [(w.ws, w.campaign_id, 2)]
    # A campaign that changed long ago is not scanned every minute for ever.
    age("3 days")
    assert swept() == []
    age("1 minute")
    assert swept() == [(w.ws, w.campaign_id, 2)]
    reconcile(session_factory, w, 2, now)
    assert swept() == []


def test_reconcile_pages_through_a_large_backlog_without_skipping_or_repeating(session_factory, su, producer) -> None:
    """A full chunk hands its keyset position (and the per-mailbox slot counters) to
    the next one; together the chunks must cover every message exactly once."""
    w = world(su, members=3)
    now = today_at(10)
    ids = [add_message(su, w, i, due_at=now - timedelta(hours=3 - i)) for i in range(3)]
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    seen: list = []
    after_due_at = after_id = None
    offsets: dict = {}
    for _ in range(5):
        session = session_factory()
        try:
            from app.core.config import Settings
            from app.db.context import set_transaction_context
            from app.modules.campaigns.pacing import MailboxPacing
            from app.modules.campaigns.reconcile import ReconcileService
            from app.modules.campaigns.worker_repository import CampaignWorkerRepository

            set_transaction_context(session, workspace_id=w.ws)
            summary = ReconcileService(
                CampaignWorkerRepository(session), default_pacing=MailboxPacing(50, 60)
            ).reconcile_batch(
                workspace_id=w.ws, campaign_id=w.campaign_id, generation=2, limit=1, now=now,
                after_due_at=after_due_at, after_id=after_id, offsets=offsets,
            )
            session.commit()
        finally:
            session.close()
        if not summary.fetched:
            break
        seen.append(summary.last_id)
        after_due_at, after_id, offsets = summary.last_due_at, summary.last_id, summary.offsets
    assert sorted(seen) == sorted(ids)  # each message visited exactly once
    due = sorted(msg(su, m)["due_at"] for m in ids)
    assert due[0] < due[1] < due[2] and all(msg(su, m)["schedule_generation"] == 2 for m in ids)
    assert Settings is not None


def test_new_messages_are_stamped_with_the_campaigns_current_generation(session_factory, su, producer) -> None:
    """A message rendered after a resume must carry the new generation, or the
    scheduler would never claim it."""
    w = world(su)
    su.execute("UPDATE public.campaigns SET schedule_generation = 4 WHERE id=%s", [w.campaign_id])
    enrollment = enroll(su, w, 0)
    planned = uuid.uuid4()
    su.execute("SET session_replication_role = replica")
    _insert(
        su, "messages", id=planned, workspace_id=w.ws, purpose="CAMPAIGN", campaign_id=w.campaign_id,
        enrollment_id=enrollment, sequence_id=w.sequence_id, step_id=w.step1, mailbox_id=w.mailbox_id,
        address_id=w.address_ids[0], status="PLANNED",
    )
    su.execute("SET session_replication_role = origin")

    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        CampaignWorkerRepository(session).render_message(
            workspace_id=w.ws, message_id=planned, content_subject="s", content_body_html="<p>b</p>",
            content_digest="b" * 64, frozen_destination="lead0@target0.test",
            frozen_sender_address="sender@acme.test", frozen_sender_name=None,
            due_at=datetime.now(UTC), anchor_at=datetime.now(UTC),
        )
        session.commit()
    finally:
        session.close()

    row = msg(su, planned)
    assert row["status"] == "SCHEDULED" and row["schedule_generation"] == 4


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_concurrent_pause_requests_transition_exactly_once(session_factory, su, producer) -> None:
    w = world(su)
    expected = camp(su, w)["version"]
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        session, ctx = api(session_factory, w)
        try:
            barrier.wait(timeout=10)
            out = CampaignActivationService(session).pause(
                ctx, w.campaign_id, PauseIn(expected_version=expected)
            )
            session.commit()
            results.append(out.status)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert results == ["PAUSED", "PAUSED"]
    assert camp(su, w)["version"] == expected + 1
    assert audit_count(su, w, "campaign.pause") == 1


def test_rapid_pause_resume_pause_ends_deterministically(session_factory, su, producer) -> None:
    w = world(su)
    pause(session_factory, su, w)
    resume(session_factory, su, w)
    pause(session_factory, su, w)
    resume(session_factory, su, w)

    after = camp(su, w)
    assert after["status"] == "RUNNING" and after["schedule_generation"] == 3
    assert audit_count(su, w, "campaign.pause") == 2 and audit_count(su, w, "campaign.resume") == 2


# ---------------------------------------------------------------------------
# settings / metadata edits while paused
# ---------------------------------------------------------------------------


def new_settings(**overrides) -> CampaignSettingsCreateIn:
    values = dict(
        timezone="UTC",
        weekdays=[1, 2, 3, 4, 5, 6, 7],
        window_start_local=time(13, 0),
        window_end_local=time(15, 0),
        daily_limit=20,
    )
    values.update(overrides)
    return CampaignSettingsCreateIn(**values)


def test_manager_changes_the_schedule_of_a_paused_campaign(session_factory, su, producer) -> None:
    w = world(su, status="PAUSED")
    before = su.execute("SELECT current_settings_id FROM public.campaigns WHERE id=%s", [w.campaign_id]).fetchone()[0]
    session, ctx = api(session_factory, w, "MANAGER")
    try:
        out = CampaignSettingsService(session).create_settings_version(ctx, w.campaign_id, new_settings())
        session.commit()
    finally:
        session.close()

    after = su.execute("SELECT current_settings_id FROM public.campaigns WHERE id=%s", [w.campaign_id]).fetchone()[0]
    assert after == out.id and after != before
    assert out.revision == 2 and out.daily_limit == 20
    assert audit_count(su, w, "campaign.settings_changed_while_paused") == 1
    # the old version is kept as history, never rewritten
    assert su.execute(
        "SELECT count(*) FROM public.campaign_settings_versions WHERE campaign_id=%s", [w.campaign_id]
    ).fetchone()[0] == 2


def test_member_cannot_change_a_paused_campaigns_schedule(session_factory, su, producer) -> None:
    w = world(su, status="PAUSED")
    session, ctx = api(session_factory, w, "MEMBER")
    try:
        with pytest.raises(AppError) as info:
            CampaignSettingsService(session).create_settings_version(ctx, w.campaign_id, new_settings())
    finally:
        session.rollback()
        session.close()
    assert info.value.status_code == 403


def test_running_campaign_schedule_stays_frozen(session_factory, su, producer) -> None:
    w = world(su)
    session, ctx = api(session_factory, w, "MANAGER")
    try:
        with pytest.raises(AppError) as info:
            CampaignSettingsService(session).create_settings_version(ctx, w.campaign_id, new_settings())
    finally:
        session.rollback()
        session.close()
    assert info.value.status_code == 409 and info.value.code == "state_conflict"


def test_schedule_edited_while_paused_applies_to_unsent_work_on_resume(session_factory, su, producer) -> None:
    w = world(su)
    now = today_at(10)
    soon = now + timedelta(hours=1)  # 11:00 -- outside the new 13:00-15:00 window
    message = add_message(su, w, 0, due_at=soon)
    pause(session_factory, su, w)
    session, ctx = api(session_factory, w, "MANAGER")
    try:
        CampaignSettingsService(session).create_settings_version(ctx, w.campaign_id, new_settings())
        session.commit()
    finally:
        session.close()
    resume(session_factory, su, w)

    reconcile(session_factory, w, 2, now)

    due = msg(su, message)["due_at"]
    assert due >= soon and 13 <= due.hour < 15


def test_manager_can_rename_a_paused_campaign_but_a_member_cannot(session_factory, su, producer) -> None:
    w = world(su, status="PAUSED")
    session, ctx = api(session_factory, w, "MANAGER")
    try:
        out = CampaignService(session).update_campaign(
            ctx, w.campaign_id, CampaignUpdateIn(expected_version=camp(su, w)["version"], name="Renamed")
        )
        session.commit()
    finally:
        session.close()
    assert out.name == "Renamed"

    session, ctx = api(session_factory, w, "MEMBER")
    try:
        with pytest.raises(AppError) as info:
            CampaignService(session).update_campaign(
                ctx, w.campaign_id, CampaignUpdateIn(expected_version=camp(su, w)["version"], name="Nope")
            )
    finally:
        session.rollback()
        session.close()
    assert info.value.status_code == 403


def test_running_campaign_cannot_be_renamed(session_factory, su, producer) -> None:
    w = world(su)
    session, ctx = api(session_factory, w, "MANAGER")
    try:
        with pytest.raises(AppError) as info:
            CampaignService(session).update_campaign(
                ctx, w.campaign_id, CampaignUpdateIn(expected_version=camp(su, w)["version"], name="Nope")
            )
    finally:
        session.rollback()
        session.close()
    assert info.value.status_code == 409


# ---------------------------------------------------------------------------
# pause racing the send path (CAMPAIGN_STATE_MACHINE: "pause racing send authorization")
# ---------------------------------------------------------------------------


def test_pause_waits_for_an_authorization_in_flight_and_counts_it(session_factory, su, producer) -> None:
    """The send path locks the campaign before it decides; a pause arriving then
    must wait for that decision, never slip in between check and authorization.
    If the send was authorized first it finishes, and the pause says so."""
    from app.modules.sending.repository import SendingRepository

    w = world(su)
    message = add_message(su, w, 0, status="QUEUED", due_at=datetime.now(UTC) - timedelta(minutes=1))

    sender = session_factory()
    repo = SendingRepository(sender)
    loaded = repo.load_message_for_send(workspace_id=w.ws, message_id=message)  # holds the campaign lock
    assert loaded is not None and loaded.campaign_status == "RUNNING"

    finished = threading.Event()
    outcome: dict = {}

    def pauser() -> None:
        try:
            outcome["out"] = pause(session_factory, su, w)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            outcome["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=pauser)
    thread.start()
    assert not finished.wait(1.0), "pause must wait for the in-flight authorization"

    # The authorization transaction commits: the message is now SENDING.
    assert repo.transition_message_to_sending(
        workspace_id=w.ws, message_id=message, expected_version=loaded.message_version
    )
    sender.commit()
    sender.close()

    assert finished.wait(15)
    thread.join(timeout=5)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["out"].status == "PAUSED"
    assert outcome["out"].outstanding_attempts == 1  # the SENDING one, which cannot be recalled
    assert msg(su, message)["status"] == "SENDING"


def test_a_pause_that_wins_the_race_stops_the_send_before_authorization(session_factory, su, producer) -> None:
    from app.modules.sending import gates
    from app.modules.sending.repository import SendingRepository

    w = world(su)
    message = add_message(su, w, 0, status="QUEUED", due_at=datetime.now(UTC) - timedelta(minutes=1))
    pause(session_factory, su, w)

    sender = session_factory()
    try:
        loaded = SendingRepository(sender).load_message_for_send(workspace_id=w.ws, message_id=message)
        assert loaded is not None and loaded.campaign_status == "PAUSED"
        with pytest.raises(gates.CampaignStateRejected):
            gates.check_campaign_state(loaded)
    finally:
        sender.rollback()
        sender.close()
    assert msg(su, message)["status"] == "QUEUED"  # untouched; it returns to the schedule via lease recovery


# ---------------------------------------------------------------------------
# progress survives a pause/resume, lead by lead
# ---------------------------------------------------------------------------


def test_every_leads_progress_and_the_campaigns_history_survive_pause_and_resume(
    session_factory, su, producer
) -> None:
    """Lead A: step 1 sent, step 2 waiting. Lead B: step 1 still to send.
    Lead C: step 1 sent and step 2 sent (sequence finished). A pause that lasts
    longer than the wait, then a resume, must not restart, skip, duplicate or
    rewrite anything that already happened."""
    w = world(su, members=3)
    now = today_at(10)
    sent_at = now - timedelta(days=5)
    a1 = add_message(su, w, 0, status="SENT", due_at=sent_at, anchor_at=sent_at)
    a_enrollment = su.execute("SELECT enrollment_id FROM public.messages WHERE id=%s", [a1]).fetchone()[0]
    a2 = add_message(
        su, w, 0, enrollment=a_enrollment, step=w.step2, due_at=now - timedelta(days=1), anchor_at=sent_at
    )
    b1 = add_message(su, w, 1, due_at=now - timedelta(hours=2))
    c1 = add_message(su, w, 2, status="SENT", due_at=sent_at, anchor_at=sent_at)
    c_enrollment = su.execute("SELECT enrollment_id FROM public.messages WHERE id=%s", [c1]).fetchone()[0]
    c2 = add_message(
        su, w, 2, enrollment=c_enrollment, step=w.step2, status="SENT",
        due_at=sent_at + timedelta(days=2), anchor_at=sent_at + timedelta(days=2),
    )
    ids = [a1, a2, b1, c1, c2]

    def snapshot():
        history = su.execute(
            "SELECT id, status, enrollment_id, step_id, content_digest, accepted_at FROM public.messages "
            "WHERE campaign_id=%s ORDER BY id",
            [w.campaign_id],
        ).fetchall()
        enrollments = su.execute(
            "SELECT id, state, next_step_id FROM public.campaign_enrollments WHERE campaign_id=%s ORDER BY id",
            [w.campaign_id],
        ).fetchall()
        return history, enrollments

    sent_before = su.execute("SELECT count(*) FROM public.messages WHERE status='SENT' AND campaign_id=%s", [w.campaign_id]).fetchone()[0]
    before_history, before_enrollments = snapshot()

    pause(session_factory, su, w)
    su.execute("UPDATE public.campaigns SET paused_at = now() - interval '9 days' WHERE id=%s", [w.campaign_id])
    resume(session_factory, su, w)
    reconcile(session_factory, w, 2, now)

    after_history, after_enrollments = snapshot()
    assert len(after_history) == len(before_history) == 5  # nothing duplicated, nothing lost
    assert after_enrollments == before_enrollments  # nobody restarted, skipped or re-pointed
    # Statuses, content and acceptance times of everything that happened are untouched.
    assert after_history == before_history
    assert su.execute("SELECT count(*) FROM public.messages WHERE status='SENT' AND campaign_id=%s", [w.campaign_id]).fetchone()[0] == sent_before

    # Only the not-yet-sent work was re-timed, and none of it earlier than it may be.
    a2_due, b1_due = msg(su, a2)["due_at"], msg(su, b1)["due_at"]
    assert a2_due >= sent_at + timedelta(minutes=2880)  # step 2 waits 2 days after step 1 went out
    assert b1_due >= now
    assert msg(su, a2)["schedule_generation"] == msg(su, b1)["schedule_generation"] == 2
    for finished in (a1, c1, c2):
        assert msg(su, finished)["schedule_generation"] == 1  # sent history keeps its stamp
    assert len(ids) == 5


def test_supplement_routes_resolve_before_the_generic_audience_id_route(client, su, producer) -> None:
    """GET /audience/supplements must not be swallowed by /audience/{audience_id}
    (which would answer 422 because "supplements" is not a UUID)."""
    w = world(su, status="PAUSED")
    base = f"/api/v1/workspaces/{w.ws}/campaigns/{w.campaign_id}/audience"
    client.holder["as"] = (w, "MANAGER")
    listed = client.get(f"{base}/supplements")
    assert listed.status_code == 200 and listed.json() == []

    client.holder["as"] = (w, "MEMBER")
    assert client.post(f"{base}/supplements", json={"lead_ids": [], "list_ids": []}).status_code == 403
    assert client.post(f"{base}/supplements/{uuid.uuid4()}/enroll").status_code == 403


# ---------------------------------------------------------------------------
# the real send path, against the real database
# ---------------------------------------------------------------------------


def _queued_before_the_pause(su, w: World, *, generation: int = 1):
    """A message the scheduler claimed (QUEUED) before the campaign was paused."""
    message = add_message(
        su, w, 0, status="QUEUED", due_at=datetime.now(UTC) - timedelta(minutes=1), generation=generation
    )
    return message


def _payload(w: World, message, dispatch_generation: int = 1):
    from app.modules.scheduler.schemas import SendTaskPayload

    return SendTaskPayload(
        work_id=str(uuid.uuid4()),
        delivery_id=str(uuid.uuid4()),
        message_id=str(message),
        dispatch_id=str(uuid.uuid4()),
        resource_id=str(message),
        workspace_id=str(w.ws),
        dispatch_generation=dispatch_generation,
    )


def test_a_message_claimed_before_a_pause_is_handed_back_by_the_real_send_path(
    session_factory, su, producer
) -> None:
    """Claimed at generation 1, campaign resumed to generation 2. The real
    SendingService must hand it back (not send it, not skip it for good), and the
    reconcile pass must then re-stamp it so it can be claimed again."""
    from unittest.mock import MagicMock

    from app.core.config import Settings
    from app.modules.sending.service import SendingService

    w = world(su)
    message = _queued_before_the_pause(su, w)
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    session = session_factory()
    try:
        limiter = MagicMock()  # must never be reached: the generation gate is first
        outcome = SendingService(session, settings=Settings.current(), rate_limiter=limiter).execute(
            _payload(w, message)
        )
    finally:
        session.close()

    assert (outcome.outcome, outcome.reason) == ("DEFERRED", "stale_schedule_generation")
    limiter.reserve.assert_not_called()
    after = msg(su, message)
    assert after["status"] == "SCHEDULED"  # back on the schedule, not skipped
    assert after["dispatch_generation"] == 2  # a late duplicate task is now stale
    assert su.execute("SELECT count(*) FROM public.message_attempts WHERE message_id=%s", [message]).fetchone()[0] == 0

    # Still fenced until reconciled...
    far = datetime.now(UTC) + timedelta(days=3)
    assert message not in discoverable(session_factory, w, far)
    reconcile(session_factory, w, 2, today_at(10))
    assert message in discoverable(session_factory, w, far)


def test_the_stale_task_redelivered_after_the_hand_back_is_a_harmless_noop(session_factory, su, producer) -> None:
    from unittest.mock import MagicMock

    from app.core.config import Settings
    from app.modules.sending.service import SendingService

    w = world(su)
    message = _queued_before_the_pause(su, w)
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])
    for expected in ("DEFERRED", "NOOP"):  # the same task delivered twice
        session = session_factory()
        try:
            outcome = SendingService(session, settings=Settings.current(), rate_limiter=MagicMock()).execute(
                _payload(w, message)
            )
        finally:
            session.close()
        assert outcome.outcome == expected
    assert msg(su, message)["status"] == "SCHEDULED"  # not SKIPPED by the duplicate


def test_a_re_rendered_message_passes_the_send_gates_integrity_check(session_factory, su, producer) -> None:
    """The reconcile pass rewrote the body; the send gate recomputes the digest
    from what is stored. They must agree, or the message would be skipped for good."""
    from app.modules.sending import gates
    from app.modules.sending.repository import SendingRepository
    from tests.test_campaign_paused_edits_real_db import EDITED, freeze, update_step

    w = world(su)
    freeze(su, w)
    now = today_at(10)
    message = add_message(su, w, 0, due_at=now + timedelta(hours=1))
    pause(session_factory, su, w)
    update_step(session_factory, su, w, w.step1, email_subject="Rewritten for {{company}}", email_body_html=EDITED)
    resume(session_factory, su, w)
    assert reconcile(session_factory, w, 2, now).rerendered == 1

    sender = session_factory()
    try:
        loaded = SendingRepository(sender).load_message_for_send(workspace_id=w.ws, message_id=message)
        assert loaded is not None and loaded.content_subject == "Rewritten for Acme"
        gates.check_snapshot_integrity(loaded)  # raises SnapshotIntegrityRejected on any mismatch
        gates.check_schedule_generation(loaded)
    finally:
        sender.rollback()
        sender.close()


def test_concurrent_resumes_bump_the_generation_once(session_factory, su, producer) -> None:
    w = world(su, status="PAUSED")
    expected = camp(su, w)["version"]
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker() -> None:
        session, ctx = api(session_factory, w)
        try:
            barrier.wait(timeout=10)
            out = CampaignActivationService(session).resume(ctx, w.campaign_id, ResumeIn(expected_version=expected))
            session.commit()
            results.append(out.status)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert results == ["RUNNING", "RUNNING"]
    assert camp(su, w)["schedule_generation"] == 2  # one bump, not two
    assert audit_count(su, w, "campaign.resume") == 1


# ---------------------------------------------------------------------------
# overdue follow-ups after a long pause: the real progression sweep + reconcile
# ---------------------------------------------------------------------------


def test_the_real_sweeper_and_the_reconcile_pass_share_the_same_free_slots(session_factory, su, producer) -> None:
    """Three leads' first emails were accepted long ago (their follow-ups are overdue
    the moment progression runs after the resume) and a fourth lead's first email was
    still waiting. Together they must respect the mailbox's daily cap: no two passes
    may read the same free slot."""
    from app.core.config import Settings
    from app.db.context import set_transaction_context
    from app.modules.campaigns.pacing import MailboxPacing
    from app.modules.campaigns.progression import ProgressionService
    from app.modules.campaigns.worker_repository import CampaignWorkerRepository

    w = world(su, members=4)
    now = today_at(10)
    long_ago = now - timedelta(days=9)
    for i in range(3):  # accepted long ago -> follow-up due 7 days ago
        add_message(su, w, i, status="SENT", due_at=long_ago, anchor_at=long_ago)
    stale = add_message(su, w, 3, due_at=now - timedelta(hours=2))  # still waiting to be sent
    su.execute("UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s", [w.campaign_id])

    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        summary = ProgressionService(
            CampaignWorkerRepository(session),
            is_suppressed=lambda ws, address_id: False,
            default_pacing=MailboxPacing(daily_cap=2, min_spacing_seconds=60),
            overdue_grace=timedelta(minutes=2),
        ).advance_batch(workspace_id=w.ws, campaign_id=w.campaign_id, limit=100, now=now)
        session.commit()
    finally:
        session.close()
    assert (summary.advanced, summary.spread) == (3, 3)

    # Same mailbox pacing for both passes, as in production (one policy per mailbox).
    reconcile(session_factory, w, 2, now, pacing=MailboxPacing(daily_cap=2, min_spacing_seconds=60))

    rows = su.execute(
        "SELECT due_at, schedule_generation FROM public.messages WHERE campaign_id=%s AND status='SCHEDULED' ORDER BY due_at",
        [w.campaign_id],
    ).fetchall()
    assert len(rows) == 4 and all(due >= now for due, _gen in rows)
    assert all(gen == 2 for _due, gen in rows)  # the sweeper stamped the new generation too
    per_day: dict = {}
    for due, _gen in rows:
        per_day[due.date()] = per_day.get(due.date(), 0) + 1
    assert max(per_day.values()) <= 2  # the cap held across both passes
    assert len(per_day) == 2  # 4 messages, 2 per day
    assert msg(su, stale)["due_at"] >= now and Settings is not None
