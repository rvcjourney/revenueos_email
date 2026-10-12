"""Migrations 0040-0045 applied to a database that already holds campaign data.

Every other real-database test builds on an empty database with all migrations;
this one proves the upgrade path: a campaign, its enrollment and message that
exist at migration 0039 survive 0040-0045 untouched and keep working, the new
columns get safe defaults, and the migrations refuse to run twice.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from tests.support.real_pg import apply_range, throwaway_database
from tests.support.real_seed import seed_world

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def upgraded():
    with throwaway_database(up_to=39) as uri:
        conn = psycopg.connect(uri, autocommit=True)
        running = seed_world(
            conn, running=True, campaign_type="STANDARD", members=3, config={}
        )
        paused = seed_world(
            conn, running=True, campaign_type="STANDARD", members=1, config={}
        )
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "UPDATE public.campaigns SET status='PAUSED' WHERE id=%s",
            [paused.campaign_id],
        )
        conn.execute("SET session_replication_role = origin")
        before = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE status='PLANNED') FROM public.messages"
        ).fetchone()
        apply_range(uri, 40, 45)
        yield uri, conn, running, paused, before
        conn.close()


def test_existing_campaigns_get_safe_pause_defaults(upgraded) -> None:
    _, conn, running, paused, _ = upgraded
    rows = conn.execute(
        "SELECT id, status, paused_at, paused_total_seconds, last_resumed_at, schedule_generation "
        "FROM public.campaigns WHERE id = ANY(%s)",
        [[running.campaign_id, paused.campaign_id]],
    ).fetchall()
    assert len(rows) == 2
    for _id, _status, paused_at, total, resumed_at, generation in rows:
        # No backfill on purpose: it would bump versions under open browser tabs.
        assert (paused_at, total, resumed_at, generation) == (None, 0, None, 1)


def test_existing_audiences_are_activation_audiences_and_history_is_intact(
    upgraded,
) -> None:
    _, conn, running, _, before = upgraded
    assert conn.execute(
        "SELECT DISTINCT kind FROM public.campaign_audiences"
    ).fetchall() == [("ACTIVATION",)]
    assert (
        conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE status='PLANNED') FROM public.messages"
        ).fetchone()
        == before
    )
    assert conn.execute(
        "SELECT state FROM public.campaign_enrollments WHERE campaign_id=%s",
        [running.campaign_id],
    ).fetchone() == ("ACTIVE",)


def test_the_activated_audience_can_still_be_enrolled_and_pause_columns_are_writable(
    upgraded,
) -> None:
    _, conn, running, _, _ = upgraded
    member = conn.execute(
        "SELECT id, lead_id, address_id, frozen_variables FROM public.campaign_audience_members "
        "WHERE audience_id=%s ORDER BY capture_ordinal OFFSET 1 LIMIT 1",
        [running.audience_id],
    ).fetchone()
    address = conn.execute(
        "SELECT canonical_address FROM public.recipient_addresses WHERE id=%s",
        [member[2]],
    ).fetchone()[0]
    conn.execute(
        "SET session_replication_role = replica"
    )  # config is frozen after activation
    conn.execute(
        "INSERT INTO public.campaign_mailboxes (workspace_id, campaign_id, mailbox_id) VALUES (%s,%s,%s)",
        [running.ws, running.campaign_id, running.mailbox_id],
    )
    conn.execute("SET session_replication_role = origin")
    # The relaxed enrollment guard still accepts the activated audience exactly as before.
    conn.execute(
        "INSERT INTO public.campaign_enrollments (workspace_id, campaign_id, audience_id, audience_member_id, "
        "sequence_id, lead_id, address_id, frozen_destination, state, next_step_id, next_sequence_position, "
        "assigned_mailbox_id, frozen_variables) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s,1,%s,%s::jsonb)",
        [
            running.ws,
            running.campaign_id,
            running.audience_id,
            member[0],
            running.sequence_id,
            member[1],
            member[2],
            address,
            running.step1,
            running.mailbox_id,
            __import__("json").dumps(member[3]),
        ],
    )
    conn.execute(
        "UPDATE public.campaigns SET paused_at = now(), paused_total_seconds = 5 WHERE id=%s",
        [running.campaign_id],
    )
    assert conn.execute(
        "SELECT paused_total_seconds FROM public.campaigns WHERE id=%s",
        [running.campaign_id],
    ).fetchone() == (5,)


# 0041 (ownership repair) is idempotent by design and has its own test file.
@pytest.mark.parametrize("number", [40, 42, 43, 44, 45])
def test_each_migration_refuses_to_run_twice(upgraded, number) -> None:
    uri = upgraded[0]
    with pytest.raises(psycopg.errors.RaiseException, match="already applied|exists"):
        apply_range(uri, number, number)


def test_0042_refuses_to_run_on_a_database_that_predates_what_its_guards_read() -> None:
    """The replacement step guards read campaigns.erased_at (0033) on every step
    write; applied on an older database they would break ALL sequence editing."""
    with throwaway_database(up_to=32) as uri:
        with pytest.raises(psycopg.errors.RaiseException, match="0033"):
            apply_range(uri, 42, 42)


def test_0044_needs_0042_and_a_personalized_step_stays_frozen_until_it_is_applied() -> (
    None
):
    with throwaway_database(up_to=40) as uri:
        with pytest.raises(psycopg.errors.RaiseException, match="0042"):
            apply_range(uri, 44, 44)
    with throwaway_database(up_to=43) as uri:
        conn = psycopg.connect(uri, autocommit=True)
        try:
            w = seed_world(
                conn,
                running=True,
                campaign_type="HYPER_PERSONALIZED",
                members=1,
                enroll_first=False,
            )
            conn.execute("SET session_replication_role = replica")
            conn.execute(
                "UPDATE public.campaign_sequences SET status='FROZEN', frozen_at=now(), content_digest=%s WHERE id=%s",
                ["a" * 64, w.sequence_id],
            )
            conn.execute(
                "UPDATE public.campaigns SET status='PAUSED' WHERE id=%s",
                [w.campaign_id],
            )
            conn.execute("SET session_replication_role = origin")
            with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
                conn.execute(
                    "UPDATE public.sequence_steps SET email_subject='x' WHERE id=%s",
                    [w.step2],
                )
            apply_range(uri, 44, 44)
            conn.execute(
                "UPDATE public.sequence_steps SET email_subject='x' WHERE id=%s",
                [w.step2],
            )
            assert conn.execute(
                "SELECT email_subject FROM public.sequence_steps WHERE id=%s", [w.step2]
            ).fetchone() == ("x",)
        finally:
            conn.close()


def test_choosing_an_audience_for_a_draft_works_before_0043_is_applied() -> None:
    """Code ships ahead of migrations more often than anyone plans: the ordinary
    draft flow must not need the supplement column."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.modules.campaigns.repository import CampaignRepository

    with throwaway_database(up_to=42) as uri:
        conn = psycopg.connect(uri, autocommit=True)
        w = seed_world(
            conn,
            running=False,
            campaign_type="STANDARD",
            members=1,
            enroll_first=False,
            config={},
        )
        engine = create_engine(
            "postgresql+psycopg://" + uri.split("://", 1)[1], future=True
        )
        session = sessionmaker(bind=engine)()
        row = CampaignRepository(session).insert_campaign_audience(
            workspace_id=w.ws,
            campaign_id=w.campaign_id,
            selection_manifest={"version": 1, "lists": [], "leads": []},
        )
        session.commit()
        assert row["status"] == "CAPTURING"
        assert not conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='campaign_audiences' AND column_name='kind'"
        ).fetchall()
        session.close()
        engine.dispose()
        conn.close()


