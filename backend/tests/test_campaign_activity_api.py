from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import WorkspaceContext, get_db, get_workspace_context
from app.main import create_app

_TABLES = [
    "CREATE TABLE public.campaigns (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,"
    " name TEXT NOT NULL)",
    "CREATE TABLE public.recipient_addresses (id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL, canonical_address TEXT NOT NULL)",
    "CREATE TABLE public.sequence_steps (id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL, position INTEGER NOT NULL)",
    "CREATE TABLE public.campaign_enrollments (id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL, campaign_id TEXT NOT NULL, lead_id TEXT NOT NULL,"
    " address_id TEXT NOT NULL, frozen_destination TEXT NOT NULL)",
    "CREATE TABLE public.messages (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,"
    " campaign_id TEXT, enrollment_id TEXT, step_id TEXT, address_id TEXT NOT NULL,"
    " status TEXT NOT NULL, frozen_destination TEXT, content_subject TEXT,"
    " content_body_html TEXT, terminal_reason TEXT, accepted_at TIMESTAMP,"
    " updated_at TIMESTAMP)",
    "CREATE TABLE public.message_attempts (id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL, message_id TEXT NOT NULL, ordinal INTEGER NOT NULL,"
    " evidence_state TEXT NOT NULL, error_category TEXT, error_code TEXT,"
    " completed_at TIMESTAMP)",
    "CREATE TABLE public.message_events (id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL, message_id TEXT NOT NULL, kind TEXT NOT NULL,"
    " bounce_type TEXT, bounce_code TEXT, detail TEXT,"
    " first_occurred_at TIMESTAMP NOT NULL,"
    " occurrence_count INTEGER NOT NULL DEFAULT 1,"
    " qualified_at TIMESTAMP, qualified_count INTEGER NOT NULL DEFAULT 0,"
    " last_user_agent TEXT)",
    "CREATE TABLE public.recipient_outcomes (id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL, enrollment_id TEXT NOT NULL, kind TEXT NOT NULL,"
    " occurred_at TIMESTAMP NOT NULL)",
    "CREATE TABLE public.inbound_messages (id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL, subject TEXT, content_text TEXT,"
    " classification TEXT, received_at TIMESTAMP NOT NULL)",
    "CREATE TABLE public.inbound_outreach_links (id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL, inbound_message_id TEXT NOT NULL,"
    " outbound_message_id TEXT NOT NULL, campaign_id TEXT, enrollment_id TEXT,"
    " status TEXT NOT NULL)",
]

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _id() -> str:
    return str(uuid.uuid4())


