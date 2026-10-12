"""Opt-in reopen of finished leads at resume (ADR-0020), against a REAL PostgreSQL.

A lead that FINISHED the sequence before the pause is given the email appended while
the campaign was paused -- only when the user asked at resume, and only if the lead is
clean. Proves who is reopened and who never is, replay safety, the default (nothing
reopens) and that exactly one appended email results per lead. No email is sent.
"""

# ruff: noqa: E402, E501, F401, F811
from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.errors import AppError
from app.db.context import set_transaction_context
from app.modules.campaigns.activation_service import CampaignActivationService
from app.modules.campaigns.pacing import MailboxPacing
from app.modules.campaigns.progression import ProgressionService
from app.modules.campaigns.schemas import ResumeIn
from app.modules.campaigns.worker_repository import CampaignWorkerRepository
from tests.support.pause_support import (
    add_message,
    api,
    audit_count,
    camp,
    enroll,
    today_at,
)
from tests.support.real_seed import _insert
from tests.test_campaign_paused_edits_real_db import (  # fixtures + helpers
    add_pair,
    paused_world,
    producer,
    session_factory,
    su,
    uri,
)

pytestmark = pytest.mark.filterwarnings("ignore")

LONG_AGO = datetime.now(UTC) - timedelta(days=20)


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


def finished_lead(su, w, i: int, *, last_status: str = "SENT") -> uuid.UUID:
    """A lead that was sent step 1 and step 3 (the end of E-W-E) and COMPLETED."""
    eid = enroll(su, w, i)
    add_message(
        su,
        w,
        i,
        enrollment=eid,
        step=w.step1,
        status="SENT",
        due_at=LONG_AGO - timedelta(days=3),
    )
    last = add_message(
        su,
        w,
        i,
        enrollment=eid,
        step=w.step2,
        status="SENT" if last_status == "SENT" else "SCHEDULED",
        due_at=LONG_AGO,
        anchor_at=LONG_AGO,
    )
    if last_status != "SENT":
        su.execute("SET session_replication_role = replica")
        su.execute(
            "UPDATE public.messages SET status=%s, terminal_reason='provider rejected' WHERE id=%s",
            [last_status, last],
        )
        su.execute("SET session_replication_role = origin")
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaign_enrollments SET state='COMPLETED', next_step_id=NULL, next_sequence_position=NULL WHERE id=%s",
        [eid],
    )
    su.execute("SET session_replication_role = origin")
    return eid


def state(su, enrollment) -> tuple:
    return su.execute(
        "SELECT state, next_step_id, next_sequence_position FROM public.campaign_enrollments WHERE id=%s",
        [enrollment],
    ).fetchone()


def paused_with_appended_email(session_factory, su):
    """E(1) W(2) E(3), paused, with W(4) E(5) appended while paused."""
    w = paused_world(su, reached=False)
    add_pair(session_factory, w, 4, subject="The new last email")
    return w


def resume_with(session_factory, su, w, *, include_completed: bool):
    session, ctx = api(session_factory, w)
    try:
        out = CampaignActivationService(session).resume(
            ctx,
            w.campaign_id,
            ResumeIn(
                expected_version=camp(su, w)["version"],
                include_completed=include_completed,
            ),
        )
        session.commit()
        return out
    finally:
        session.close()


def run_reopen(workers, w, generation: int | None = None):
    workers.reopen_completed_enrollments_chunk.apply(
        kwargs={
            "workspace_id": str(w.ws),
            "campaign_id": str(w.campaign_id),
            "generation": generation or 2,
        }
    ).get()


def reopen_tasks(producer) -> list[dict]:
    return [
        k
        for n, k, _ in producer.sent
        if n == "campaigns.reopen_completed_enrollments_chunk"
    ]


# ---------------------------------------------------------------------------


def test_default_resume_reopens_nothing(session_factory, su, producer, workers) -> None:
    w = paused_with_appended_email(session_factory, su)
    lead = finished_lead(su, w, 0)

    resume_with(session_factory, su, w, include_completed=False)

    assert reopen_tasks(producer) == []
    assert state(su, lead)[0] == "COMPLETED"
    assert audit_count(su, w, "campaign.reopen_completed_requested") == 0


def test_opt_in_reopens_a_clean_finished_lead_at_its_last_sent_email(
    session_factory, su, producer, workers
) -> None:
    w = paused_with_appended_email(session_factory, su)
    lead = finished_lead(su, w, 0)

    resume_with(session_factory, su, w, include_completed=True)
    (kwargs,) = reopen_tasks(producer)
    assert kwargs["generation"] == camp(su, w)["schedule_generation"]
    assert audit_count(su, w, "campaign.reopen_completed_requested") == 1

    workers.reopen_completed_enrollments_chunk.apply(kwargs=kwargs).get()
    assert state(su, lead) == (
        "ACTIVE",
        w.step2,
        3,
    )  # pointing at the email it was already sent


def test_leads_that_must_not_be_contacted_again_are_never_reopened(
    session_factory, su, producer, workers
) -> None:
    w = paused_with_appended_email(session_factory, su)
    clean = finished_lead(su, w, 0)
    replied = finished_lead(su, w, 1)
    failed_last = finished_lead(su, w, 2, last_status="FAILED")
    su.execute("SET session_replication_role = replica")
    _insert(
        su,
        "recipient_outcomes",
        workspace_id=w.ws,
        enrollment_id=replied,
        kind="REPLIED",
        source_key="r1",
        occurred_at=datetime.now(UTC),
    )
    su.execute("SET session_replication_role = origin")

    resume_with(session_factory, su, w, include_completed=True)
    workers.reopen_completed_enrollments_chunk.apply(
        kwargs=reopen_tasks(producer)[0]
    ).get()

    assert state(su, clean)[0] == "ACTIVE"
    assert state(su, replied)[0] == "COMPLETED"
    assert state(su, failed_last)[0] == "COMPLETED"


