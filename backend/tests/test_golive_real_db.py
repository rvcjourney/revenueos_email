"""Go-live safety checks against a REAL PostgreSQL (RLS, grants, triggers enforced).

Covers what SQLite unit tests cannot: that the public unsubscribe flow, capacity
deferral and automatic mailbox protection work as the real database roles
(app_api, app_worker_general, app_worker_send, app_worker_sync). Nothing is sent
and nothing touches production: the database is a throwaway pgserver instance.
"""

# ruff: noqa: E402, E501 -- imports follow importorskip; long lines are SQL.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db
from app.main import app
from app.modules.unsubscribe.tokens import make_unsubscribe_token
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def uri():
    with throwaway_database() as value:
        yield value


@pytest.fixture(scope="module")
def engine(uri):
    engine = create_engine("postgresql+psycopg://" + uri.split("://", 1)[1], future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture()
def su(uri):
    conn = psycopg.connect(uri, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture()
def api_client(session_factory):
    """The real app with its DB dependency replaced by a real ``app_api`` session,
    exactly as ``get_db`` provides one (anonymous: no user, no workspace)."""

    def override_get_db():
        with session_factory() as session:
            session.execute(text("SET LOCAL ROLE app_api"))
            yield session
            session.commit()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def scalar(su, sql: str, params: list | None = None):
    return su.execute(sql, params or []).fetchone()[0]


def sent_world(su) -> World:
    """A tenant whose first email was SENT, with a PLANNED follow-up that an
    unsubscribe must cancel."""
    w = seed_world(su, running=True, members=1)
    now = datetime.now(UTC)
    su.execute("SET session_replication_role = replica")
    su.execute(
        """UPDATE public.messages SET status='SENT', accepted_at=%s, due_at=%s, anchor_at=%s,
                  rendered_at=%s, content_subject='Quick idea', content_body_html='<p>Hi</p>',
                  content_digest=%s, frozen_destination='lead0@target0.test',
                  frozen_sender_address='sender@acme.test', rfc_message_id='<s1@acme.test>'
           WHERE id=%s""",
        [now, now, now, now, "a" * 64, w.message1_id],
    )
    follow_up = uuid.uuid4()
    su.execute(
        """INSERT INTO public.messages
           (id, workspace_id, purpose, campaign_id, enrollment_id, sequence_id, step_id,
            mailbox_id, address_id, status)
           VALUES (%s,%s,'CAMPAIGN',%s,%s,%s,%s,%s,%s,'PLANNED')""",
        [follow_up, w.ws, w.campaign_id, w.enrollment_id, w.sequence_id, w.step2,
         w.mailbox_id, w.address_ids[0]],
    )
    su.execute("SET session_replication_role = origin")
    w.follow_up_id = follow_up  # type: ignore[attr-defined]
    return w


SIGNING_KEY = "test-unsubscribe-signing-key"  # set for every test by tests/conftest.py


def link_token(w: World) -> str:
    return make_unsubscribe_token(w.ws, w.message1_id, SIGNING_KEY)


class TestPublicUnsubscribe:
    """An anonymous recipient using the signed link in an email, against the real
    database roles. Before the fix this died with "new row violates row-level
    security policy for table suppressions"."""

    def test_post_suppresses_stops_and_cancels(self, su, api_client):
        w = sent_world(su)

        response = api_client.post(f"/api/v1/unsubscribe/{link_token(w)}")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "unsubscribed"
        assert scalar(
            su,
            "SELECT count(*) FROM public.suppressions "
            "WHERE workspace_id=%s AND address_id=%s AND reason='UNSUBSCRIBE' AND status='ACTIVE'",
            [w.ws, w.address_ids[0]],
        ) == 1
        assert scalar(
            su, "SELECT state FROM public.campaign_enrollments WHERE id=%s", [w.enrollment_id]
        ) != "ACTIVE"
        # A message that was never sent is SKIPPED with the reason, not CANCELLED.
        assert su.execute(
            "SELECT status, terminal_reason FROM public.messages WHERE id=%s",
            [w.follow_up_id],  # type: ignore[attr-defined]
        ).fetchone() == ("SKIPPED", "unsubscribed")
        assert scalar(
            su,
            "SELECT count(*) FROM public.recipient_outcomes "
            "WHERE workspace_id=%s AND enrollment_id=%s AND kind='UNSUBSCRIBED'",
            [w.ws, w.enrollment_id],
        ) == 1

    def test_one_click_form_post_from_a_mail_provider(self, su, api_client):
        """RFC 8058: the provider POSTs `List-Unsubscribe=One-Click` as a form."""
        w = sent_world(su)

        response = api_client.post(
            f"/api/v1/unsubscribe/{link_token(w)}",
            data={"List-Unsubscribe": "One-Click"},
        )

        assert response.status_code == 200, response.text
        assert scalar(
            su,
            "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s AND address_id=%s",
            [w.ws, w.address_ids[0]],
        ) == 1

    def test_a_browser_gets_a_page_not_json(self, su, api_client):
        w = sent_world(su)

        response = api_client.post(
            f"/api/v1/unsubscribe/{link_token(w)}", headers={"Accept": "text/html"}
        )

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "unsubscribed" in response.text.lower()

    def test_repeating_it_is_harmless(self, su, api_client):
        w = sent_world(su)
        token = link_token(w)

        first = api_client.post(f"/api/v1/unsubscribe/{token}")
        second = api_client.post(f"/api/v1/unsubscribe/{token}")

        assert first.status_code == second.status_code == 200
        assert scalar(
            su,
            "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s AND address_id=%s",
            [w.ws, w.address_ids[0]],
        ) == 1
        assert scalar(
            su,
            "SELECT count(*) FROM public.recipient_outcomes "
            "WHERE workspace_id=%s AND kind='UNSUBSCRIBED'",
            [w.ws],
        ) == 1

    def test_get_never_unsubscribes(self, su, api_client):
        """Link scanners fetch every URL in an email; that must not opt anyone out."""
        w = sent_world(su)

        page = api_client.get(f"/api/v1/unsubscribe/{link_token(w)}")

        assert page.status_code == 200 and "Confirm Unsubscribe" in page.text
        assert scalar(
            su,
            "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s",
            [w.ws],
        ) == 0

    def test_a_forged_token_is_rejected_without_taking_a_db_connection(
        self, su, session_factory
    ):
        w = sent_world(su)
        checkouts: list[int] = []

        def counting_get_db():
            checkouts.append(1)
            with session_factory() as session:
                session.execute(text("SET LOCAL ROLE app_api"))
                yield session
                session.commit()

        app.dependency_overrides[get_db] = counting_get_db
        try:
            client = TestClient(app)
            forged = make_unsubscribe_token(w.ws, w.message1_id, "some-other-key")
            for token in (forged, "u1.not-a-real-token", "plain-garbage"):
                response = client.post(f"/api/v1/unsubscribe/{token}")
                assert response.status_code == 404, token
        finally:
            app.dependency_overrides.clear()

        assert checkouts == []
        assert scalar(
            su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s", [w.ws]
        ) == 0

    def test_a_token_only_ever_affects_its_own_tenant(self, su, api_client):
        mine = sent_world(su)
        other = sent_world(su)  # same lead address text, different workspace

        api_client.post(f"/api/v1/unsubscribe/{link_token(mine)}")

        assert scalar(
            su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s", [mine.ws]
        ) == 1
        assert scalar(
            su, "SELECT count(*) FROM public.suppressions WHERE workspace_id=%s", [other.ws]
        ) == 0
        assert scalar(
            su, "SELECT state FROM public.campaign_enrollments WHERE id=%s", [other.enrollment_id]
        ) == "ACTIVE"


# ---------------------------------------------------------------------------
# Handing a claimed message back to the scheduler (capacity / safety hold)
# ---------------------------------------------------------------------------

from datetime import timedelta

from app.modules.sending.repository import SendingRepository


def claimed_world(su, *, origin: str = "SCHEDULED", next_retry_at=None) -> World:
    """A message the scheduler has just claimed: QUEUED, generation 1, leased."""
    w = seed_world(su, running=True, members=1)
    now = datetime.now(UTC)
    su.execute("SET session_replication_role = replica")
    su.execute(
        """UPDATE public.messages SET status='QUEUED', dispatch_origin=%s, dispatch_generation=1,
                  claim_expires_at=%s, due_at=%s, anchor_at=%s, next_retry_at=%s, rendered_at=%s,
                  content_subject='Quick idea', content_body_html='<p>Hi</p>',
                  content_digest=%s, frozen_destination='lead0@target0.test',
                  frozen_sender_address='sender@acme.test'
           WHERE id=%s""",
        [origin, now + timedelta(minutes=5), now - timedelta(minutes=1), now - timedelta(minutes=1),
         next_retry_at, now, "a" * 64, w.message1_id],
    )
    su.execute("SET session_replication_role = origin")
    return w


def defer(session_factory, w: World, *, generation: int = 1, workspace=None, minutes: int = 15):
    with session_factory() as session:
        done = SendingRepository(session).defer_for_capacity(
            workspace_id=workspace or w.ws,
            message_id=w.message1_id,
            dispatch_generation=generation,
            due_at=datetime.now(UTC) + timedelta(minutes=minutes),
        )
        session.commit()
    return done


def message_state(su, w: World):
    return su.execute(
        "SELECT status, dispatch_origin, claim_expires_at IS NULL, due_at, version "
        "FROM public.messages WHERE id=%s",
        [w.message1_id],
    ).fetchone()


class TestHandingAMessageBack:
    """`defer_for_capacity` as `app_worker_send`, the role the send worker runs as."""

    def test_a_claimed_message_goes_back_to_scheduled_for_later(self, su, session_factory):
        w = claimed_world(su)
        version = message_state(su, w)[4]

        assert defer(session_factory, w) is True

        status, origin, claim_cleared, due_at, new_version = message_state(su, w)
        assert (status, origin, claim_cleared) == ("SCHEDULED", None, True)
        assert due_at > datetime.now(UTC) + timedelta(minutes=14)
        assert new_version == version + 1
        # The handed-back claim is now stale: a late duplicate of its task must not
        # find a SCHEDULED message at the generation it was sent for.
        assert scalar(
            su, "SELECT dispatch_generation FROM public.messages WHERE id=%s", [w.message1_id]
        ) == 2

    def test_the_scheduler_does_not_pick_it_up_before_it_is_due(self, su, session_factory):
        w = claimed_world(su)
        defer(session_factory, w, minutes=30)

        assert scalar(
            su,
            "SELECT count(*) FROM public.messages WHERE id=%s "
            "AND status IN ('SCHEDULED','RETRY_SCHEDULED') AND due_at <= now()",
            [w.message1_id],
        ) == 0

    def test_a_retrying_message_keeps_its_retry_time(self, su, session_factory):
        later = datetime.now(UTC) + timedelta(hours=2)
        w = claimed_world(su, origin="RETRY_SCHEDULED", next_retry_at=later)

        assert defer(session_factory, w, minutes=1) is True

        status, _, _, due_at, _ = message_state(su, w)
        assert status == "RETRY_SCHEDULED"
        assert due_at >= later  # never earlier than the retry the policy asked for

    def test_a_message_with_an_attempt_is_never_moved(self, su, session_factory):
        w = claimed_world(su)
        su.execute("SET session_replication_role = replica")
        su.execute(
            """INSERT INTO public.message_attempts
               (workspace_id, message_id, mailbox_id, ordinal, invocation_owner,
                credential_generation, authorization_deadline, dispatch_generation)
               VALUES (%s,%s,%s,1,'worker',1,%s,1)""",
            [w.ws, w.message1_id, w.mailbox_id, datetime.now(UTC) + timedelta(minutes=1)],
        )
        su.execute("SET session_replication_role = origin")

        assert defer(session_factory, w) is False
        assert message_state(su, w)[0] == "QUEUED"

    def test_a_stale_claim_is_ignored(self, su, session_factory):
        w = claimed_world(su)

        assert defer(session_factory, w, generation=7) is False
        assert message_state(su, w)[0] == "QUEUED"

    def test_it_cannot_touch_another_tenants_message(self, su, session_factory):
        mine = claimed_world(su)
        other = seed_world(su, running=True, members=1)

        assert defer(session_factory, mine, workspace=other.ws) is False
        assert message_state(su, mine)[0] == "QUEUED"

    def test_a_message_already_sent_is_not_reopened(self, su, session_factory):
        w = sent_world(su)

        assert defer(session_factory, w) is False
        assert message_state(su, w)[0] == "SENT"
