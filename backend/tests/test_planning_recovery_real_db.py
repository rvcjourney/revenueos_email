"""Planning-job recovery (ADR-0020 follow-up, migration 0045), against a REAL
PostgreSQL (pgserver).

A failed or lost enroll/render job must be recoverable without hand-editing the
database: an explicit retry (manager-only, tenant-scoped, idempotent) and an
automatic sweep for jobs whose task message was lost. Resume refuses while a job
is FAILED so leads never silently miss their first email. No email is sent.
"""

# ruff: noqa: E402, E501, F401, F811
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.errors import AppError
from app.modules.campaigns.activation_service import CampaignActivationService
from app.modules.campaigns.supplement_service import SupplementService
from app.modules.scheduler.repository import SchedulerRepository
from tests.support.pause_support import api, camp, resume, world
from tests.test_campaign_supplement_real_db import (  # fixtures + helpers
    new_lead,
    paused,
    producer,
    run_capture,
    select,
    session_factory,
    start_enroll,
    su,
    supplements,
    uri,
    workers,
)

pytestmark = pytest.mark.filterwarnings("ignore")


def _job(su, audience_id, phase: str) -> dict:
    cur = su.execute(
        "SELECT state, error_reason, processed_count, lease_owner, lease_expires_at "
        "FROM public.campaign_planning_jobs WHERE audience_id=%s AND phase=%s",
        [audience_id, phase],
    )
    return dict(zip([d.name for d in cur.description], cur.fetchone(), strict=True))


def _set_job(su, audience_id, phase: str, **cols) -> None:
    su.execute("SET session_replication_role = replica")
    sets = ", ".join(
        f"{k} = %s" if not str(v).startswith("SQL:") else f"{k} = {v[4:]}"
        for k, v in cols.items()
    )
    params = [v for v in cols.values() if not str(v).startswith("SQL:")]
    su.execute(
        f"UPDATE public.campaign_planning_jobs SET {sets} WHERE audience_id=%s AND phase=%s",  # noqa: S608
        [*params, audience_id, phase],
    )
    su.execute("SET session_replication_role = origin")


def enrolled(session_factory, su, producer, workers):
    """A paused campaign with one captured supplement whose jobs are PENDING."""
    w = paused(su)
    lead, _ = new_lead(su, w, "alpha")
    out = select(session_factory, w, [lead])
    run_capture(workers, session_factory, w, out.id)
    start_enroll(session_factory, w, out.id)
    return w, out


def stuck(session_factory):
    session = session_factory()
    try:
        return SchedulerRepository(session).find_stuck_planning_jobs(limit=100)
    finally:
        session.rollback()
        session.close()


def mine(found, w):
    return [(a, p) for ws, c, _act, a, p in found if ws == w.ws and c == w.campaign_id]


# ---------------------------------------------------------------------------
# explicit retry
# ---------------------------------------------------------------------------


