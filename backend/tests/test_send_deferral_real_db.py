"""`SendingService.execute` on a REAL PostgreSQL: what happens to a claimed message
that cannot be sent yet (mailbox at its limit, no limit configured, mailbox held,
unsubscribe not configured).

The Redis limiter is stood in for (it has its own tests); everything else is real:
the roles (`app_worker_send`), row-level security, the messages constraints and the
state the scheduler would see afterwards. No email is sent and nothing leaves the
machine.
"""

# ruff: noqa: E402, E501 -- imports follow importorskip; long lines are SQL.
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.modules.rate_limit.schemas import RateDenial, RateLimitDenied, RateScopeKind
from app.modules.scheduler.schemas import SendTaskPayload
from app.modules.sending.service import SendingService
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

SUBJECT, BODY = "Quick idea", "<p>Hi</p>"


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


def claimed_message(su, *, with_limit: bool = True) -> World:
    """A campaign email the scheduler has claimed (QUEUED, generation 1), from a
    connected mailbox that, unless told otherwise, has the default daily limit."""
    w = seed_world(su, running=True, members=1)
    now = datetime.now(UTC)
    digest = hashlib.sha256(f"{SUBJECT}\x00{BODY}\x001".encode()).hexdigest()
    su.execute("SET session_replication_role = replica")
    su.execute(
        """UPDATE public.mailboxes SET connection_state='CONNECTED', health_state='HEALTHY',
                  connected_generation=current_connection_generation,
                  provider_account_id='acct-'||id::text WHERE id=%s""",
        [w.mailbox_id],
    )
    su.execute(
        """UPDATE public.messages SET status='QUEUED', dispatch_origin='SCHEDULED', dispatch_generation=1,
                  claim_expires_at=%s, due_at=%s, anchor_at=%s, rendered_at=%s, renderer_version=1,
                  content_subject=%s, content_body_html=%s, content_digest=%s,
                  frozen_destination='lead0@target0.test', frozen_sender_address='sender@acme.test'
           WHERE id=%s""",
        [now + timedelta(minutes=5), now - timedelta(minutes=1), now - timedelta(minutes=1), now,
         SUBJECT, BODY, digest, w.message1_id],
    )
    su.execute("SET session_replication_role = origin")
    if with_limit:
        su.execute(
            """INSERT INTO public.tenant_rate_policies
               (workspace_id, mailbox_id, kind, unit, window_seconds, limit_value, window_kind, min_spacing_seconds)
               VALUES (%s,%s,'MAILBOX','MESSAGE',86400,50,'ROLLING',60)""",
            [w.ws, w.mailbox_id],
        )
    return w


def payload(w: World) -> SendTaskPayload:
    return SendTaskPayload(
        schema_version=1, work_id=str(uuid.uuid4()), delivery_id=str(uuid.uuid4()),
        message_id=str(w.message1_id), dispatch_id=str(uuid.uuid4()),
        resource_id=str(w.message1_id), workspace_id=str(w.ws), dispatch_generation=1,
    )


def run(session_factory, w: World, limiter: MagicMock | None = None, **settings_overrides):
    limiter = limiter or MagicMock()
    limiter.current_generation.return_value = 1
    settings = Settings.current().model_copy(update=settings_overrides)
    with session_factory() as session:
        outcome = SendingService(session, settings=settings, rate_limiter=limiter).execute(payload(w))
    return outcome, limiter


def state(su, w: World):
    return su.execute(
        "SELECT status, dispatch_origin, claim_expires_at IS NULL, due_at, version "
        "FROM public.messages WHERE id=%s",
        [w.message1_id],
    ).fetchone()


def generation(su, w: World) -> int:
    return su.execute(
        "SELECT dispatch_generation FROM public.messages WHERE id=%s", [w.message1_id]
    ).fetchone()[0]


def attempts(su, w: World) -> int:
    return su.execute(
        "SELECT count(*) FROM public.message_attempts WHERE message_id=%s", [w.message1_id]
    ).fetchone()[0]


