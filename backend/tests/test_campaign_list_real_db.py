"""Campaign list filters, orderings and status counts against a REAL PostgreSQL
(pgserver).

Everything runs as the real `app_api` role, so RLS and grants apply: the type
filter, each optional sort with keyset paging, the creator's display name, the
per-status counts, and that none of it reaches another tenant's campaigns.
Nothing touches the production-linked Supabase project.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import WorkspaceContext, get_db, get_workspace_context
from app.core.errors import AppError
from app.db.context import set_transaction_context
from app.main import app
from app.modules.campaigns.service import CampaignService
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, _insert, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

SORTS = ["updated_desc", "created_desc", "name_asc"]


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


def api(session_factory, w: World, role: str = "MEMBER") -> tuple[Session, WorkspaceContext]:
    user = {"OWNER": w.owner, "MANAGER": w.manager, "MEMBER": w.member}[role]
    session = session_factory()
    session.execute(text("SET LOCAL ROLE app_api"))
    set_transaction_context(session, user_id=user, workspace_id=w.ws)
    return session, WorkspaceContext(workspace_id=w.ws, user_id=user, role_code=role)


def add_campaign(
    su,
    w: World,
    name: str,
    *,
    status: str = "DRAFT",
    campaign_type: str = "STANDARD",
    age_days: int = 0,
    creator: uuid.UUID | None = None,
) -> uuid.UUID:
    """A bare campaign row in any state (FKs and triggers are skipped)."""
    campaign_id = uuid.uuid4()
    stamp = datetime.now(UTC) - timedelta(days=age_days)
    row: dict = dict(
        id=campaign_id,
        workspace_id=w.ws,
        name=name,
        creator_id=creator or w.manager,
        status=status,
        campaign_type=campaign_type,
        created_at=stamp,
        updated_at=stamp,
    )
    # The table's shape checks still apply: an activated state needs its
    # activation fields, and an archived row its archive bookkeeping.
    if status == "ARCHIVED":
        row.update(archived_at=stamp, previous_status="DRAFT")
    elif status != "DRAFT":
        row.update(
            planning_status="READY",
            activation_id=uuid.uuid4(),
            activated_sequence_id=w.sequence_id,
            activated_audience_id=w.audience_id,
            current_settings_id=w.settings_id,
            start_at=stamp,
        )
    su.execute("SET session_replication_role = replica")
    _insert(su, "campaigns", **row)
    su.execute("SET session_replication_role = origin")
    return campaign_id


@pytest.fixture()
def world(su) -> World:
    """One tenant holding: the seeded hyper-personalized draft "Outbound", plus
    four standard campaigns of different ages and states."""
    w = seed_world(su, running=False)
    su.execute("SET session_replication_role = origin")
    su.execute("UPDATE public.profiles SET display_name='Mira Manager' WHERE id=%s", [w.manager])
    add_campaign(su, w, "alpha", age_days=4)
    add_campaign(su, w, "Bravo", age_days=3, status="PAUSED")
    add_campaign(su, w, "charlie", age_days=2, status="COMPLETED", creator=w.owner)
    add_campaign(su, w, "Delta", age_days=1, status="ARCHIVED")
    return w


def list_all(session_factory, w: World, *, limit: int, **filters) -> list:
    """Every page of a listing, following the cursor."""
    session, ctx = api(session_factory, w)
    try:
        items, cursor = [], None
        for _ in range(20):
            page = CampaignService(session).list_campaigns(
                ctx,
                limit=limit,
                cursor=cursor,
                query=filters.get("query"),
                status=filters.get("status"),
                campaign_type=filters.get("campaign_type"),
                sort=filters.get("sort"),
            )
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return items
        raise AssertionError("paging did not terminate")
    finally:
        session.close()


def test_default_listing_keeps_id_order_and_hides_archived(session_factory, world) -> None:
    items = list_all(session_factory, world, limit=2)
    assert [str(i.id) for i in items] == sorted(str(i.id) for i in items)
    assert {i.name for i in items} == {"Outbound", "alpha", "Bravo", "charlie"}


def test_type_filter_narrows_the_list(session_factory, world) -> None:
    hyper = list_all(session_factory, world, limit=50, campaign_type="HYPER_PERSONALIZED")
    assert [i.name for i in hyper] == ["Outbound"]
    standard = list_all(session_factory, world, limit=50, campaign_type="STANDARD")
    assert {i.name for i in standard} == {"alpha", "Bravo", "charlie"}


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("updated_desc", ["Outbound", "Delta", "charlie", "Bravo", "alpha"]),
        ("created_desc", ["Outbound", "Delta", "charlie", "Bravo", "alpha"]),
        ("name_asc", ["alpha", "Bravo", "charlie", "Delta", "Outbound"]),
    ],
)
def test_each_sort_pages_without_gaps_or_repeats(session_factory, world, sort, expected) -> None:
    items = list_all(session_factory, world, limit=2, sort=sort, status="ALL")
    assert [i.name for i in items] == expected


def test_sorted_paging_survives_equal_sort_keys(session_factory, su, world) -> None:
    for _ in range(3):
        add_campaign(su, world, "same name", age_days=9)
    names = [i.name for i in list_all(session_factory, world, limit=1, sort="name_asc", status="ALL")]
    assert names.count("same name") == 3 and len(names) == 8


def test_a_cursor_only_continues_its_own_ordering(session_factory, world) -> None:
    session, ctx = api(session_factory, world)
    service = CampaignService(session)

    def first_cursor(sort):
        page = service.list_campaigns(ctx, limit=1, cursor=None, query=None, status="ALL", sort=sort)
        assert page.next_cursor
        return page.next_cursor

    by_name, by_id = first_cursor("name_asc"), first_cursor(None)
    for cursor, sort in ((by_name, "updated_desc"), (by_id, "name_asc"), (by_name, None), ("garbage", "name_asc")):
        with pytest.raises(AppError) as info:
            service.list_campaigns(ctx, limit=1, cursor=cursor, query=None, status="ALL", sort=sort)
        assert info.value.status_code == 422
    session.close()


def test_list_items_carry_the_creator_name_and_start_time(session_factory, world) -> None:
    by_name = {i.name: i for i in list_all(session_factory, world, limit=50)}
    assert by_name["alpha"].creator_name == "Mira Manager"
    # The owner has no display name set.
    assert by_name["charlie"].creator_name is None
    assert by_name["alpha"].start_at is None
    assert by_name["Bravo"].start_at is not None


def test_counts_per_status_follow_the_filters(session_factory, world) -> None:
    session, ctx = api(session_factory, world)
    service = CampaignService(session)

    everything = service.count_campaigns(ctx, query=None)
    assert everything.by_status == {"DRAFT": 2, "PAUSED": 1, "COMPLETED": 1, "ARCHIVED": 1}
    assert (everything.not_archived, everything.total) == (4, 5)

    standard = service.count_campaigns(ctx, query=None, campaign_type="STANDARD")
    assert standard.by_status == {"DRAFT": 1, "PAUSED": 1, "COMPLETED": 1, "ARCHIVED": 1}

    searched = service.count_campaigns(ctx, query="  ALPH ")
    assert searched.by_status == {"DRAFT": 1} and searched.total == 1
    session.close()


def test_another_tenant_sees_none_of_it(session_factory, su, world) -> None:
    other = seed_world(su, running=False)
    su.execute("SET session_replication_role = origin")

    assert [i.name for i in list_all(session_factory, other, limit=50, status="ALL")] == ["Outbound"]
    mine = {str(i.id) for i in list_all(session_factory, world, limit=50, status="ALL")}
    theirs = {str(i.id) for i in list_all(session_factory, other, limit=50, status="ALL", sort="name_asc")}
    assert not mine & theirs

    session, ctx = api(session_factory, other)
    counts = CampaignService(session).count_campaigns(ctx, query=None)
    assert counts.by_status == {"DRAFT": 1} and counts.total == 1
    # Names come from the caller's own workspace directory only.
    items = CampaignService(session).list_campaigns(ctx, limit=50, cursor=None, query=None, status="ALL").items
    assert [i.creator_name for i in items] == [None]
    session.close()


def test_http_endpoints_validate_and_serve(session_factory, world) -> None:
    session, ctx = api(session_factory, world)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_workspace_context] = lambda: ctx
    try:
        client = TestClient(app)
        base = f"/api/v1/workspaces/{world.ws}/campaigns"

        counts = client.get(f"{base}/counts", params={"type": "STANDARD"})
        assert counts.status_code == 200
        assert counts.json()["not_archived"] == 3

        page = client.get(base, params={"sort": "name_asc", "type": "STANDARD", "limit": 2})
        assert page.status_code == 200
        body = page.json()
        assert [i["name"] for i in body["items"]] == ["alpha", "Bravo"]
        assert body["next_cursor"]

        assert client.get(base, params={"sort": "newest"}).status_code == 422
        assert client.get(base, params={"type": "OTHER"}).status_code == 422
        assert client.get(f"{base}/counts", params={"type": "OTHER"}).status_code == 422
    finally:
        app.dependency_overrides.clear()
        session.close()
