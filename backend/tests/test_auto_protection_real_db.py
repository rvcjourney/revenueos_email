"""Automatic mailbox protection on a REAL PostgreSQL, as the real roles.

The evaluator reads as `app_worker_general` and writes as `app_worker_sync`; people
list and release holds as `app_api`. Row-level security, column grants and the
safety-counter trigger are all enforced. Nothing here touches a shared database.
"""

# ruff: noqa: E402, E501 -- imports follow importorskip; long lines are SQL.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.deps import WorkspaceContext, get_db, get_workspace_context
from app.main import app
from app.modules.safety.evaluator import MailboxSafetyEvaluator
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


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


def scalar(su, sql: str, params: list | None = None):
    return su.execute(sql, params or []).fetchone()[0]


def add_sent(su, w: World, *, sent: int, bounced: int, age: timedelta = timedelta(hours=1),
             mailbox_id: uuid.UUID | None = None) -> None:
    """`sent` emails accepted `age` ago from the mailbox, `bounced` of them with a BOUNCED event."""
    mailbox = mailbox_id or w.mailbox_id
    accepted = NOW - age
    su.execute("SET session_replication_role = replica")
    for i in range(sent):
        message = uuid.uuid4()
        su.execute(
            """INSERT INTO public.messages
               (id, workspace_id, purpose, campaign_id, enrollment_id, sequence_id, step_id,
                mailbox_id, address_id, status, due_at, anchor_at, accepted_at, rendered_at,
                content_subject, content_body_html, content_digest, frozen_destination,
                frozen_sender_address)
               VALUES (%s,%s,'CAMPAIGN',%s,%s,%s,%s,%s,%s,'SENT',%s,%s,%s,%s,
                       'Hello','<p>Hi</p>',%s,'lead@target.test','sender@acme.test')""",
            [message, w.ws, w.campaign_id, uuid.uuid4(), w.sequence_id, w.step1, mailbox,
             w.address_ids[0], accepted, accepted, accepted, accepted, "a" * 64],
        )
        if i < bounced:
            su.execute(
                """INSERT INTO public.message_events
                   (workspace_id, message_id, kind, bounce_type, source, first_occurred_at, last_occurred_at)
                   VALUES (%s,%s,'BOUNCED','HARD','test',%s,%s)""",
                [w.ws, message, accepted, accepted],
            )
    su.execute("SET session_replication_role = origin")


def new_mailbox(su, w: World) -> uuid.UUID:
    mailbox_id = uuid.uuid4()
    su.execute(
        "INSERT INTO public.mailboxes (id, workspace_id, provider, original_address) VALUES (%s,%s,'SMTP',%s)",
        [mailbox_id, w.ws, f"other-{mailbox_id.hex[:6]}@acme.test"],
    )
    return mailbox_id


def evaluate(session_factory, w: World, mailbox_id=None, now: datetime = NOW):
    with session_factory() as session:
        result = MailboxSafetyEvaluator(session).evaluate(
            workspace_id=w.ws, mailbox_id=mailbox_id or w.mailbox_id, now=now
        )
        session.commit()
    return result


def holds(su, w: World, mailbox_id=None, status: str | None = None):
    sql = "SELECT source_work_identity, status, reason FROM public.safety_holds WHERE workspace_id=%s AND target_mailbox_id=%s"
    params: list = [w.ws, mailbox_id or w.mailbox_id]
    if status:
        sql += " AND status=%s"
        params.append(status)
    return su.execute(sql + " ORDER BY created_at", params).fetchall()


def pending(su, w: World, mailbox_id=None) -> int:
    return scalar(
        su, "SELECT pending_safety_count FROM public.mailboxes WHERE workspace_id=%s AND id=%s",
        [w.ws, mailbox_id or w.mailbox_id],
    )