def minutes_until_due(su, w: World) -> float:
    return (state(su, w)[3] - datetime.now(UTC)).total_seconds() / 60


def denied(retry_after: float) -> RateLimitDenied:
    return RateLimitDenied(
        RateDenial("CAPACITY_DENIED", RateScopeKind.MAILBOX, "mailbox", retry_after)
    )


class TestAMailboxAtItsLimit:
    def test_the_message_goes_back_to_the_scheduler_for_later(self, su, session_factory):
        w = claimed_message(su)
        limiter = MagicMock()
        limiter.reserve.side_effect = denied(7200.0)

        outcome, _ = run(session_factory, w, limiter)

        assert (outcome.outcome, outcome.reason) == ("DEFERRED", "rate_limit_denied")
        status, origin, claim_cleared, _, _ = state(su, w)
        assert (status, origin, claim_cleared) == ("SCHEDULED", None, True)
        assert 120 <= minutes_until_due(su, w) <= 133  # two hours plus up to 10% jitter
        assert attempts(su, w) == 0  # nothing was authorized, nothing was sent
        assert generation(su, w) == 2  # the claim that was handed back is now stale

    def test_a_short_wait_is_not_shorter_than_thirty_seconds(self, su, session_factory):
        w = claimed_message(su)
        limiter = MagicMock()
        limiter.reserve.side_effect = denied(1.0)

        run(session_factory, w, limiter)

        assert 0.45 <= minutes_until_due(su, w) <= 0.6

    def test_it_is_not_picked_up_again_before_it_is_due(self, su, session_factory):
        w = claimed_message(su)
        limiter = MagicMock()
        limiter.reserve.side_effect = denied(3600.0)
        run(session_factory, w, limiter)

        due_now = su.execute(
            "SELECT count(*) FROM public.messages WHERE id=%s "
            "AND status IN ('SCHEDULED','RETRY_SCHEDULED') AND due_at <= now()",
            [w.message1_id],
        ).fetchone()[0]

        assert due_now == 0

    def test_a_redelivered_task_after_the_hand_back_is_a_harmless_noop(self, su, session_factory):
        w = claimed_message(su)
        limiter = MagicMock()
        limiter.reserve.side_effect = denied(3600.0)
        run(session_factory, w, limiter)
        before = state(su, w)

        outcome, _ = run(session_factory, w, limiter)  # the old queue message arrives again

        # Recognised as stale. At the old generation it would pass the generation
        # check, find the message SCHEDULED and have it permanently skipped.
        assert (outcome.outcome, outcome.reason) == ("NOOP", "stale_dispatch_generation")
        assert state(su, w) == before


class TestAMailboxWithNoLimit:
    def test_it_never_sends_unlimited(self, su, session_factory):
        w = claimed_message(su, with_limit=False)

        outcome, limiter = run(session_factory, w)

        assert (outcome.outcome, outcome.reason) == ("DEFERRED", "mailbox_limits_missing")
        limiter.reserve.assert_not_called()  # no capacity was even asked for
        assert state(su, w)[0] == "SCHEDULED" and attempts(su, w) == 0
        assert 14 <= minutes_until_due(su, w) <= 17

    def test_it_sends_as_soon_as_a_limit_exists(self, su, session_factory):
        w = claimed_message(su, with_limit=False)
        run(session_factory, w)
        su.execute(
            """INSERT INTO public.tenant_rate_policies
               (workspace_id, mailbox_id, kind, unit, window_seconds, limit_value, window_kind, min_spacing_seconds)
               VALUES (%s,%s,'MAILBOX','MESSAGE',86400,50,'ROLLING',60)""",
            [w.ws, w.mailbox_id],
        )
        # The scheduler would claim it again once due; make it due and claim it.
        su.execute(
            "UPDATE public.messages SET due_at = now() - interval '1 minute' WHERE id=%s",
            [w.message1_id],
        )
        su.execute(
            "UPDATE public.messages SET status='QUEUED', dispatch_origin='SCHEDULED', dispatch_generation=3, "
            "claim_expires_at = now() + interval '5 minutes' WHERE id=%s",
            [w.message1_id],
        )
        limiter = MagicMock()
        limiter.reserve.side_effect = denied(60.0)  # reaches the limiter now

        with session_factory() as session:
            limiter.current_generation.return_value = 1
            service = SendingService(session, settings=Settings.current(), rate_limiter=limiter)
            outcome = service.execute(payload(w).model_copy(update={"dispatch_generation": 3}))

        assert outcome.reason == "rate_limit_denied"
        limiter.reserve.assert_called_once()


