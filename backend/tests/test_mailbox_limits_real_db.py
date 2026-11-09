"""Mailbox sending limits and safety-hold release on a REAL PostgreSQL.

The database starts at migration 0037 (what production has), is seeded with
mailboxes that have no limit, and then 0038 is applied, exactly as the owner will.
Everything below runs as the real roles (app_api, app_connection) so row-level
security and column grants are enforced. Nothing here touches a shared database.
"""

# ruff: noqa: E402, E501 -- imports follow importorskip; long lines are SQL.
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.modules.rate_limit.policy_service import (
    DAILY_WINDOW_SECONDS,
    MAILBOX_LIMIT_UNIT,
    MailboxLimitsIn,
    MailboxLimitsService,
    insert_default_mailbox_limit,
)
from tests.support.real_pg import MIGRATIONS, apply_range, throwaway_database
from tests.support.real_seed import World, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

LIMIT_SQL = (
    "SELECT limit_value, min_spacing_seconds, window_seconds, window_kind "
    "FROM public.tenant_rate_policies WHERE workspace_id=%s AND mailbox_id=%s AND kind='MAILBOX'"
)


@pytest.fixture(scope="module")
def database():
    """(uri, worlds seeded before 0038, how many limit rows existed before 0038)."""
    with throwaway_database(up_to=37) as uri:
        with psycopg.connect(uri, autocommit=True) as su:
            legacy = [seed_world(su, running=False, members=1) for _ in range(2)]
            before = su.execute("SELECT count(*) FROM public.tenant_rate_policies").fetchone()[0]
        apply_range(uri, 38, 38)
        yield uri, legacy, before


@pytest.fixture(scope="module")
def uri(database):
    return database[0]


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


@contextmanager
def acting(session_factory, w: World, user: uuid.UUID, role: str = "app_api", workspace=None):
    """A session as a real role, for one user, scoped to one workspace."""
    with session_factory() as session:
        session.execute(text(f"SET LOCAL ROLE {role}"))
        session.execute(
            text("SELECT set_config('app.user_id', :u, true), set_config('app.workspace_id', :w, true)"),
            {"u": str(user), "w": str(workspace or w.ws)},
        )
        yield session
        session.commit()


def ctx(w: World, user: uuid.UUID | None = None) -> SimpleNamespace:
    # What the service reads from a WorkspaceContext: the workspace and, for the
    # audit log, the acting user.
    return SimpleNamespace(workspace_id=w.ws, user_id=user or w.manager)


def limit_row(su, w: World, mailbox_id=None):
    return su.execute(LIMIT_SQL, [w.ws, mailbox_id or w.mailbox_id]).fetchall()


def new_mailbox(su, w: World) -> uuid.UUID:
    """A mailbox created without a limit, as if by code that predates provisioning."""
    mailbox_id = uuid.uuid4()
    su.execute(
        "INSERT INTO public.mailboxes (id, workspace_id, provider, original_address) "
        "VALUES (%s, %s, 'SMTP', %s)",
        [mailbox_id, w.ws, f"extra-{mailbox_id.hex[:6]}@acme.test"],
    )
    return mailbox_id


class TestMigration0038:
    def test_nothing_had_a_limit_before(self, database):
        assert database[2] == 0

    def test_backfills_a_default_limit_for_every_existing_mailbox(self, database, su):
        for w in database[1]:
            assert limit_row(su, w) == [(50, 60, DAILY_WINDOW_SECONDS, "ROLLING")]

    def test_re_applying_is_idempotent(self, uri, su):
        count = su.execute("SELECT count(*) FROM public.tenant_rate_policies").fetchone()[0]

        # Only this migration: the RevenueOS 0038 shares the number, refuses a
        # second run on purpose, and is not what this test is about.
        with psycopg.connect(uri, autocommit=True) as conn:
            conn.execute(
                (MIGRATIONS / "0038_golive_limits_and_hold_release.sql").read_bytes()
            )

        assert su.execute("SELECT count(*) FROM public.tenant_rate_policies").fetchone()[0] == count

    def test_default_matches_the_settings_used_for_new_mailboxes(self):
        # 0038 hard-codes 50 / 60; a new mailbox uses the settings. They must agree.
        settings = Settings.current()
        assert settings.mailbox_default_daily_cap == 50
        assert settings.mailbox_default_min_spacing_seconds == 60