class World:
    def __init__(self, session, ws_id: uuid.UUID) -> None:
        self.session = session
        self.ws = str(ws_id)
        self.campaign = self.add_campaign(self.ws)
        self.step = _id()
        self._exec(
            "INSERT INTO public.sequence_steps VALUES (:id, :ws, 1)",
            id=self.step,
            ws=self.ws,
        )

    def _exec(self, sql: str, **params) -> None:
        self.session.execute(text(sql), params)

    def add_campaign(self, ws: str) -> str:
        cid = _id()
        self._exec("INSERT INTO public.campaigns VALUES (:id, :ws, 'C')", id=cid, ws=ws)
        return cid

    def add_recipient(self, email: str, *, ws: str | None = None, campaign=None):
        ws = ws or self.ws
        campaign = campaign or self.campaign
        addr, enr, lead = _id(), _id(), _id()
        self._exec(
            "INSERT INTO public.recipient_addresses VALUES (:id, :ws, :e)",
            id=addr,
            ws=ws,
            e=email,
        )
        self._exec(
            "INSERT INTO public.campaign_enrollments VALUES (:id, :ws, :c, :l, :a, :e)",
            id=enr,
            ws=ws,
            c=campaign,
            l=lead,
            a=addr,
            e=email,
        )
        return addr, enr, lead

    def add_message(
        self,
        email: str,
        *,
        status="SENT",
        at=NOW,
        subject="Hello there",
        ws: str | None = None,
        campaign=None,
        terminal_reason=None,
        recipient=None,
    ) -> dict:
        ws = ws or self.ws
        campaign = campaign or self.campaign
        addr, enr, lead = recipient or self.add_recipient(
            email, ws=ws, campaign=campaign
        )
        mid = _id()
        self._exec(
            "INSERT INTO public.messages (id, workspace_id, campaign_id, enrollment_id,"
            " step_id, address_id, status, frozen_destination, content_subject,"
            " content_body_html, terminal_reason, accepted_at, updated_at)"
            " VALUES (:id, :ws, :c, :enr, :st, :a, :s, :d, :sub, '<p>SECRET</p>',"
            " :tr, :at, :at)",
            id=mid,
            ws=ws,
            c=campaign,
            enr=enr,
            st=self.step,
            a=addr,
            s=status,
            d=email,
            sub=subject,
            tr=terminal_reason,
            at=at if status == "SENT" else None,
        )
        if status != "SENT":
            self._exec(
                "UPDATE public.messages SET updated_at = :at WHERE id = :id",
                at=at,
                id=mid,
            )
        return {"id": mid, "enr": enr, "lead": lead, "addr": addr, "ws": ws}

    def add_event(self, message: dict, kind: str, at=NOW, **cols) -> None:
        self._exec(
            "INSERT INTO public.message_events (id, workspace_id, message_id, kind,"
            " bounce_type, bounce_code, detail, first_occurred_at, occurrence_count,"
            " qualified_at, qualified_count, last_user_agent)"
            " VALUES (:id, :ws, :m, :k, :bt, :bc, :d, :at, :oc, :qa, :qc, 'UA-SECRET')",
            id=_id(),
            ws=message["ws"],
            m=message["id"],
            k=kind,
            bt=cols.get("bounce_type"),
            bc=cols.get("bounce_code"),
            d=cols.get("detail"),
            at=at,
            oc=cols.get("occurrence_count", 1),
            qa=cols.get("qualified_at"),
            qc=cols.get("qualified_count", 0),
        )

    def add_attempt(self, message: dict, ordinal: int, state: str, at, **cols) -> None:
        self._exec(
            "INSERT INTO public.message_attempts VALUES (:id, :ws, :m, :o, :s, :ec,"
            " :code, :at)",
            id=_id(),
            ws=message["ws"],
            m=message["id"],
            o=ordinal,
            s=state,
            ec=cols.get("error_category"),
            code=cols.get("error_code"),
            at=at,
        )

    def add_outcome(self, message: dict, kind: str, at=NOW) -> None:
        self._exec(
            "INSERT INTO public.recipient_outcomes VALUES (:id, :ws, :e, :k, :at)",
            id=_id(),
            ws=message["ws"],
            e=message["enr"],
            k=kind,
            at=at,
        )

    def add_reply(
        self,
        message: dict,
        *,
        at=NOW,
        classification="HUMAN_REPLY",
        status="CONFIRMED",
        links=1,
        body="Thanks, let's talk",
    ) -> None:
        inbound = _id()
        self._exec(
            "INSERT INTO public.inbound_messages VALUES (:id, :ws, 'Re: Hello',"
            " :b, :cl, :at)",
            id=inbound,
            ws=message["ws"],
            b=body,
            cl=classification,
            at=at,
        )
        for _ in range(links):
            self._exec(
                "INSERT INTO public.inbound_outreach_links VALUES (:id, :ws, :i, :o,"
                " :c, :e, :s)",
                id=_id(),
                ws=message["ws"],
                i=inbound,
                o=message["id"],
                c=self.campaign,
                e=message["enr"],
                s=status,
            )


@pytest.fixture
def activity_setup():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    session = sessionmaker(bind=engine)()
    session.execute(text("ATTACH DATABASE ':memory:' AS public;"))
    for ddl in _TABLES:
        session.execute(text(ddl))
    session.commit()

    ws_id = uuid.uuid4()
    app = create_app()

    def override_get_db():
        yield session

    def override_ctx(workspace_id: uuid.UUID = ws_id):
        return WorkspaceContext(
            workspace_id=workspace_id, user_id=uuid.uuid4(), role_code="MANAGER"
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_workspace_context] = override_ctx
    world = World(session, ws_id)
    yield world, TestClient(app), ws_id
    app.dependency_overrides.clear()
    session.close()


def _get(setup, campaign=None, **params):
    world, client, ws_id = setup
    campaign = campaign or world.campaign
    world.session.commit()
    return client.get(
        f"/api/v1/workspaces/{ws_id}/analytics/campaigns/{campaign}/activity",
        params=params,
    )