def test_pause_and_resume_still_work_before_0040_and_start_recording_after_it(
    monkeypatch,
) -> None:
    """Pause is the emergency stop: shipping the code ahead of migration 0040 must not
    break it. Without the columns it flips the status (resume still bumps the fence);
    once 0040 is applied the next call records the pause time."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.api.deps import WorkspaceContext
    from app.db.context import set_transaction_context
    from app.modules.campaigns import repository as campaign_repository
    from app.modules.campaigns.activation_service import CampaignActivationService
    from app.modules.campaigns.schemas import PauseIn, ResumeIn
    from tests.support.pause_support import FakeProducer

    fake = FakeProducer()
    monkeypatch.setattr(
        "app.services.task_dispatch.get_task_producer", lambda *a, **k: fake
    )
    monkeypatch.setattr(
        "app.modules.campaigns.preflight.unsubscribe_ready", lambda s: True
    )
    campaign_repository.reset_pause_columns_cache()

    with throwaway_database(up_to=39) as uri:
        conn = psycopg.connect(uri, autocommit=True)
        w = seed_world(
            conn,
            running=True,
            campaign_type="STANDARD",
            members=1,
            enroll_first=False,
            config={},
        )
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "INSERT INTO public.campaign_mailboxes (workspace_id, campaign_id, mailbox_id) VALUES (%s,%s,%s)",
            [w.ws, w.campaign_id, w.mailbox_id],
        )
        conn.execute("SET session_replication_role = origin")
        engine = create_engine(
            "postgresql+psycopg://" + uri.split("://", 1)[1], future=True
        )
        factory = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False, future=True
        )

        def command(name: str):
            session = factory()
            session.execute(text("SET LOCAL ROLE app_api"))
            set_transaction_context(session, user_id=w.manager, workspace_id=w.ws)
            ctx = WorkspaceContext(
                workspace_id=w.ws, user_id=w.manager, role_code="MANAGER"
            )
            version = conn.execute(
                "SELECT version FROM public.campaigns WHERE id=%s", [w.campaign_id]
            ).fetchone()[0]
            try:
                service = CampaignActivationService(session)
                out = (
                    service.pause(ctx, w.campaign_id, PauseIn(expected_version=version))
                    if name == "pause"
                    else service.resume(
                        ctx, w.campaign_id, ResumeIn(expected_version=version)
                    )
                )
                session.commit()
                return out
            finally:
                session.close()

        def state():
            return conn.execute(
                "SELECT status, schedule_generation FROM public.campaigns WHERE id=%s",
                [w.campaign_id],
            ).fetchone()

        try:
            assert command("pause").status == "PAUSED" and state() == ("PAUSED", 1)
            assert command("resume").status == "RUNNING" and state() == ("RUNNING", 2)

            # Apply 0040: the negative answer is cached briefly, then noticed.
            apply_range(uri, 40, 40)
            campaign_repository._pause_columns_cache["checked_at"] -= 120
            assert command("pause").paused_at is not None
            assert command("resume").paused_at is None
            assert (
                conn.execute(
                    "SELECT paused_total_seconds, last_resumed_at IS NOT NULL FROM public.campaigns WHERE id=%s",
                    [w.campaign_id],
                ).fetchone()[1]
                is True
            )
        finally:
            campaign_repository.reset_pause_columns_cache()
            engine.dispose()
            conn.close()