class TestChangingALimit:
    def test_a_manager_reads_and_changes_their_mailbox_limit(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        insert_default(su, w)

        with acting(session_factory, w, w.manager) as s:
            before = MailboxLimitsService(s).get(ctx(w), w.mailbox_id)
            after = MailboxLimitsService(s).set(
                ctx(w), w.mailbox_id, MailboxLimitsIn(daily_cap=120, min_spacing_seconds=90)
            )

        assert (before.daily_cap, before.min_spacing_seconds, before.configured) == (50, 60, True)
        assert (after.daily_cap, after.min_spacing_seconds) == (120, 90)
        assert limit_row(su, w) == [(120, 90, DAILY_WINDOW_SECONDS, "ROLLING")]

    def test_a_manager_can_create_the_limit_when_none_exists(self, su, session_factory):
        w = seed_world(su, running=False, members=1)

        with acting(session_factory, w, w.manager) as s:
            unset = MailboxLimitsService(s).get(ctx(w), w.mailbox_id)
            made = MailboxLimitsService(s).set(
                ctx(w), w.mailbox_id, MailboxLimitsIn(daily_cap=30, min_spacing_seconds=45)
            )

        assert unset.configured is False and unset.daily_cap is None
        assert made.daily_cap == 30
        assert limit_row(su, w) == [(30, 45, DAILY_WINDOW_SECONDS, "ROLLING")]

    def test_a_member_may_read_but_not_change_a_limit(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        insert_default(su, w)

        with acting(session_factory, w, w.member) as s:
            assert MailboxLimitsService(s).get(ctx(w), w.mailbox_id).daily_cap == 50

        with pytest.raises(DBAPIError):
            with acting(session_factory, w, w.member) as s:
                MailboxLimitsService(s).set(
                    ctx(w), w.mailbox_id, MailboxLimitsIn(daily_cap=500, min_spacing_seconds=0)
                )

        assert limit_row(su, w) == [(50, 60, DAILY_WINDOW_SECONDS, "ROLLING")]

    def test_a_manager_cannot_write_workspace_or_campaign_policies(self, su, session_factory):
        """Only mailbox limits opened up to managers; other policies stay ADMIN-only."""
        w = seed_world(su, running=False, members=1)

        with pytest.raises(DBAPIError):
            with acting(session_factory, w, w.manager) as s:
                s.execute(
                    text(
                        "INSERT INTO public.tenant_rate_policies "
                        "(workspace_id, kind, unit, window_seconds, limit_value, window_kind) "
                        "VALUES (:w, 'WORKSPACE', 'MESSAGE', 86400, 100000, 'ROLLING')"
                    ),
                    {"w": str(w.ws)},
                )

    def test_another_tenant_cannot_see_or_change_this_limit(self, su, session_factory):
        mine = seed_world(su, running=False, members=1)
        theirs = seed_world(su, running=False, members=1)
        insert_default(su, theirs)

        # Their manager, in their workspace, asking about MY mailbox: it does not exist for them.
        with acting(session_factory, theirs, theirs.manager) as s:
            with pytest.raises(AppError) as err:
                MailboxLimitsService(s).get(ctx(theirs), mine.mailbox_id)
        assert err.value.status_code == 404

        # Pointing the policy row at my workspace is refused by row-level security.
        with pytest.raises(DBAPIError):
            with acting(session_factory, theirs, theirs.manager) as s:
                s.execute(
                    text(
                        "INSERT INTO public.tenant_rate_policies "
                        "(workspace_id, mailbox_id, kind, unit, window_seconds, limit_value, window_kind) "
                        "VALUES (:w, :m, 'MAILBOX', 'MESSAGE', 86400, 500, 'ROLLING')"
                    ),
                    {"w": str(mine.ws), "m": str(mine.mailbox_id)},
                )
        assert limit_row(su, mine) == []

    def test_out_of_range_values_are_rejected_before_the_database(self):
        for bad in ({"daily_cap": 0, "min_spacing_seconds": 0},
                    {"daily_cap": 501, "min_spacing_seconds": 0},
                    {"daily_cap": 10, "min_spacing_seconds": -1},
                    {"daily_cap": 10, "min_spacing_seconds": 3601}):
            with pytest.raises(ValueError):
                MailboxLimitsIn(**bad)


def insert_default(su, w: World) -> None:
    su.execute(
        "INSERT INTO public.tenant_rate_policies "
        "(workspace_id, mailbox_id, kind, unit, window_seconds, limit_value, window_kind, min_spacing_seconds) "
        "VALUES (%s, %s, 'MAILBOX', 'MESSAGE', 86400, 50, 'ROLLING', 60)",
        [w.ws, w.mailbox_id],
    )


class TestProvisioningOnConnect:
    """The role that connects a mailbox also gives it its default limit."""

    def test_connection_role_creates_the_default_limit(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        mailbox_id = new_mailbox(su, w)

        with acting(session_factory, w, w.manager, role="app_connection") as s:
            insert_default_mailbox_limit(s, workspace_id=w.ws, mailbox_id=mailbox_id)

        assert limit_row(su, w, mailbox_id) == [(50, 60, DAILY_WINDOW_SECONDS, "ROLLING")]

    def test_it_never_overwrites_a_limit_someone_already_set(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        su.execute(
            "INSERT INTO public.tenant_rate_policies "
            "(workspace_id, mailbox_id, kind, unit, window_seconds, limit_value, window_kind, min_spacing_seconds) "
            "VALUES (%s, %s, 'MAILBOX', %s, 86400, 7, 'ROLLING', 300)",
            [w.ws, w.mailbox_id, MAILBOX_LIMIT_UNIT],
        )

        for _ in range(2):  # a reconnect repeats it
            with acting(session_factory, w, w.manager, role="app_connection") as s:
                insert_default_mailbox_limit(s, workspace_id=w.ws, mailbox_id=w.mailbox_id)

        assert limit_row(su, w) == [(7, 300, DAILY_WINDOW_SECONDS, "ROLLING")]

    def test_a_member_cannot_provision_a_limit(self, su, session_factory):
        w = seed_world(su, running=False, members=1)

        with pytest.raises(DBAPIError):
            with acting(session_factory, w, w.member, role="app_connection") as s:
                insert_default_mailbox_limit(s, workspace_id=w.ws, mailbox_id=w.mailbox_id)

        assert limit_row(su, w) == []

    def test_the_connection_role_cannot_provision_into_another_workspace(self, su, session_factory):
        mine = seed_world(su, running=False, members=1)
        theirs = seed_world(su, running=False, members=1)

        with pytest.raises(DBAPIError):
            with acting(session_factory, theirs, theirs.manager, role="app_connection") as s:
                insert_default_mailbox_limit(s, workspace_id=mine.ws, mailbox_id=mine.mailbox_id)

        assert limit_row(su, mine) == []


def active_hold(su, w: World, identity: str = "auto-bounce:test") -> uuid.UUID:
    hold_id = uuid.uuid4()
    su.execute(
        "INSERT INTO public.safety_holds "
        "(id, workspace_id, source_work_identity, target_kind, target_mailbox_id, status, reason) "
        "VALUES (%s, %s, %s, 'MAILBOX', %s, 'ACTIVE', 'Bounce rate 12%%')",
        [hold_id, w.ws, identity, w.mailbox_id],
    )
    return hold_id


RELEASE = text(
    "UPDATE public.safety_holds SET status='RESOLVED', resolved_at=transaction_timestamp() "
    "WHERE id=:id AND workspace_id=:w AND status='ACTIVE'"
)


class TestReleasingASafetyHold:
    def test_a_manager_can_release_an_active_hold(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        hold = active_hold(su, w)

        with acting(session_factory, w, w.manager) as s:
            released = s.execute(RELEASE, {"id": str(hold), "w": str(w.ws)}).rowcount

        assert released == 1
        assert su.execute(
            "SELECT status, resolved_at IS NOT NULL FROM public.safety_holds WHERE id=%s", [hold]
        ).fetchone() == ("RESOLVED", True)

    def test_a_member_cannot_release_a_hold(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        hold = active_hold(su, w)

        with acting(session_factory, w, w.member) as s:
            released = s.execute(RELEASE, {"id": str(hold), "w": str(w.ws)}).rowcount

        assert released == 0
        assert scalar(su, "SELECT status FROM public.safety_holds WHERE id=%s", [hold]) == "ACTIVE"

    def test_another_workspace_cannot_release_it(self, su, session_factory):
        mine = seed_world(su, running=False, members=1)
        theirs = seed_world(su, running=False, members=1)
        hold = active_hold(su, mine)

        with acting(session_factory, theirs, theirs.manager) as s:
            released = s.execute(RELEASE, {"id": str(hold), "w": str(mine.ws)}).rowcount

        assert released == 0
        assert scalar(su, "SELECT status FROM public.safety_holds WHERE id=%s", [hold]) == "ACTIVE"

    def test_the_api_role_can_change_only_status_and_resolution(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        hold = active_hold(su, w)

        with pytest.raises(DBAPIError):
            with acting(session_factory, w, w.manager) as s:
                s.execute(
                    text("UPDATE public.safety_holds SET reason='nothing to see' WHERE id=:id"),
                    {"id": str(hold)},
                )

    def test_a_released_hold_cannot_be_reopened_by_the_api_role(self, su, session_factory):
        w = seed_world(su, running=False, members=1)
        hold = active_hold(su, w)
        with acting(session_factory, w, w.manager) as s:
            s.execute(RELEASE, {"id": str(hold), "w": str(w.ws)})

        # The policy only matches ACTIVE rows, so the update touches nothing.
        with acting(session_factory, w, w.manager) as s:
            reopened = s.execute(
                text("UPDATE public.safety_holds SET status='ACTIVE', resolved_at=NULL WHERE id=:id"),
                {"id": str(hold)},
            ).rowcount

        assert reopened == 0
        assert scalar(su, "SELECT status FROM public.safety_holds WHERE id=%s", [hold]) == "RESOLVED"


def scalar(su, sql: str, params: list | None = None):
    return su.execute(sql, params or []).fetchone()[0]


class TestWhatPlanningReads:
    """The render worker's queries, as `app_worker_general`, on the real schema."""

    def _repo(self, session, w: World):
        from app.modules.campaigns.worker_repository import CampaignWorkerRepository

        session.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(w.ws)}
        )
        return CampaignWorkerRepository(session)

    def test_mailbox_limit_rows_come_back_per_mailbox(self, su, session_factory):
        w = seed_world(su, running=True, members=2)
        insert_default(su, w)
        bare = new_mailbox(su, w)  # a mailbox with no limit is simply absent

        with session_factory() as s:
            found = self._repo(s, w).list_mailbox_limit_policies(
                workspace_id=w.ws, mailbox_ids=[w.mailbox_id, bare]
            )

        assert found == {
            w.mailbox_id: [
                {"window_seconds": 86_400, "limit_value": 50, "min_spacing_seconds": 60}
            ]
        }

    def test_another_workspaces_limits_are_not_visible(self, su, session_factory):
        mine = seed_world(su, running=True, members=1)
        theirs = seed_world(su, running=True, members=1)
        insert_default(su, theirs)

        with session_factory() as s:
            found = self._repo(s, mine).list_mailbox_limit_policies(
                workspace_id=mine.ws, mailbox_ids=[theirs.mailbox_id]
            )

        assert found == {}

    def test_no_mailboxes_means_no_query(self, su, session_factory):
        w = seed_world(su, running=True, members=1)

        with session_factory() as s:
            assert self._repo(s, w).list_mailbox_limit_policies(
                workspace_id=w.ws, mailbox_ids=[]
            ) == {}

    def test_planned_messages_carry_the_recipients_capture_ordinal(self, su, session_factory):
        w = seed_world(su, running=True, members=1)

        with session_factory() as s:
            rows = self._repo(s, w).fetch_planned_messages_chunk(
                workspace_id=w.ws, campaign_id=w.campaign_id, limit=10
            )

        assert [(r["id"], r["capture_ordinal"]) for r in rows] == [(w.message1_id, 1)]

    def test_the_activation_context_has_the_campaign_daily_limit(self, su, session_factory):
        w = seed_world(su, running=True, members=1)
        su.execute(
            "UPDATE public.campaign_settings_versions SET daily_limit = 120 WHERE id = %s",
            [w.settings_id],
        )
        activation = scalar(su, "SELECT activation_id FROM public.campaigns WHERE id=%s", [w.campaign_id])

        with session_factory() as s:
            ctx = self._repo(s, w).get_activation_context(
                workspace_id=w.ws, campaign_id=w.campaign_id, activation_id=activation
            )

        assert ctx is not None and ctx["daily_limit"] == 120


class TestLimitsEndpoints:
    """GET/PUT /workspaces/{ws}/mailboxes/{id}/limits through the real app, with
    the real `app_api` role underneath (only authentication is stood in for)."""

    @pytest.fixture()
    def client_as(self, session_factory):
        from fastapi.testclient import TestClient

        from app.api.deps import WorkspaceContext, get_db, get_workspace_context
        from app.main import app

        def make(w: World, user: uuid.UUID, role: str):
            def override_db():
                with session_factory() as session:
                    session.execute(text("SET LOCAL ROLE app_api"))
                    session.execute(
                        text(
                            "SELECT set_config('app.user_id', :u, true), "
                            "set_config('app.workspace_id', :w, true)"
                        ),
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

    @staticmethod
    def url(w: World, mailbox_id=None) -> str:
        return f"/api/v1/workspaces/{w.ws}/mailboxes/{mailbox_id or w.mailbox_id}/limits"

    def test_a_manager_reads_then_changes_the_limits(self, su, client_as):
        w = seed_world(su, running=False, members=1)
        insert_default(su, w)
        client = client_as(w, w.manager, "MANAGER")

        read = client.get(self.url(w))
        saved = client.put(self.url(w), json={"daily_cap": 80, "min_spacing_seconds": 45})
        again = client.get(self.url(w))

        assert read.status_code == 200, read.text
        assert read.json() | {"max_daily_cap": 500} == {
            "daily_cap": 50, "min_spacing_seconds": 60, "configured": True,
            "default_daily_cap": 50, "default_min_spacing_seconds": 60,
            "max_daily_cap": 500, "max_spacing_seconds": 3600,
        }
        assert saved.status_code == 200, saved.text
        assert (again.json()["daily_cap"], again.json()["min_spacing_seconds"]) == (80, 45)
        assert limit_row(su, w) == [(80, 45, DAILY_WINDOW_SECONDS, "ROLLING")]

    def test_a_mailbox_with_no_limit_reads_as_not_configured(self, su, client_as):
        w = seed_world(su, running=False, members=1)
        client = client_as(w, w.manager, "MANAGER")

        body = client.get(self.url(w)).json()

        assert body["configured"] is False and body["daily_cap"] is None

    def test_a_member_may_read_but_is_refused_a_change(self, su, client_as):
        w = seed_world(su, running=False, members=1)
        insert_default(su, w)
        client = client_as(w, w.member, "MEMBER")

        assert client.get(self.url(w)).status_code == 200
        refused = client.put(self.url(w), json={"daily_cap": 500, "min_spacing_seconds": 0})

        assert refused.status_code == 403
        assert limit_row(su, w) == [(50, 60, DAILY_WINDOW_SECONDS, "ROLLING")]

    @pytest.mark.parametrize(
        "payload",
        [
            {"daily_cap": 0, "min_spacing_seconds": 60},
            {"daily_cap": 501, "min_spacing_seconds": 60},
            {"daily_cap": 50, "min_spacing_seconds": -1},
            {"daily_cap": 50, "min_spacing_seconds": 3601},
            {"daily_cap": "many", "min_spacing_seconds": 60},
            {"daily_cap": 50},
        ],
    )
    def test_bad_values_are_a_422_and_change_nothing(self, su, client_as, payload):
        w = seed_world(su, running=False, members=1)
        insert_default(su, w)
        client = client_as(w, w.manager, "MANAGER")

        assert client.put(self.url(w), json=payload).status_code == 422
        assert limit_row(su, w) == [(50, 60, DAILY_WINDOW_SECONDS, "ROLLING")]

    def test_an_unknown_mailbox_is_a_404(self, su, client_as):
        w = seed_world(su, running=False, members=1)
        client = client_as(w, w.manager, "MANAGER")
        missing = uuid.uuid4()

        assert client.get(self.url(w, missing)).status_code == 404
        put = client.put(self.url(w, missing), json={"daily_cap": 10, "min_spacing_seconds": 0})
        assert put.status_code == 404

    def test_another_tenants_mailbox_is_a_404_and_stays_untouched(self, su, client_as):
        mine = seed_world(su, running=False, members=1)
        theirs = seed_world(su, running=False, members=1)
        insert_default(su, theirs)
        client = client_as(mine, mine.manager, "MANAGER")

        read = client.get(self.url(mine, theirs.mailbox_id))
        put = client.put(
            self.url(mine, theirs.mailbox_id), json={"daily_cap": 500, "min_spacing_seconds": 0}
        )

        assert (read.status_code, put.status_code) == (404, 404)
        assert limit_row(su, theirs) == [(50, 60, DAILY_WINDOW_SECONDS, "ROLLING")]