def _seed_full(world: World) -> None:
    """One recipient per activity type, each a minute apart (newest last)."""
    t = lambda n: NOW - timedelta(minutes=10 - n)  # noqa: E731
    sent = world.add_message("sent@x.test", at=t(1))
    opened = world.add_message("open@x.test", at=t(2))
    world.add_event(
        opened,
        "OPENED",
        at=t(3),
        qualified_at=t(3),
        qualified_count=4,
        occurrence_count=9,
    )
    bounced = world.add_message("bounce@x.test", at=t(2))
    world.add_event(
        bounced,
        "BOUNCED",
        at=t(4),
        bounce_type="HARD",
        bounce_code="550",
        detail="No such user",
    )
    replied = world.add_message("reply@x.test", at=t(2))
    world.add_reply(replied, at=t(5))
    unsub = world.add_message("unsub@x.test", at=t(2))
    world.add_outcome(unsub, "UNSUBSCRIBED", at=t(6))
    failed = world.add_message(
        "fail@x.test",
        status="FAILED",
        at=t(7),
        terminal_reason="retry_budget_exhausted",
    )
    world.add_attempt(
        failed, 1, "REJECTED", t(7), error_category="RATE_LIMIT", error_code="429"
    )
    assert sent


def test_feed_lists_each_type_once_newest_first(activity_setup) -> None:
    _seed_full(activity_setup[0])
    resp = _get(activity_setup, limit=100)
    assert resp.status_code == 200
    body = resp.json()
    items = body["items"]
    times = [i["occurred_at"] for i in items]
    assert times == sorted(times, reverse=True)
    by_email = {}
    for i in items:
        by_email.setdefault(i["recipient_email"], []).append(i["type"])
    assert by_email["sent@x.test"] == ["SENT"]
    assert "OPENED" in by_email["open@x.test"]
    assert by_email["bounce@x.test"].count("BOUNCED") == 1
    assert "REPLIED" in by_email["reply@x.test"]
    assert "UNSUBSCRIBED" in by_email["unsub@x.test"]
    assert by_email["fail@x.test"] == ["FAILED"]
    assert body["counts_by_type"]["SENT"] == 5
    assert body["click_tracking_supported"] is False
    assert body["delivery_confirmation_supported"] is False


def test_open_is_one_entry_with_count_and_ignores_unqualified_hits(
    activity_setup,
) -> None:
    world = activity_setup[0]
    human = world.add_message("human@x.test")
    world.add_event(
        human, "OPENED", qualified_at=NOW, qualified_count=3, occurrence_count=20
    )
    bot = world.add_message("bot@x.test")
    world.add_event(bot, "OPENED", occurrence_count=5)  # scanner only
    items = _get(activity_setup, type="OPENED").json()["items"]
    assert len(items) == 1
    assert items[0]["open_count"] == 3
    assert items[0]["detail"] == "Opened 3 times"


def test_bounce_details_and_status(activity_setup) -> None:
    world = activity_setup[0]
    hard = world.add_message("hard@x.test")
    world.add_event(
        hard,
        "BOUNCED",
        bounce_type="HARD",
        bounce_code="550",
        detail="No such user",
        occurrence_count=3,
    )
    soft = world.add_message("soft@x.test")
    world.add_event(soft, "BOUNCED", bounce_type="SOFT", bounce_code="452")
    items = {
        i["recipient_email"]: i
        for i in _get(activity_setup, type="BOUNCED").json()["items"]
    }
    assert len(items) == 2
    assert items["hard@x.test"]["status"] == "ERROR"
    assert items["hard@x.test"]["detail"] == "Hard bounce · 550 · No such user"
    assert items["soft@x.test"]["status"] == "WARNING"


def test_reply_deduped_per_inbound_and_bounce_notices_excluded(activity_setup) -> None:
    world = activity_setup[0]
    msg = world.add_message("r@x.test")
    world.add_reply(msg, links=2)  # one inbound linked to two outbound rows
    world.add_reply(world.add_message("ooo@x.test"), classification="OUT_OF_OFFICE")
    world.add_reply(world.add_message("dsn@x.test"), classification="BOUNCE")
    world.add_reply(world.add_message("cand@x.test"), status="CANDIDATE")
    items = _get(activity_setup, type="REPLIED").json()["items"]
    emails = sorted(i["recipient_email"] for i in items)
    assert emails == ["ooo@x.test", "r@x.test"]
    ooo = next(i for i in items if i["recipient_email"] == "ooo@x.test")
    assert ooo["status"] == "INFO"
    assert ooo["detail"].startswith("Automatic reply")


def test_failed_and_retrying_with_details(activity_setup) -> None:
    world = activity_setup[0]
    failed = world.add_message("f@x.test", status="FAILED", terminal_reason="boom")
    world.add_attempt(
        failed,
        1,
        "REJECTED",
        NOW - timedelta(hours=2),
        error_category="TEMPORARY_PROVIDER_ERROR",
    )
    world.add_attempt(
        failed,
        2,
        "REJECTED",
        NOW,
        error_category="AUTH_FAILURE",
        error_code="invalid_grant",
    )
    retry = world.add_message("r@x.test", status="RETRY_SCHEDULED")
    world.add_attempt(retry, 1, "REJECTED", NOW, error_category="RATE_LIMIT")
    items = {
        i["recipient_email"]: i
        for i in _get(activity_setup, type="FAILED").json()["items"]
    }
    assert items["f@x.test"]["status"] == "ERROR"
    assert items["f@x.test"]["detail"] == (
        "Mailbox authentication failed · invalid_grant · boom"
    )
    assert items["r@x.test"]["status"] == "WARNING"
    assert items["r@x.test"]["detail"].endswith("will retry")