def test_retry_resets_a_failed_supplement_job_and_runs_it_to_completion(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    _set_job(
        su, out.id, "ENROLL", state="FAILED", error_reason="boom", processed_count=0
    )
    producer.sent.clear()

    session, ctx = api(session_factory, w)
    try:
        shown = SupplementService(session).retry(ctx, w.campaign_id, out.id)
        session.commit()
    finally:
        session.close()
    assert shown.enroll_state == "FAILED"  # the worker, not the API, changes the job
    (name, kwargs, _) = next(
        t for t in producer.sent if t[0] == "campaigns.retry_planning_job"
    )
    assert kwargs["audience_id"] == str(out.id)

    workers.retry_planning_job.apply(kwargs=kwargs).get()
    job = _job(su, out.id, "ENROLL")
    assert (job["state"], job["error_reason"], job["lease_owner"]) == (
        "PENDING",
        None,
        None,
    )
    enroll_kwargs = next(
        k for n, k in workers.sent if n == "campaigns.enroll_activation_chunk"
    )
    workers.sent.clear()

    workers.enroll_activation_chunk.apply(kwargs=enroll_kwargs).get()
    render_kwargs = next(
        k for n, k in workers.sent if n == "campaigns.render_messages_chunk"
    )
    workers.render_messages_chunk.apply(kwargs=render_kwargs).get()
    (result,) = supplements(session_factory, w)
    assert (result.enroll_state, result.render_state, result.enrolled_count) == (
        "READY",
        "READY",
        1,
    )
    assert (
        su.execute(
            "SELECT count(*) FROM public.messages WHERE campaign_id=%s", [w.campaign_id]
        ).fetchone()[0]
        == 1
    )


def test_retry_is_idempotent_and_a_repeat_changes_nothing(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    _set_job(su, out.id, "ENROLL", state="FAILED", error_reason="boom")
    kwargs = {
        "workspace_id": str(w.ws),
        "campaign_id": str(w.campaign_id),
        "activation_id": str(
            su.execute(
                "SELECT activation_id FROM public.campaigns WHERE id=%s",
                [w.campaign_id],
            ).fetchone()[0]
        ),
        "audience_id": str(out.id),
    }
    workers.retry_planning_job.apply(kwargs=kwargs).get()
    assert [n for n, _ in workers.sent].count("campaigns.enroll_activation_chunk") == 1
    workers.sent.clear()

    workers.retry_planning_job.apply(kwargs=kwargs).get()  # redelivered
    assert workers.sent == []
    assert _job(su, out.id, "ENROLL")["state"] == "PENDING"

    # Nothing failed -> the API does not even dispatch.
    producer.sent.clear()
    session, ctx = api(session_factory, w)
    try:
        SupplementService(session).retry(ctx, w.campaign_id, out.id)
    finally:
        session.close()
    assert not any(n == "campaigns.retry_planning_job" for n, *_ in producer.sent)


def test_a_failed_render_alone_is_retried_as_render(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    _set_job(su, out.id, "ENROLL", state="READY", processed_count=1)
    _set_job(su, out.id, "RENDER", state="FAILED", error_reason="boom")
    kwargs = {
        "workspace_id": str(w.ws),
        "campaign_id": str(w.campaign_id),
        "activation_id": str(
            su.execute(
                "SELECT activation_id FROM public.campaigns WHERE id=%s",
                [w.campaign_id],
            ).fetchone()[0]
        ),
        "audience_id": str(out.id),
    }
    workers.retry_planning_job.apply(kwargs=kwargs).get()
    assert [n for n, _ in workers.sent] == ["campaigns.render_messages_chunk"]
    assert _job(su, out.id, "RENDER")["state"] == "PENDING"
    assert _job(su, out.id, "ENROLL")["state"] == "READY"  # untouched


def test_retry_needs_a_manager_and_stays_inside_the_workspace(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    _set_job(su, out.id, "ENROLL", state="FAILED", error_reason="boom")

    session, ctx = api(session_factory, w, "MEMBER")
    try:
        with pytest.raises(AppError) as info:
            SupplementService(session).retry(ctx, w.campaign_id, out.id)
    finally:
        session.close()
    assert info.value.status_code == 403

    other = world(su)
    session, ctx = api(session_factory, other)  # a manager of ANOTHER workspace
    try:
        with pytest.raises(AppError) as info:
            SupplementService(session).retry(ctx, w.campaign_id, out.id)
        assert info.value.status_code == 404
        with pytest.raises(AppError) as info:
            CampaignActivationService(session).retry_planning(ctx, w.campaign_id)
        assert info.value.status_code == 404
    finally:
        session.close()
    assert _job(su, out.id, "ENROLL")["state"] == "FAILED"


def test_retry_supplement_only_while_paused(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    _set_job(su, out.id, "ENROLL", state="FAILED", error_reason="boom")
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaigns SET status='RUNNING' WHERE id=%s", [w.campaign_id]
    )
    su.execute("SET session_replication_role = origin")
    session, ctx = api(session_factory, w)
    try:
        with pytest.raises(AppError) as info:
            SupplementService(session).retry(ctx, w.campaign_id, out.id)
    finally:
        session.close()
    assert info.value.status_code == 409


def test_activation_retry_dispatches_the_worker_task_and_audits(
    session_factory, su, producer
) -> None:
    w = paused(su)
    producer.sent.clear()
    session, ctx = api(session_factory, w)
    try:
        CampaignActivationService(session).retry_planning(ctx, w.campaign_id)
    finally:
        session.close()
    (name, kwargs, _) = next(
        t for t in producer.sent if t[0] == "campaigns.retry_planning_job"
    )
    assert "audience_id" not in kwargs
    assert (
        su.execute(
            "SELECT count(*) FROM public.audit_events WHERE target_id=%s AND action='campaign.planning_retry'",
            [w.campaign_id],
        ).fetchone()[0]
        == 1
    )


def test_resetting_the_activations_own_job_leaves_supplement_jobs_alone(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    _set_job(su, out.id, "ENROLL", state="FAILED", error_reason="boom")
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaign_planning_jobs SET state='FAILED', error_reason='own' "
        "WHERE campaign_id=%s AND audience_id IS NULL AND phase='ENROLL'",
        [w.campaign_id],
    )
    su.execute("SET session_replication_role = origin")
    activation = su.execute(
        "SELECT activation_id FROM public.campaigns WHERE id=%s", [w.campaign_id]
    ).fetchone()[0]

    workers.retry_planning_job.apply(
        kwargs={
            "workspace_id": str(w.ws),
            "campaign_id": str(w.campaign_id),
            "activation_id": str(activation),
        }
    ).get()

    own = su.execute(
        "SELECT state FROM public.campaign_planning_jobs WHERE campaign_id=%s AND audience_id IS NULL AND phase='ENROLL'",
        [w.campaign_id],
    ).fetchone()[0]
    assert own == "PENDING"
    assert _job(su, out.id, "ENROLL")["state"] == "FAILED"


# ---------------------------------------------------------------------------
# resume refuses while a job is FAILED
# ---------------------------------------------------------------------------


def test_resume_is_blocked_while_a_planning_job_is_failed_and_allowed_after_the_retry(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    enroll_kwargs = next(
        k for n, k, _ in producer.sent if n == "campaigns.enroll_activation_chunk"
    )
    _set_job(su, out.id, "ENROLL", state="FAILED", error_reason="boom")

    with pytest.raises(AppError) as info:
        resume(session_factory, su, w)
    assert info.value.code == "resume_blocked"
    assert "planning_failed" in {e["code"] for e in info.value.details["errors"]}
    assert camp(su, w)["status"] == "PAUSED"

    workers.retry_planning_job.apply(
        kwargs={**enroll_kwargs, "activation_id": enroll_kwargs["activation_id"]}
    ).get()
    workers.enroll_activation_chunk.apply(kwargs=enroll_kwargs).get()
    render_kwargs = next(
        k for n, k in workers.sent if n == "campaigns.render_messages_chunk"
    )
    workers.render_messages_chunk.apply(kwargs=render_kwargs).get()
    resume(session_factory, su, w)
    assert camp(su, w)["status"] == "RUNNING"


# ---------------------------------------------------------------------------
# automatic sweep
# ---------------------------------------------------------------------------


def test_sweep_ignores_fresh_jobs_and_finds_old_pending_ones(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    assert (
        mine(stuck(session_factory), w) == []
    )  # just created: its task is surely in flight

    _set_job(su, out.id, "ENROLL", next_due_at="SQL:now() - interval '5 minutes'")
    assert mine(stuck(session_factory), w) == [(out.id, "ENROLL")]


def test_sweep_never_offers_render_before_enroll_is_ready(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    _set_job(su, out.id, "RENDER", next_due_at="SQL:now() - interval '5 minutes'")
    assert mine(stuck(session_factory), w) == []  # ENROLL is still PENDING

    _set_job(su, out.id, "ENROLL", state="READY", processed_count=1)
    assert mine(stuck(session_factory), w) == [(out.id, "RENDER")]


def test_sweep_takes_over_expired_leases_only(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    _set_job(
        su,
        out.id,
        "ENROLL",
        state="PROCESSING",
        lease_owner="worker-a",
        lease_expires_at="SQL:now() + interval '10 minutes'",
        next_due_at="SQL:now() - interval '5 minutes'",
    )
    assert mine(stuck(session_factory), w) == []  # a live worker holds it

    _set_job(su, out.id, "ENROLL", lease_expires_at="SQL:now() - interval '1 minute'")
    assert mine(stuck(session_factory), w) == [(out.id, "ENROLL")]


def test_sweep_skips_failed_and_ready_jobs(
    session_factory, su, producer, workers
) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    old = "SQL:now() - interval '5 minutes'"
    _set_job(su, out.id, "ENROLL", state="FAILED", error_reason="x", next_due_at=old)
    assert mine(stuck(session_factory), w) == []  # failures wait for an explicit retry

    _set_job(su, out.id, "ENROLL", state="READY")
    assert mine(stuck(session_factory), w) == []


def test_a_swept_task_is_safe_to_run_twice(
    session_factory, su, producer, workers
) -> None:
    """The sweep may re-publish a job that is actually fine: the claim + idempotent
    chunk make the second run a no-op."""
    w, out = enrolled(session_factory, su, producer, workers)
    kwargs = next(
        k for n, k, _ in producer.sent if n == "campaigns.enroll_activation_chunk"
    )
    workers.enroll_activation_chunk.apply(kwargs=kwargs).get()
    workers.enroll_activation_chunk.apply(kwargs=kwargs).get()
    assert (
        su.execute(
            "SELECT count(*) FROM public.campaign_enrollments WHERE campaign_id=%s",
            [w.campaign_id],
        ).fetchone()[0]
        == 1
    )


def test_render_task_waits_for_enroll(session_factory, su, producer, workers) -> None:
    w, out = enrolled(session_factory, su, producer, workers)
    activation = str(
        su.execute(
            "SELECT activation_id FROM public.campaigns WHERE id=%s", [w.campaign_id]
        ).fetchone()[0]
    )
    workers.render_messages_chunk.apply(
        kwargs={
            "workspace_id": str(w.ws),
            "campaign_id": str(w.campaign_id),
            "activation_id": activation,
            "audience_id": str(out.id),
        }
    ).get()
    assert (
        _job(su, out.id, "RENDER")["state"] == "PENDING"
    )  # not claimed, not finished early


def test_the_scheduler_role_is_read_only_on_planning_jobs(su) -> None:
    for privilege in ("INSERT", "DELETE"):
        assert not su.execute(
            "SELECT has_table_privilege('app_scheduler', 'public.campaign_planning_jobs', %s)",
            [privilege],
        ).fetchone()[0]
    assert not su.execute(
        "SELECT has_any_column_privilege('app_scheduler', 'public.campaign_planning_jobs', 'UPDATE')"
    ).fetchone()[0]
