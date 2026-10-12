"""Adding leads to a PAUSED, activated campaign -- ADR-0020 / migration 0043,
against a REAL PostgreSQL (pgserver).

Runs the real capture / enroll / render worker tasks (eagerly, against the
throwaway database) so the supplement path is proven end to end: capture with
exclusions (already enrolled, suppressed, archived), enrollment through the
relaxed database guard, planning-job isolation from the activation's own jobs,
the resume gate, replay safety and tenant isolation. No email is sent.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # `workers` lives next to `backend`
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.errors import AppError
from app.modules.campaigns.activation_service import CampaignActivationService
from app.modules.campaigns.schemas import AudienceSelectIn
from app.modules.campaigns.supplement_service import SupplementService
from tests.support.pause_support import (
    FakeProducer,
    add_message,
    api,
    camp,
    discoverable,
    draft_world,
    enroll,
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
    monkeypatch.setattr("app.modules.campaigns.preflight.unsubscribe_ready", lambda s: True)
    return fake


@pytest.fixture()
def workers(monkeypatch, session_factory):
    """The real worker module, pointed at the throwaway database, with the
    broker hop recorded instead of sent."""
    import workers.campaigns as module

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(module, "SessionLocal", session_factory)
    monkeypatch.setattr(
        module.celery_app, "send_task", lambda name, kwargs=None, **o: sent.append((name, kwargs or {}))
    )
    module.sent = sent  # type: ignore[attr-defined]
    return module


def new_lead(su, w: World, name: str, *, status: str = "ACTIVE") -> tuple[uuid.UUID, uuid.UUID]:
    lead, address = uuid.uuid4(), uuid.uuid4()
    email = f"{name}@supplement.test"
    su.execute("SET session_replication_role = replica")
    _insert(su, "recipient_addresses", id=address, workspace_id=w.ws, canonical_address=email)
    extra = {"archived_at": datetime.now(UTC)} if status == "ARCHIVED" else {}
    _insert(
        su, "leads", id=lead, workspace_id=w.ws, original_address=email, canonical_address=email,
        first_name=name.title(), company=f"{name.title()} Inc", status=status, **extra,
    )
    su.execute("SET session_replication_role = origin")
    return lead, address


def suppress(su, w: World, address: uuid.UUID) -> None:
    su.execute("SET session_replication_role = replica")
    _insert(su, "suppressions", workspace_id=w.ws, address_id=address, reason="MANUAL", status="ACTIVE")
    su.execute("SET session_replication_role = origin")


def paused(su) -> World:
    w = world(su, status="PAUSED", members=3)
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaign_sequences SET status='FROZEN', frozen_at=now(), content_digest=%s WHERE id=%s",
        ["a" * 64, w.sequence_id],
    )
    # the activation's own planning jobs, finished long ago
    for phase in ("ENROLL", "RENDER"):
        _insert(
            su, "campaign_planning_jobs", workspace_id=w.ws, campaign_id=w.campaign_id,
            activation_id=su.execute("SELECT activation_id FROM public.campaigns WHERE id=%s", [w.campaign_id]).fetchone()[0],
            sequence_id=w.sequence_id, phase=phase, state="READY", total_count=3, processed_count=3,
        )
    su.execute("SET session_replication_role = origin")
    return w


def manager(session_factory, w: World, role: str = "MANAGER"):
    session, ctx = api(session_factory, w, role)
    return session, ctx


def select(session_factory, w: World, leads: list[uuid.UUID], role: str = "MANAGER"):
    session, ctx = manager(session_factory, w, role)
    try:
        out = SupplementService(session).select(ctx, w.campaign_id, AudienceSelectIn(lead_ids=leads))
        return out
    finally:
        session.close()


def run_capture(workers, session_factory, w: World, audience_id) -> None:
    workers.capture_audience_chunk.apply(
        kwargs={"workspace_id": str(w.ws), "campaign_id": str(w.campaign_id), "audience_id": str(audience_id)}
    ).get()


def supplements(session_factory, w: World, role: str = "MANAGER"):
    session, ctx = manager(session_factory, w, role)
    try:
        return SupplementService(session).list_supplements(ctx, w.campaign_id)
    finally:
        session.close()


def start_enroll(session_factory, w: World, audience_id, role: str = "MANAGER"):
    session, ctx = manager(session_factory, w, role)
    try:
        return SupplementService(session).enroll(ctx, w.campaign_id, audience_id)
    finally:
        session.close()


def run_planning(workers, w: World, audience_id) -> None:
    """Enroll then render, exactly as the broker would chain them."""
    activation = None
    for name, kwargs in list(workers.sent):
        if name == "campaigns.render_messages_chunk":
            activation = kwargs
    workers.sent.clear()
    return activation


# ---------------------------------------------------------------------------


def test_capture_excludes_leads_already_in_the_campaign_and_unsendable_ones(session_factory, su, producer, workers) -> None:
    w = paused(su)
    enroll(su, w, 0)  # lead0 is already enrolled
    fresh, _ = new_lead(su, w, "fresh")
    sup_lead, sup_addr = new_lead(su, w, "blocked")
    suppress(su, w, sup_addr)
    arch, _ = new_lead(su, w, "gone", status="ARCHIVED")

    out = select(session_factory, w, [w.lead_ids[0], fresh, sup_lead, arch])
    assert out.status == "CAPTURING"
    run_capture(workers, session_factory, w, out.id)

    (result,) = supplements(session_factory, w)
    assert result.status == "READY"
    assert (result.accepted_count, result.excluded_count) == (1, 3)
    assert result.exclusions == {"already_enrolled": 1, "suppressed": 1, "archived_lead": 1}
    kinds = su.execute("SELECT kind FROM public.campaign_audiences WHERE id=%s", [out.id]).fetchone()[0]
    assert kinds == "SUPPLEMENT"


def test_supplement_is_enrolled_through_the_real_planner_without_touching_the_original_audience(
    session_factory, su, producer, workers
) -> None:
    w = paused(su)
    a, _ = new_lead(su, w, "alpha")
    b, _ = new_lead(su, w, "bravo")
    out = select(session_factory, w, [a, b])
    run_capture(workers, session_factory, w, out.id)
    original_before = su.execute(
        "SELECT status, (SELECT count(*) FROM public.campaign_audience_members WHERE audience_id=%s) "
        "FROM public.campaign_audiences WHERE id=%s",
        [w.audience_id, w.audience_id],
    ).fetchone()

    started = start_enroll(session_factory, w, out.id)
    assert started.enroll_state == "PENDING" and started.render_state == "PENDING"
    (name, kwargs) = next((n, k) for n, k, _ in producer.sent if n == "campaigns.enroll_activation_chunk")
    assert kwargs["audience_id"] == str(out.id)

    workers.enroll_activation_chunk.apply(kwargs=kwargs).get()
    (render_name, render_kwargs) = next((n, k) for n, k in workers.sent if n == "campaigns.render_messages_chunk")
    assert render_kwargs["audience_id"] == str(out.id)
    workers.render_messages_chunk.apply(kwargs=render_kwargs).get()

    (result,) = supplements(session_factory, w)
    assert (result.enroll_state, result.render_state, result.enrolled_count) == ("READY", "READY", 2)
    rows = su.execute(
        "SELECT e.audience_id, e.state, m.status, m.schedule_generation, m.due_at IS NOT NULL "
        "FROM public.campaign_enrollments e JOIN public.messages m ON m.enrollment_id = e.id "
        "WHERE e.campaign_id=%s",
        [w.campaign_id],
    ).fetchall()
    assert len(rows) == 2
    assert all(r[0] == out.id and r[1] == "ACTIVE" and r[2] == "SCHEDULED" and r[4] for r in rows)
    assert all(r[3] == camp(su, w)["schedule_generation"] for r in rows)
    # The original audience and the campaign's own planning state are untouched.
    assert su.execute(
        "SELECT status, (SELECT count(*) FROM public.campaign_audience_members WHERE audience_id=%s) "
        "FROM public.campaign_audiences WHERE id=%s",
        [w.audience_id, w.audience_id],
    ).fetchone() == original_before
    assert su.execute("SELECT planning_status FROM public.campaigns WHERE id=%s", [w.campaign_id]).fetchone()[0] == "READY"


def test_enroll_is_idempotent_and_replayed_tasks_create_nothing_new(session_factory, su, producer, workers) -> None:
    w = paused(su)
    a, _ = new_lead(su, w, "alpha")
    out = select(session_factory, w, [a])
    run_capture(workers, session_factory, w, out.id)

    first = start_enroll(session_factory, w, out.id)
    again = start_enroll(session_factory, w, out.id)
    assert (first.enroll_state, again.enroll_state) == ("PENDING", "PENDING")
    assert su.execute(
        "SELECT count(*) FROM public.campaign_planning_jobs WHERE audience_id=%s", [out.id]
    ).fetchone()[0] == 3  # capture + enroll + render, not doubled
    assert sum(1 for n, _, _ in producer.sent if n == "campaigns.enroll_activation_chunk") == 1

    kwargs = next(k for n, k, _ in producer.sent if n == "campaigns.enroll_activation_chunk")
    workers.enroll_activation_chunk.apply(kwargs=kwargs).get()
    workers.enroll_activation_chunk.apply(kwargs=kwargs).get()  # redelivered
    assert su.execute("SELECT count(*) FROM public.campaign_enrollments WHERE campaign_id=%s", [w.campaign_id]).fetchone()[0] == 1
    assert su.execute("SELECT count(*) FROM public.messages WHERE campaign_id=%s", [w.campaign_id]).fetchone()[0] == 1


def test_resume_waits_for_unfinished_additions_then_sends_the_new_leads_spread_out(
    session_factory, su, producer, workers
) -> None:
    w = paused(su)
    leads = [new_lead(su, w, f"lead{i}")[0] for i in range(3)]
    out = select(session_factory, w, leads)
    run_capture(workers, session_factory, w, out.id)
    start_enroll(session_factory, w, out.id)

    with pytest.raises(AppError) as info:
        resume(session_factory, su, w)
    assert info.value.code == "resume_blocked"
    assert {e["code"] for e in info.value.details["errors"]} == {"lead_additions_in_progress"}
    assert camp(su, w)["status"] == "PAUSED"

    enroll_kwargs = next(k for n, k, _ in producer.sent if n == "campaigns.enroll_activation_chunk")
    workers.enroll_activation_chunk.apply(kwargs=enroll_kwargs).get()
    render_kwargs = next(k for n, k in workers.sent if n == "campaigns.render_messages_chunk")
    workers.render_messages_chunk.apply(kwargs=render_kwargs).get()

    resume(session_factory, su, w)
    now = today_at(10)
    summary = reconcile(session_factory, w, 2, now)
    due = sorted(r[0] for r in su.execute("SELECT due_at FROM public.messages WHERE campaign_id=%s", [w.campaign_id]).fetchall())
    assert summary.restamped == 3 and len(due) == 3
    assert due[0] < due[1] < due[2]  # one slot each, not one instant
    ids = {r[0] for r in su.execute("SELECT id FROM public.messages WHERE campaign_id=%s", [w.campaign_id]).fetchall()}
    assert ids <= discoverable(session_factory, w, datetime.now(UTC) + timedelta(days=3))


def test_activation_planning_status_ignores_supplement_jobs(session_factory, su, producer, workers) -> None:
    w = paused(su)
    a, _ = new_lead(su, w, "alpha")
    out = select(session_factory, w, [a])
    run_capture(workers, session_factory, w, out.id)
    start_enroll(session_factory, w, out.id)

    session, ctx = api(session_factory, w, "MANAGER")
    try:
        planning = CampaignActivationService(session).get_planning_status(ctx, w.campaign_id)
    finally:
        session.close()
    assert planning.enroll is not None and planning.enroll.total_count == 3  # the activation's own job
    assert planning.render is not None and planning.render.total_count == 3


# ---------------------------------------------------------------------------
# who and when
# ---------------------------------------------------------------------------


def test_member_cannot_add_leads(session_factory, su, producer) -> None:
    w = paused(su)
    a, _ = new_lead(su, w, "alpha")
    with pytest.raises(AppError) as info:
        select(session_factory, w, [a], role="MEMBER")
    assert info.value.status_code == 403
    session, _ctx = api(session_factory, w, "MEMBER")
    try:
        with pytest.raises(Exception, match="row-level security"):  # and RLS agrees
            session.execute(
                text(
                    "INSERT INTO public.campaign_audiences (id, workspace_id, campaign_id, revision, status, kind, selection_manifest) "
                    "VALUES (:id, :ws, :cid, 9, 'CAPTURING', 'SUPPLEMENT', '{}'::jsonb)"
                ),
                {"id": str(uuid.uuid4()), "ws": str(w.ws), "cid": str(w.campaign_id)},
            )
    finally:
        session.rollback()
        session.close()


@pytest.mark.parametrize("status", ["RUNNING", "COMPLETED"])
def test_leads_can_only_be_added_while_paused(session_factory, su, producer, status) -> None:
    w = paused(su)
    su.execute("SET session_replication_role = replica")
    su.execute("UPDATE public.campaigns SET status=%s WHERE id=%s", [status, w.campaign_id])
    su.execute("SET session_replication_role = origin")
    a, _ = new_lead(su, w, "alpha")
    with pytest.raises(AppError) as info:
        select(session_factory, w, [a])
    assert info.value.status_code == 409


def insert_supplement_audience(session_factory, w: World) -> None:
    session, _ctx = api(session_factory, w, "MANAGER")
    try:
        session.execute(
            text(
                "INSERT INTO public.campaign_audiences (id, workspace_id, campaign_id, revision, status, kind, selection_manifest) "
                "VALUES (:id, :ws, :cid, 9, 'CAPTURING', 'SUPPLEMENT', "
                "'{\"version\": 1, \"lists\": [], \"leads\": []}'::jsonb)"
            ),
            {"id": str(uuid.uuid4()), "ws": str(w.ws), "cid": str(w.campaign_id)},
        )
        session.commit()
    finally:
        session.rollback()
        session.close()


def test_the_database_only_accepts_a_supplement_audience_on_a_paused_campaign(session_factory, su, producer) -> None:
    insert_supplement_audience(session_factory, paused(su))  # PAUSED + execute: fine
    with pytest.raises(Exception, match="row-level security"):
        insert_supplement_audience(session_factory, draft_world(su))  # DRAFT: not a supplement
    running = world(su)
    with pytest.raises(Exception, match="row-level security"):
        insert_supplement_audience(session_factory, running)


def test_enrollment_guard_rejects_a_supplement_unless_the_campaign_is_paused(session_factory, su, producer, workers) -> None:
    w = paused(su)
    a, _ = new_lead(su, w, "alpha")
    out = select(session_factory, w, [a])
    run_capture(workers, session_factory, w, out.id)
    member = su.execute(
        "SELECT id, lead_id, address_id, frozen_variables FROM public.campaign_audience_members WHERE audience_id=%s", [out.id]
    ).fetchone()
    su.execute("SET session_replication_role = replica")
    su.execute("UPDATE public.campaigns SET status='RUNNING' WHERE id=%s", [w.campaign_id])
    su.execute("SET session_replication_role = origin")
    address = su.execute("SELECT canonical_address FROM public.recipient_addresses WHERE id=%s", [member[2]]).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation, match="activated audience"):
        _insert(
            su, "campaign_enrollments", id=uuid.uuid4(), workspace_id=w.ws, campaign_id=w.campaign_id,
            audience_id=out.id, audience_member_id=member[0], sequence_id=w.sequence_id, lead_id=member[1],
            address_id=member[2], frozen_destination=address, state="ACTIVE", next_step_id=w.step1,
            next_sequence_position=1, assigned_mailbox_id=w.mailbox_id, frozen_variables=member[3],
        )


def test_a_lead_can_never_be_enrolled_twice_even_with_triggers_out_of_the_way(session_factory, su, producer) -> None:
    """The unique (campaign, lead) / (campaign, address) keys are the backstop under
    the guard and the capture-time exclusion: even a writer that bypasses both
    (replica mode skips triggers) cannot enroll the same lead again."""
    w = paused(su)
    enroll(su, w, 0)
    su.execute("SET session_replication_role = replica")
    try:
        with pytest.raises(psycopg.errors.UniqueViolation):
            su.execute(
                "INSERT INTO public.campaign_enrollments (id, workspace_id, campaign_id, audience_id, audience_member_id, "
                "sequence_id, lead_id, address_id, frozen_destination, state, next_step_id, next_sequence_position, "
                "assigned_mailbox_id, frozen_variables) SELECT gen_random_uuid(), workspace_id, campaign_id, audience_id, "
                "audience_member_id, sequence_id, lead_id, address_id, frozen_destination, state, next_step_id, "
                "next_sequence_position, assigned_mailbox_id, frozen_variables FROM public.campaign_enrollments "
                "WHERE campaign_id = %s",
                [w.campaign_id],
            )
    finally:
        su.execute("SET session_replication_role = origin")


def test_other_tenants_leads_and_campaigns_are_invisible(session_factory, su, producer) -> None:
    mine, theirs = paused(su), paused(su)
    their_lead, _ = new_lead(su, theirs, "theirs")
    with pytest.raises(AppError) as info:  # their lead id is "not found" in my workspace
        select(session_factory, mine, [their_lead])
    assert info.value.status_code == 422
    session, ctx = api(session_factory, mine, "MANAGER")
    try:
        with pytest.raises(AppError) as info:
            SupplementService(session).select(ctx, theirs.campaign_id, AudienceSelectIn(lead_ids=[their_lead]))
        assert info.value.status_code == 404
    finally:
        session.rollback()
        session.close()
    assert pause is not None and msg is not None and add_message is not None


def test_only_a_manager_can_cancel_a_supplement_capture(session_factory, su, producer) -> None:
    from app.modules.campaigns.audience_service import AudienceService

    w = paused(su)
    a, _ = new_lead(su, w, "alpha")
    out = select(session_factory, w, [a])  # CAPTURING: the worker has not run

    session, ctx = api(session_factory, w, "MEMBER")
    try:
        with pytest.raises(AppError) as info:
            AudienceService(session).abandon_audience(ctx, w.campaign_id, out.id)
        assert info.value.status_code == 403
    finally:
        session.rollback()
        session.close()
    before = len([n for n, _k, _o in producer.sent if n == "campaigns.capture_audience_chunk"])

    session, ctx = api(session_factory, w, "MANAGER")
    try:
        AudienceService(session).abandon_audience(ctx, w.campaign_id, out.id)
    finally:
        session.close()
    after = len([n for n, _k, _o in producer.sent if n == "campaigns.capture_audience_chunk"])
    assert after == before + 1
