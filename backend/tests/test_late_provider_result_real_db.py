"""Guarded send finalize (issue I-01) against a REAL PostgreSQL (pgserver): the
real roles (app_worker_send), grants, RLS and attempt/message guard triggers.

Proves: a normal result applies; recovery then a late acceptance resolves to SENT;
recovery then a late rejection changes nothing (evidence + audit appended, no
retry); a late result after reconciliation changes nothing instead of tripping
the terminal-attempt guard; and a real recovery/finalize race, in either order,
ends consistent. No provider is called and no email is sent.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.modules.mailboxes.providers.base import ErrorCategory, ProviderSendResult
from app.modules.sending.recovery_service import RecoveryService
from app.modules.sending.repository import (
    FINALIZE_LATE,
    FINALIZE_LATE_ACCEPTED,
    SendingRepository,
)
from app.modules.sending.service import SendingService
from tests.support.pause_support import add_message, world
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World
from tests.test_same_thread_real_db import replica, scalar

pytestmark = pytest.mark.filterwarnings("ignore")

ACCEPTED = ProviderSendResult(status="ACCEPTED", provider_message_id="p-1", provider_thread_id="t-1", rfc_message_id="<late@acme.test>")
RETRYABLE = ProviderSendResult(status="DEFINITIVELY_REJECTED", error_category=ErrorCategory.TEMPORARY_PROVIDER_ERROR, error_code="http_503")
AMBIGUOUS = ProviderSendResult(status="UNKNOWN", error_category="NETWORK_ERROR", error_code="timeout")


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


def sending(su, *, expired: bool = False) -> tuple[World, uuid.UUID, uuid.UUID]:
    """A message in SENDING with its PREPARED attempt (deadline passed if `expired`)."""
    w = world(su)
    now = datetime.now(UTC)
    message = add_message(su, w, 0, status="SENDING", due_at=now - timedelta(minutes=2))
    attempt = uuid.uuid4()
    started = now - timedelta(minutes=2)
    deadline = now - timedelta(seconds=30) if expired else now + timedelta(seconds=30)
    replica(
        su,
        """INSERT INTO public.message_attempts
           (id, workspace_id, message_id, mailbox_id, ordinal, invocation_owner, credential_generation,
            authorization_deadline, dispatch_generation, evidence_state, started_at)
           VALUES (%s,%s,%s,%s,1,'worker-send:t',1,%s,1,'PREPARED',%s)""",
        [attempt, w.ws, message, w.mailbox_id, deadline, started],
    )
    return w, message, attempt


def load(session_factory, w: World, message):
    """What the worker loaded before calling the provider."""
    session = session_factory()
    try:
        return SendingRepository(session).load_message_for_send(workspace_id=w.ws, message_id=message)
    finally:
        session.rollback()
        session.close()


def finalize(session_factory, w: World, message, attempt, result: ProviderSendResult, ctx=None):
    ctx = ctx or load(session_factory, w, message)
    assert ctx is not None
    session = session_factory()
    try:
        service = SendingService(session, settings=Settings.current(), rate_limiter=MagicMock())
        outcome = service._finalize_send_result(ctx, attempt, result)
        session.commit()
        return outcome
    finally:
        session.close()


def recover(session_factory, w: World) -> int:
    session = session_factory()
    try:
        return RecoveryService(session).recover_stale_executions(workspace_id=w.ws, now=datetime.now(UTC))
    finally:
        session.close()


def state(su, message, attempt) -> tuple:
    m = su.execute(
        "SELECT status, rfc_message_id, provider_message_id, next_retry_at FROM public.messages WHERE id=%s", [message]
    ).fetchone()
    a = su.execute("SELECT evidence_state, provider_thread_ref FROM public.message_attempts WHERE id=%s", [attempt]).fetchone()
    return (*m, *a)


def evidence(su, attempt) -> list[tuple]:
    return su.execute(
        "SELECT source_kind, classification FROM public.attempt_evidence WHERE attempt_id=%s", [attempt]
    ).fetchall()


def late_audits(su, message) -> list[str]:
    return [r[0] for r in su.execute(
        "SELECT reason FROM public.audit_events WHERE target_id=%s AND action='message.late_provider_result'", [message]
    ).fetchall()]


def test_a_result_for_the_live_attempt_applies(session_factory, su) -> None:
    w, message, attempt = sending(su)
    outcome = finalize(session_factory, w, message, attempt, ACCEPTED)
    assert outcome.outcome == "SENT"
    assert state(su, message, attempt) == ("SENT", "<late@acme.test>", "p-1", None, "ACCEPTED", "t-1")
    assert evidence(su, attempt) == [] and late_audits(su, message) == []


def test_recovery_then_a_late_acceptance_resolves_to_sent(session_factory, su) -> None:
    w, message, attempt = sending(su, expired=True)
    assert recover(session_factory, w) == 1
    assert state(su, message, attempt)[0] == "UNKNOWN_OUTCOME"
    outcome = finalize(session_factory, w, message, attempt, ACCEPTED)
    assert outcome.outcome == "SENT"
    assert state(su, message, attempt) == ("SENT", "<late@acme.test>", "p-1", None, "ACCEPTED", "t-1")
    assert scalar(su, "SELECT count(*) FROM public.audit_events WHERE target_id=%s AND action='message.sent' AND reason='accepted_after_recovery'", message) == 1


@pytest.mark.parametrize("result", [RETRYABLE, AMBIGUOUS])
def test_recovery_then_a_late_failure_changes_nothing(session_factory, su, result) -> None:
    w, message, attempt = sending(su, expired=True)
    recover(session_factory, w)
    before = state(su, message, attempt)
    outcome = finalize(session_factory, w, message, attempt, result)
    assert outcome.outcome == "NOOP" and outcome.reason == "late_provider_result"
    assert state(su, message, attempt) == before == ("UNKNOWN_OUTCOME", None, None, None, "UNKNOWN", None)
    kind = "REJECTED" if result is RETRYABLE else "UNKNOWN"
    assert evidence(su, attempt) == [("late_provider_result", kind)]
    assert late_audits(su, message) == [kind]


def test_a_late_result_after_reconciliation_changes_nothing(session_factory, su) -> None:
    w, message, attempt = sending(su, expired=True)
    recover(session_factory, w)
    replica(su, "UPDATE public.message_attempts SET evidence_state='ACCEPTED', completed_at=now(), provider_request_id='r' WHERE id=%s", [attempt])
    replica(su, "UPDATE public.messages SET status='SENT', accepted_at=now() WHERE id=%s", [message])
    outcome = finalize(session_factory, w, message, attempt, AMBIGUOUS)
    assert outcome.outcome == "NOOP"
    assert state(su, message, attempt)[0] == "SENT" and state(su, message, attempt)[4] == "ACCEPTED"
    assert evidence(su, attempt) == [("late_provider_result", "UNKNOWN")]


def test_the_send_role_can_write_everything_the_guarded_finalize_needs(session_factory, su) -> None:
    w, message, attempt = sending(su, expired=True)
    recover(session_factory, w)
    session = session_factory()
    try:
        repo = SendingRepository(session)
        outcome = repo.finalize_attempt_result(
            workspace_id=w.ws, message_id=message, attempt_id=attempt, message_status="FAILED",
            evidence_state="REJECTED", error_category="POLICY_REJECTION", error_code="x", terminal_reason="send_rejected",
        )
        assert session.execute(text("SELECT current_user")).scalar() == "app_worker_send"
        session.commit()
    finally:
        session.close()
    assert outcome == FINALIZE_LATE
    assert evidence(su, attempt) == [("late_provider_result", "REJECTED")]


def _race(session_factory, su, *, recovery_first: bool):
    """Hold the row lock in one transaction while the other blocks on it."""
    w, message, attempt = sending(su, expired=True)
    ctx = load(session_factory, w, message)  # loaded before the provider call
    results: dict[str, object] = {}
    first = session_factory()
    if recovery_first:
        # Recovery has taken the attempt row and is about to commit.
        first.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(w.ws)})
        first.execute(
            text("UPDATE public.message_attempts SET evidence_state='UNKNOWN', error_category='NETWORK_ERROR', error_code='execution_lease_expired' WHERE id=:a AND evidence_state='PREPARED'"),
            {"a": str(attempt)},
        )
        first.execute(
            text("UPDATE public.messages SET status='UNKNOWN_OUTCOME', version=version+1 WHERE id=:m AND status='SENDING'"),
            {"m": str(message)},
        )
        worker = threading.Thread(target=lambda: results.update(out=finalize(session_factory, w, message, attempt, ACCEPTED, ctx)))
    else:
        repo = SendingRepository(first)
        results["first"] = repo.finalize_attempt_result(
            workspace_id=w.ws, message_id=message, attempt_id=attempt, message_status="SENT", evidence_state="ACCEPTED",
            provider_message_id="p-1", provider_request_id="r", rfc_message_id="<late@acme.test>",
        )
        worker = threading.Thread(target=lambda: results.update(out=recover(session_factory, w)))
    worker.start()
    time.sleep(0.5)
    assert worker.is_alive(), "the second transaction should be waiting on the row lock"
    first.commit()
    first.close()
    worker.join(timeout=10)
    assert not worker.is_alive()
    return message, attempt, results


def test_recovery_racing_ahead_of_a_late_acceptance_ends_sent(session_factory, su) -> None:
    message, attempt, results = _race(session_factory, su, recovery_first=True)
    assert results["out"].outcome == "SENT"  # type: ignore[attr-defined]
    assert state(su, message, attempt)[0] == "SENT" and state(su, message, attempt)[4] == "ACCEPTED"


def test_a_finalize_racing_ahead_of_recovery_ends_sent_and_recovery_does_nothing(session_factory, su) -> None:
    message, attempt, results = _race(session_factory, su, recovery_first=False)
    assert results["out"] == 0
    assert state(su, message, attempt)[0] == "SENT" and state(su, message, attempt)[4] == "ACCEPTED"
    assert results["first"] not in (FINALIZE_LATE, FINALIZE_LATE_ACCEPTED)