def test_filters_status_date_and_search(activity_setup) -> None:
    world = activity_setup[0]
    old = world.add_message("old_user@x.test", at=datetime(2026, 9, 1, 9, tzinfo=UTC))
    world.add_message(
        "new%user@x.test",
        at=datetime(2026, 10, 5, 9, tzinfo=UTC),
        subject="Quarterly plan",
    )
    world.add_event(
        old, "BOUNCED", at=datetime(2026, 9, 1, 10, tzinfo=UTC), bounce_type="HARD"
    )

    only_oct = _get(activity_setup, start_date="2026-10-01", end_date="2026-10-31")
    assert [i["recipient_email"] for i in only_oct.json()["items"]] == [
        "new%user@x.test"
    ]
    assert (
        _get(activity_setup, end_date="2026-09-01").json()["counts_by_type"]["BOUNCED"]
        == 1
    )
    assert [
        i["type"] for i in _get(activity_setup, status="ERROR").json()["items"]
    ] == ["BOUNCED"]
    # LIKE wildcards in the search term are literal.
    assert len(_get(activity_setup, q="w%u").json()["items"]) == 1
    assert _get(activity_setup, q="old_u").json()["items"][0]["type"] == "BOUNCED"
    assert _get(activity_setup, q="x_y").json()["items"] == []
    assert len(_get(activity_setup, q="QUARTERLY").json()["items"]) == 1


def test_counts_ignore_type_filter_and_only_on_first_page(activity_setup) -> None:
    _seed_full(activity_setup[0])
    first = _get(activity_setup, type="SENT", limit=2).json()
    assert first["counts_by_type"]["FAILED"] == 1
    assert len(first["items"]) == 2 and first["has_more"] is True
    nxt = _get(activity_setup, type="SENT", limit=2, cursor=first["next_cursor"]).json()
    assert nxt["counts_by_type"] is None


def test_pagination_walks_all_rows_once_including_timestamp_ties(
    activity_setup,
) -> None:
    world = activity_setup[0]
    for n in range(7):
        world.add_message(f"p{n}@x.test", at=NOW)  # identical timestamps
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = _get(activity_setup, **params).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == 7 and len(set(seen)) == 7


def test_response_never_leaks_body_or_user_agent(activity_setup) -> None:
    world = activity_setup[0]
    msg = world.add_message("leak@x.test")
    world.add_event(msg, "OPENED", qualified_at=NOW, qualified_count=1)
    raw = _get(activity_setup).text
    assert "SECRET" not in raw and "UA-SECRET" not in raw


def test_other_workspace_campaign_is_not_found(activity_setup) -> None:
    world, client, ws_id = activity_setup
    foreign = world.add_campaign(str(uuid.uuid4()))
    world.add_message("x@y.test", ws=str(uuid.uuid4()), campaign=foreign)
    assert _get(activity_setup, campaign=foreign).status_code == 404
    assert _get(activity_setup, campaign=str(uuid.uuid4())).status_code == 404


def test_other_workspace_rows_never_appear(activity_setup) -> None:
    world = activity_setup[0]
    other_ws = str(uuid.uuid4())
    world.add_message("leak@other.test", ws=other_ws, campaign=world.campaign)
    world.add_message("mine@x.test")
    emails = [i["recipient_email"] for i in _get(activity_setup).json()["items"]]
    assert emails == ["mine@x.test"]


@pytest.mark.parametrize(
    "params",
    [
        {"start_date": "nope"},
        {"end_date": "2026-13-45"},
        {"start_date": "2026-10-10", "end_date": "2026-10-01"},
        {"cursor": "!!not-a-cursor!!"},
        {"limit": 0},
        {"limit": 101},
        {"type": "CLICKED"},
        {"status": "MAYBE"},
    ],
)
def test_invalid_parameters_are_rejected(activity_setup, params) -> None:
    assert _get(activity_setup, **params).status_code == 422


def test_empty_campaign(activity_setup) -> None:
    body = _get(activity_setup).json()
    assert body["items"] == [] and body["has_more"] is False
    assert sum(body["counts_by_type"].values()) == 0