class TestAHeldMailbox:
    def test_its_messages_wait_fifteen_minutes_instead_of_churning(self, su, session_factory):
        w = claimed_message(su)
        su.execute(
            """INSERT INTO public.safety_holds
               (workspace_id, source_work_identity, target_kind, target_mailbox_id, status, reason)
               VALUES (%s,'auto-bounce:test','MAILBOX',%s,'ACTIVE','Bounce rate 12%%')""",
            [w.ws, w.mailbox_id],
        )

        outcome, limiter = run(session_factory, w)

        assert (outcome.outcome, outcome.reason) == ("DEFERRED", "safety_hold_active")
        limiter.reserve.assert_not_called()
        assert state(su, w)[0] == "SCHEDULED" and attempts(su, w) == 0
        assert 14 <= minutes_until_due(su, w) <= 17

    def test_a_release_lets_the_next_claim_through_to_the_limiter(self, su, session_factory):
        w = claimed_message(su)
        su.execute(
            """INSERT INTO public.safety_holds
               (id, workspace_id, source_work_identity, target_kind, target_mailbox_id, status, reason)
               VALUES (%s,%s,'auto-bounce:test','MAILBOX',%s,'ACTIVE','Bounce rate 12%%')""",
            [uuid.uuid4(), w.ws, w.mailbox_id],
        )
        run(session_factory, w)
        su.execute(
            "UPDATE public.safety_holds SET status='RESOLVED', resolved_at=now() WHERE workspace_id=%s",
            [w.ws],
        )
        su.execute(
            "UPDATE public.messages SET status='QUEUED', dispatch_origin='SCHEDULED', dispatch_generation=3, "
            "claim_expires_at = now() + interval '5 minutes' WHERE id=%s",
            [w.message1_id],
        )
        limiter = MagicMock()
        limiter.reserve.side_effect = denied(60.0)
        limiter.current_generation.return_value = 1

        with session_factory() as session:
            outcome = SendingService(session, settings=Settings.current(), rate_limiter=limiter).execute(
                payload(w).model_copy(update={"dispatch_generation": 3})
            )

        assert outcome.reason == "rate_limit_denied"  # past the hold gate, stopped only by the limiter


class TestUnsubscribeNotConfigured:
    def test_nothing_is_sent_and_the_message_waits(self, su, session_factory):
        w = claimed_message(su)

        outcome, limiter = run(session_factory, w, unsubscribe_signing_key="")

        assert (outcome.outcome, outcome.reason) == ("DEFERRED", "unsubscribe_not_configured")
        limiter.reserve.assert_not_called()
        assert state(su, w)[0] == "SCHEDULED" and attempts(su, w) == 0
        assert 14 <= minutes_until_due(su, w) <= 17


class TestAnotherTenant:
    def test_deferring_one_tenants_message_leaves_anothers_alone(self, su, session_factory):
        mine = claimed_message(su)
        theirs = claimed_message(su)
        limiter = MagicMock()
        limiter.reserve.side_effect = denied(3600.0)

        run(session_factory, mine, limiter)

        assert state(su, mine)[0] == "SCHEDULED"
        assert state(su, theirs)[0] == "QUEUED"
