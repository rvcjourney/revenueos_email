"""Pause/resume safety properties that span several services, against a REAL
PostgreSQL (pgserver) with a FAKE provider (nothing is ever sent).

* An email whose outcome was unknown when the campaign was paused, and is later
  confirmed as sent, plans no follow-up while paused and exactly one after resume.
* A reconcile pass that dies half-way leaves nothing half-done and replays cleanly.
* The same `email.send` delivered twice around a pause and resume reaches the provider
  at most once.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.context import set_transaction_context
from app.modules.campaigns.pacing import MailboxPacing
from app.modules.campaigns.progression import ProgressionService
from app.modules.campaigns.reconcile import ReconcileService
from app.modules.campaigns.worker_repository import CampaignWorkerRepository
from app.modules.mailboxes.providers.base import ProviderCapability, ProviderSendResult
from app.modules.rate_limit.schemas import RateReservation
from app.modules.scheduler.repository import SchedulerRepository
from app.modules.scheduler.schemas import SendTaskPayload
from app.modules.sending.reconciliation_service import ReconciliationService
from app.modules.sending.repository import SendingRepository
from app.modules.sending.service import SendingService
from tests.support.pause_support import (
    DEFAULT_PACING,
    FakeProducer,
    add_message,
    camp,
    discoverable,
    msg,
    pause,
    resume,
    today_at,
    world,
)
from tests.support.real_pg import throwaway_database

pytestmark = pytest.mark.filterwarnings("ignore")


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
def producer(monkeypatch) -> FakeProducer:
    fake = FakeProducer()
    monkeypatch.setattr(
        "app.services.task_dispatch.get_task_producer", lambda *a, **k: fake
    )
    monkeypatch.setattr(
        "app.modules.campaigns.preflight.unsubscribe_ready", lambda s: True
    )
    return fake


def progress(session_factory, w, now: datetime):
    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        summary = ProgressionService(
            CampaignWorkerRepository(session),
            is_suppressed=lambda ws, address_id: False,
            default_pacing=MailboxPacing(daily_cap=50, min_spacing_seconds=60),
            overdue_grace=timedelta(minutes=2),
        ).advance_batch(
            workspace_id=w.ws, campaign_id=w.campaign_id, limit=100, now=now
        )
        session.commit()
        return summary
    finally:
        session.close()


def followups(su, w) -> int:
    return su.execute(
        "SELECT count(*) FROM public.messages m JOIN public.sequence_steps s ON s.id = m.step_id "
        "WHERE m.campaign_id=%s AND s.position = 3",
        [w.campaign_id],
    ).fetchone()[0]


def fake_provider() -> MagicMock:
    provider = MagicMock()
    provider.capabilities = frozenset({ProviderCapability.LOOKUP_MESSAGE})
    provider.lookup_message.return_value = ProviderSendResult(
        status="ACCEPTED",
        provider_message_id="gmail-1",
        provider_thread_id="th-1",
        accepted_at=datetime.now(UTC),
    )
    provider.send_message.return_value = ProviderSendResult(
        status="ACCEPTED",
        provider_message_id="gmail-2",
        provider_thread_id="th-2",
        accepted_at=datetime.now(UTC),
    )
    return provider


# ---------------------------------------------------------------------------
# an unknown outcome that turns out to be a success, around a pause
# ---------------------------------------------------------------------------


def test_a_late_confirmed_send_plans_no_follow_up_while_paused_and_exactly_one_after_resume(
    session_factory, su, producer
) -> None:
    w = world(su, members=1)
    now = today_at(10)
    # The send was in flight when the worker died: recovery left it UNKNOWN_OUTCOME.
    unknown = add_message(
        su, w, 0, status="UNKNOWN_OUTCOME", due_at=now - timedelta(hours=1)
    )
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.messages SET rfc_message_id=%s WHERE id=%s",
        ["<unknown-1@acme.test>", unknown],
    )
    su.execute("SET session_replication_role = origin")

    pause(session_factory, su, w)

    # The provider confirms it (real reconciliation service, real roles, fake provider).
    provider = fake_provider()
    session = session_factory()
    try:
        with (
            patch(
                "app.modules.sending.reconciliation_service.ProviderRegistry.get",
                return_value=provider,
            ),
            patch(
                "app.modules.sending.reconciliation_service.decrypt_credentials",
                return_value={"access_token": "t"},
            ),
            patch.object(
                SendingRepository,
                "get_mailbox_connection",
                return_value={
                    "credential_ciphertext": b"c",
                    "nonce": b"n",
                    "encryption_key_id": "k",
                    "protected_config": None,
                },
            ),
        ):
            counts = ReconciliationService(session).reconcile_unknown_messages(
                workspace_id=w.ws, now=now
            )
    finally:
        session.close()
    assert counts["reconciled_sent"] == 1
    assert msg(su, unknown)["status"] == "SENT"
    provider.send_message.assert_not_called()  # a lookup, never a second send

    # Paused: the sweep plans nothing, however often it runs.
    for _ in range(3):
        assert progress(session_factory, w, now).fetched == 0
    assert followups(su, w) == 0

    resume(session_factory, su, w)
    for _ in range(3):  # the sweep repeats; the unique (enrollment, step) key holds
        progress(session_factory, w, now + timedelta(days=3))
    assert followups(su, w) == 1


# ---------------------------------------------------------------------------
# a reconcile pass that crashes
# ---------------------------------------------------------------------------


def test_a_reconcile_pass_that_crashes_leaves_nothing_half_done_and_replays_cleanly(
    session_factory, su, producer
) -> None:
    w = world(su, members=5)
    now = today_at(10)
    ids = [add_message(su, w, i, due_at=now - timedelta(hours=1)) for i in range(5)]
    su.execute(
        "UPDATE public.campaigns SET schedule_generation = 2 WHERE id=%s",
        [w.campaign_id],
    )
    before = {m: msg(su, m) for m in ids}

    class Dying(CampaignWorkerRepository):
        calls = 0

        def reschedule_message(self, **kwargs):
            Dying.calls += 1
            if Dying.calls == 3:
                raise RuntimeError("worker died mid-chunk")
            return super().reschedule_message(**kwargs)

    session = session_factory()
    try:
        set_transaction_context(session, workspace_id=w.ws)
        with pytest.raises(RuntimeError):
            ReconcileService(
                Dying(session), default_pacing=DEFAULT_PACING
            ).reconcile_batch(
                workspace_id=w.ws,
                campaign_id=w.campaign_id,
                generation=2,
                limit=100,
                now=now,
            )
        session.rollback()  # what the task does on an unexpected error
    finally:
        session.close()
    assert {
        m: msg(su, m) for m in ids
    } == before  # nothing moved: it is one transaction

    from tests.support.pause_support import reconcile

    first = reconcile(session_factory, w, 2, now)
    after_first = {m: msg(su, m) for m in ids}
    assert first.restamped == 5
    assert all(row["schedule_generation"] == 2 for row in after_first.values())
    assert len({row["due_at"] for row in after_first.values()}) == 5  # one slot each

    replay = reconcile(session_factory, w, 2, now)  # redelivered task
    assert replay.restamped == 0
    assert {m: msg(su, m) for m in ids} == after_first
    assert (
        su.execute(
            "SELECT count(*) FROM public.messages WHERE campaign_id=%s", [w.campaign_id]
        ).fetchone()[0]
        == 5
    )


# ---------------------------------------------------------------------------
# the same email.send delivered twice around a pause and resume
# ---------------------------------------------------------------------------


def _limiter(generation: int) -> MagicMock:
    limiter = MagicMock()
    limiter.current_generation.return_value = generation
    limiter.reserve.side_effect = lambda *a, **k: RateReservation(
        reservation_id=str(uuid.uuid4()),
        generation=generation,
        granted_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        scopes=(),
    )
    return limiter


def _execute(session_factory, su, payload: SendTaskPayload, provider: MagicMock):
    su.execute(
        "INSERT INTO public.rate_control (id, generation, status, recovery_watermark, recovery_started_at) "
        "VALUES (true, 1, 'READY', now(), NULL) "
        "ON CONFLICT (id) DO UPDATE SET status = 'READY', recovery_watermark = now(), recovery_started_at = NULL"
    )
    generation = su.execute(
        "SELECT generation FROM public.rate_control WHERE id"
    ).fetchone()[0]
    with (
        patch(
            "app.modules.sending.service.ProviderRegistry.get", return_value=provider
        ),
        patch(
            "app.modules.sending.service.decrypt_credentials",
            return_value={"access_token": "t"},
        ),
        patch.object(
            SendingRepository,
            "get_mailbox_connection",
            return_value={
                "credential_ciphertext": b"c",
                "nonce": b"n",
                "encryption_key_id": "k",
                "protected_config": None,
                "expires_at": None,
            },
        ),
        session_factory() as session,
    ):
        return SendingService(
            session, settings=Settings.current(), rate_limiter=_limiter(generation)
        ).execute(payload)


def _claim(session_factory, w, message_id, at: datetime) -> SendTaskPayload:
    session = session_factory()
    try:
        claim = SchedulerRepository(session).claim_due_message(
            workspace_id=w.ws, message_id=message_id, authoritative_now=at
        )
        session.commit()
    finally:
        session.close()
    assert claim.claimed, claim.reason
    return SendTaskPayload(
        schema_version=1,
        work_id=str(uuid.uuid4()),
        delivery_id=str(uuid.uuid4()),
        message_id=str(message_id),
        dispatch_id=str(uuid.uuid4()),
        resource_id=str(message_id),
        workspace_id=str(w.ws),
        dispatch_generation=claim.dispatch_generation,
    )


def test_a_duplicate_send_delivery_around_a_pause_and_resume_reaches_the_provider_at_most_once(
    session_factory, su, producer
) -> None:
    w = world(su, members=1)
    now = datetime.now(UTC)
    due = now - timedelta(minutes=5)
    message = add_message(su, w, 0, due_at=due)
    su.execute(
        "INSERT INTO public.tenant_rate_policies (workspace_id, mailbox_id, kind, unit, window_seconds, limit_value, window_kind, min_spacing_seconds) "
        "VALUES (%s,%s,'MAILBOX','MESSAGE',86400,50,'ROLLING',60)",
        [w.ws, w.mailbox_id],
    )
    su.execute("SET session_replication_role = replica")
    su.execute(
        "INSERT INTO public.mailbox_connections (workspace_id, mailbox_id, generation, auth_mechanism, "
        "credential_ciphertext, encryption_key_id, nonce) "
        "SELECT workspace_id, id, current_connection_generation, 'OAUTH', decode('01', 'hex'), 'k1', "
        "decode('000102030405060708090a0b', 'hex') "
        "FROM public.mailboxes WHERE id=%s "
        "ON CONFLICT DO NOTHING",
        [w.mailbox_id],
    )
    su.execute("SET session_replication_role = origin")
    provider = fake_provider()

    # 1. The scheduler claims it and queues email.send ...
    payload = _claim(session_factory, w, message, now)
    # 2. ... the campaign is paused and resumed before a worker picks it up.
    pause(session_factory, su, w)
    resume(session_factory, su, w)
    assert camp(su, w)["schedule_generation"] == 2

    # 3. The (now stale) delivery arrives: it must not send.
    stale = _execute(session_factory, su, payload, provider)
    assert provider.send_message.call_count == 0, stale
    assert msg(su, message)["status"] != "SENT"

    # 4. The reconcile pass re-stamps it; the scheduler claims it again at generation 2.
    from tests.support.pause_support import reconcile

    reconcile(session_factory, w, 2, today_at(10))
    assert message in discoverable(session_factory, w, now + timedelta(days=3))
    current = _claim(session_factory, w, message, now + timedelta(days=3))

    # 5. The same delivery is handed to two workers: the provider is reached once.
    first = _execute(session_factory, su, current, provider)
    second = _execute(session_factory, su, current, provider)
    assert provider.send_message.call_count == 1, (first, second)
    assert msg(su, message)["status"] == "SENT"
    assert (
        su.execute(
            "SELECT count(*) FROM public.message_attempts WHERE message_id=%s",
            [message],
        ).fetchone()[0]
        == 1
    )