class TestEvaluator:
    def test_a_critical_bounce_rate_holds_the_mailbox(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        add_sent(su, w, sent=40, bounced=4)  # 10%

        result = evaluate(session_factory, w)

        assert (result.sent, result.bounced, result.level, result.held) == (40, 4, "CRITICAL", True)
        [(identity, status, reason)] = holds(su, w)
        assert identity == f"auto-bounce:{w.mailbox_id}:2026-03-10"
        assert status == "ACTIVE" and "10.0%" in reason and "4 of 40" in reason
        assert pending(su, w) == 1  # the send gate reads this counter

    def test_a_held_mailbox_is_reported_to_the_send_gate(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        add_sent(su, w, sent=40, bounced=4)

        evaluate(session_factory, w)

        # gates.check_safety_holds refuses to send when this is above zero.
        assert pending(su, w) > 0

    @pytest.mark.parametrize(
        ("sent", "bounced", "why"),
        [
            (19, 19, "too few sends for the rate to mean anything"),
            (100, 5, "exactly 5% is not above the threshold"),
            (100, 3, "a warning level, not critical"),
            (100, 0, "no bounces"),
        ],
    )
    def test_it_leaves_a_mailbox_alone_below_the_threshold(self, su, session_factory, sent, bounced, why):
        w = seed_world(su, running=False, members=1)
        add_sent(su, w, sent=sent, bounced=bounced)

        result = evaluate(session_factory, w)

        assert result.held is False, why
        assert holds(su, w) == [] and pending(su, w) == 0

    def test_just_above_five_percent_is_held(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        add_sent(su, w, sent=100, bounced=6)

        assert evaluate(session_factory, w).held is True

    def test_sends_older_than_seven_days_do_not_count(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        add_sent(su, w, sent=30, bounced=30, age=timedelta(days=8))  # a bad old list
        add_sent(su, w, sent=100, bounced=0)

        result = evaluate(session_factory, w)

        assert (result.sent, result.bounced, result.held) == (100, 0, False)

    def test_repeating_it_places_one_hold(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        add_sent(su, w, sent=40, bounced=4)

        first = evaluate(session_factory, w)
        second = evaluate(session_factory, w, now=NOW + timedelta(minutes=5))

        assert (first.held, second.held) == (True, False)
        assert len(holds(su, w)) == 1 and pending(su, w) == 1

    def test_an_unreleased_hold_from_yesterday_is_not_doubled_today(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        add_sent(su, w, sent=40, bounced=4)
        evaluate(session_factory, w)

        again = evaluate(session_factory, w, now=NOW + timedelta(days=1))

        assert again.held is False
        assert len(holds(su, w)) == 1 and pending(su, w) == 1

    def test_only_the_affected_mailbox_is_held(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        healthy = new_mailbox(su, w)
        add_sent(su, w, sent=40, bounced=4)
        add_sent(su, w, sent=60, bounced=0, mailbox_id=healthy)

        evaluate(session_factory, w)
        evaluate(session_factory, w, mailbox_id=healthy)

        assert len(holds(su, w)) == 1
        assert holds(su, w, healthy) == [] and pending(su, w, healthy) == 0

    def test_another_workspaces_sends_are_not_counted(self, su, session_factory):
        mine = seed_world(su, running=False, members=1)
        theirs = seed_world(su, running=False, members=1)
        add_sent(su, theirs, sent=40, bounced=40)
        add_sent(su, mine, sent=40, bounced=0)

        result = evaluate(session_factory, mine)

        assert (result.sent, result.bounced, result.held) == (40, 0, False)
        assert holds(su, theirs) == []

    def test_a_mailbox_with_no_sends_is_fine(self, su, session_factory):
        w = seed_world(su, running=False, members=1)

        result = evaluate(session_factory, w)

        assert (result.sent, result.bounced, result.level, result.held) == (0, 0, None, False)


class TestReleaseAndEvaluateTogether:
    def test_a_release_holds_for_the_rest_of_the_day_and_returns_the_next(self, su, session_factory):
        """A person who releases a hold is not overridden five minutes later; if the
        rate is still critical the next day, the mailbox is held again."""
        w = seed_world(su, running=False, members=1)
        add_sent(su, w, sent=40, bounced=4)
        evaluate(session_factory, w)
        su.execute(
            "UPDATE public.safety_holds SET status='RESOLVED', resolved_at=now() WHERE workspace_id=%s",
            [w.ws],
        )
        assert pending(su, w) == 0

        same_day = evaluate(session_factory, w, now=NOW + timedelta(hours=3))
        next_day = evaluate(session_factory, w, now=NOW + timedelta(days=1))

        assert same_day.held is False
        assert next_day.held is True
        assert len(holds(su, w)) == 2 and len(holds(su, w, status="ACTIVE")) == 1
        assert pending(su, w) == 1


@pytest.fixture()
def client_as(session_factory):
    def make(w: World, user: uuid.UUID, role: str) -> TestClient:
        def override_db():
            with session_factory() as session:
                session.execute(text("SET LOCAL ROLE app_api"))
                session.execute(
                    text("SELECT set_config('app.user_id', :u, true), set_config('app.workspace_id', :w, true)"),
                    {"u": str(user), "w": str(w.ws)},
                )
                yield session
                session.commit()

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_workspace_context] = lambda: WorkspaceContext(
            workspace_id=w.ws, user_id=user, role_code=role
        )
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


def held_world(su, session_factory) -> tuple[World, uuid.UUID]:
    w = seed_world(su, running=False, members=1)
    add_sent(su, w, sent=40, bounced=4)
    evaluate(session_factory, w)
    hold_id = scalar(su, "SELECT id FROM public.safety_holds WHERE workspace_id=%s", [w.ws])
    return w, hold_id


def holds_url(w: World, mailbox_id=None) -> str:
    return f"/api/v1/workspaces/{w.ws}/mailboxes/{mailbox_id or w.mailbox_id}/safety-holds"


class TestHoldEndpoints:
    def test_a_member_can_see_why_the_mailbox_is_held(self, su, session_factory, client_as):
        w, hold_id = held_world(su, session_factory)

        response = client_as(w, w.member, "MEMBER").get(holds_url(w))

        assert response.status_code == 200, response.text
        [hold] = response.json()
        assert hold["id"] == str(hold_id)
        assert hold["kind"] == "HIGH_BOUNCE_RATE" and hold["status"] == "ACTIVE"
        assert "10.0%" in hold["reason"]

    def test_a_manager_releases_the_hold_and_it_is_audited(self, su, session_factory, client_as):
        w, hold_id = held_world(su, session_factory)

        response = client_as(w, w.manager, "MANAGER").post(f"{holds_url(w)}/{hold_id}/release")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "RESOLVED" and response.json()["resolved_at"]
        assert pending(su, w) == 0  # the mailbox can send again
        assert holds(su, w, status="ACTIVE") == []
        audit = su.execute(
            "SELECT actor_kind, actor_id, action, target_type, target_id, after_state "
            "FROM public.audit_events WHERE workspace_id=%s AND action='mailbox.safety_hold_released'",
            [w.ws],
        ).fetchall()
        assert len(audit) == 1
        kind, actor, _, target_type, target, state = audit[0]
        assert (kind, actor, target_type, target) == ("USER", w.manager, "mailbox", w.mailbox_id)
        assert state["hold_id"] == str(hold_id) and state["kind"] == "HIGH_BOUNCE_RATE"

    def test_the_listing_is_empty_after_the_release(self, su, session_factory, client_as):
        w, hold_id = held_world(su, session_factory)
        client = client_as(w, w.manager, "MANAGER")
        client.post(f"{holds_url(w)}/{hold_id}/release")

        assert client.get(holds_url(w)).json() == []

    def test_releasing_twice_is_harmless_and_audited_once(self, su, session_factory, client_as):
        w, hold_id = held_world(su, session_factory)
        client = client_as(w, w.manager, "MANAGER")

        first = client.post(f"{holds_url(w)}/{hold_id}/release")
        second = client.post(f"{holds_url(w)}/{hold_id}/release")

        assert (first.status_code, second.status_code) == (200, 200)
        assert second.json()["status"] == "RESOLVED"
        assert scalar(
            su, "SELECT count(*) FROM public.audit_events WHERE workspace_id=%s AND action='mailbox.safety_hold_released'",
            [w.ws],
        ) == 1
        assert pending(su, w) == 0

    def test_a_member_cannot_release(self, su, session_factory, client_as):
        w, hold_id = held_world(su, session_factory)

        response = client_as(w, w.member, "MEMBER").post(f"{holds_url(w)}/{hold_id}/release")

        assert response.status_code == 403
        assert pending(su, w) == 1

    def test_an_unknown_hold_is_a_404(self, su, session_factory, client_as):
        w, _ = held_world(su, session_factory)

        response = client_as(w, w.manager, "MANAGER").post(f"{holds_url(w)}/{uuid.uuid4()}/release")

        assert response.status_code == 404

    def test_a_hold_cannot_be_released_through_another_mailbox_or_workspace(self, su, session_factory, client_as):
        mine, hold_id = held_world(su, session_factory)
        theirs = seed_world(su, running=False, members=1)

        # Their manager, their workspace, my hold id on their mailbox.
        wrong_mailbox = client_as(theirs, theirs.manager, "MANAGER").post(
            f"{holds_url(theirs)}/{hold_id}/release"
        )
        # Their manager reaching for my mailbox.
        wrong_workspace = client_as(theirs, theirs.manager, "MANAGER").post(
            f"{holds_url(theirs, mine.mailbox_id)}/{hold_id}/release"
        )

        assert (wrong_mailbox.status_code, wrong_workspace.status_code) == (404, 404)
        assert pending(su, mine) == 1 and len(holds(su, mine, status="ACTIVE")) == 1

    def test_listing_an_unknown_mailbox_is_a_404(self, su, session_factory, client_as):
        w = seed_world(su, running=False, members=1)

        assert client_as(w, w.manager, "MANAGER").get(holds_url(w, uuid.uuid4())).status_code == 404