def test_a_suppressed_lead_is_not_reopened(
    session_factory, su, producer, workers
) -> None:
    w = paused_with_appended_email(session_factory, su)
    lead = finished_lead(su, w, 0)
    su.execute("SET session_replication_role = replica")
    _insert(
        su,
        "suppressions",
        workspace_id=w.ws,
        address_id=w.address_ids[0],
        reason="MANUAL",
        status="ACTIVE",
    )
    su.execute("SET session_replication_role = origin")

    resume_with(session_factory, su, w, include_completed=True)
    workers.reopen_completed_enrollments_chunk.apply(
        kwargs=reopen_tasks(producer)[0]
    ).get()
    assert state(su, lead)[0] == "COMPLETED"


def test_a_lead_with_nothing_appended_after_it_is_not_reopened(
    session_factory, su, producer, workers
) -> None:
    w = paused_world(su, reached=False)  # nothing appended
    lead = finished_lead(su, w, 0)
    resume_with(session_factory, su, w, include_completed=True)
    workers.reopen_completed_enrollments_chunk.apply(
        kwargs=reopen_tasks(producer)[0]
    ).get()
    assert state(su, lead)[0] == "COMPLETED"


def test_replaying_the_task_changes_nothing_and_a_stale_generation_is_ignored(
    session_factory, su, producer, workers
) -> None:
    w = paused_with_appended_email(session_factory, su)
    lead = finished_lead(su, w, 0)
    resume_with(session_factory, su, w, include_completed=True)
    kwargs = reopen_tasks(producer)[0]

    workers.reopen_completed_enrollments_chunk.apply(kwargs=kwargs).get()
    after_first = state(su, lead)
    workers.reopen_completed_enrollments_chunk.apply(kwargs=kwargs).get()  # redelivered
    assert state(su, lead) == after_first

    other = finished_lead(su, w, 1)
    workers.reopen_completed_enrollments_chunk.apply(
        kwargs={**kwargs, "generation": 1}
    ).get()  # an older resume
    assert state(su, other)[0] == "COMPLETED"


def test_nothing_reopens_if_the_campaign_was_paused_again(
    session_factory, su, producer, workers
) -> None:
    from tests.support.pause_support import pause

    w = paused_with_appended_email(session_factory, su)
    lead = finished_lead(su, w, 0)
    resume_with(session_factory, su, w, include_completed=True)
    kwargs = reopen_tasks(producer)[0]
    pause(session_factory, su, w)

    workers.reopen_completed_enrollments_chunk.apply(kwargs=kwargs).get()
    assert state(su, lead)[0] == "COMPLETED"


def test_exactly_one_appended_email_is_planned_per_reopened_lead(
    session_factory, su, producer, workers
) -> None:
    w = paused_with_appended_email(session_factory, su)
    leads = [finished_lead(su, w, i) for i in range(2)]
    resume_with(session_factory, su, w, include_completed=True)
    workers.reopen_completed_enrollments_chunk.apply(
        kwargs=reopen_tasks(producer)[0]
    ).get()

    now = today_at(10)
    for _ in range(2):  # the sweep runs repeatedly; the unique message key must hold
        session = session_factory()
        try:
            set_transaction_context(session, workspace_id=w.ws)
            ProgressionService(
                CampaignWorkerRepository(session),
                is_suppressed=lambda ws, address_id: False,
                default_pacing=MailboxPacing(daily_cap=5, min_spacing_seconds=60),
                overdue_grace=timedelta(minutes=2),
            ).advance_batch(
                workspace_id=w.ws, campaign_id=w.campaign_id, limit=100, now=now
            )
            session.commit()
        finally:
            session.close()

    # Step 5 is the appended email (positions: E1 W2 E3 W4 E5). One WAIT step is not a message.
    appended = su.execute(
        "SELECT e.id, count(m.id) FROM public.campaign_enrollments e "
        "JOIN public.messages m ON m.enrollment_id = e.id "
        "JOIN public.sequence_steps s ON s.id = m.step_id AND s.position = 5 "
        "WHERE e.campaign_id=%s GROUP BY e.id",
        [w.campaign_id],
    ).fetchall()
    assert sorted(r[0] for r in appended) == sorted(leads)
    assert all(r[1] == 1 for r in appended)
    assert all(
        state(su, lead)[1] is not None for lead in leads
    )  # pointing at the appended email now
    assert su.execute(
        "SELECT min(due_at) >= %s FROM public.messages WHERE campaign_id=%s AND status IN ('PLANNED','SCHEDULED')",
        [now, w.campaign_id],
    ).fetchone()[
        0
    ]  # overdue appended emails are spread into the future, not released at once


def test_reopening_is_refused_when_the_campaign_will_not_resume_straight_to_running(
    session_factory, su, producer
) -> None:
    w = paused_with_appended_email(session_factory, su)
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaigns SET start_at = now() + interval '2 days' WHERE id=%s",
        [w.campaign_id],
    )
    su.execute("SET session_replication_role = origin")
    with pytest.raises(AppError) as info:
        resume_with(session_factory, su, w, include_completed=True)
    assert info.value.status_code == 422
    assert camp(su, w)["status"] == "PAUSED"
    assert reopen_tasks(producer) == []
