"""Edits to a PAUSED personalized (HYPER_PERSONALIZED) campaign -- ADR-0020 /
migrations 0044 and 0046, against a REAL PostgreSQL (pgserver).

A personalized campaign writes each lead's email from its step's reference just
before sending. While paused a manager edits it like a standard campaign: the wording
of any step, any wait, and steps after the last one a lead has reached. Emails already
written keep their text. Steps leads have reached, and attachments, stay frozen. The
service and the database guard must agree.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.errors import AppError
from app.modules.campaigns.schemas import (
    SequenceStepCreateIn,
    SequenceStepReorderEntry,
    SequenceStepsReorderIn,
    SequenceStepUpdateIn,
)
from app.modules.campaigns.sequence_service import SequenceService
from tests.support.pause_support import FakeProducer, api, audit_count, enroll
from tests.support.real_pg import throwaway_database
from tests.support.real_seed import World, _insert, seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

BETTER = "<p>Short, specific and warm. {{first_name}}</p>"


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
    return fake


def hyper_world(su, *, status: str = "PAUSED") -> World:
    """E(1) W(2) E(3) of a frozen, activated personalized campaign."""
    w = seed_world(
        su,
        running=True,
        campaign_type="HYPER_PERSONALIZED",
        members=2,
        enroll_first=False,
    )
    su.execute("SET session_replication_role = replica")
    su.execute(
        "UPDATE public.campaign_sequences SET status='FROZEN', frozen_at=now(), content_digest=%s WHERE id=%s",
        ["a" * 64, w.sequence_id],
    )
    su.execute(
        "UPDATE public.campaigns SET status=%s WHERE id=%s", [status, w.campaign_id]
    )
    su.execute("SET session_replication_role = origin")
    return w


def message(su, w: World, i: int, step: uuid.UUID, *, written: bool) -> uuid.UUID:
    """One lead's message for `step`: PLANNED (to be written) or already written."""
    mid = uuid.uuid4()
    extra = (
        dict(
            status="SCHEDULED",
            content_subject="Hi A",
            content_body_html="<p>Hi A</p>",
            content_digest="d" * 64,
            rendered_at=datetime.now(UTC),
            frozen_destination=f"lead{i}@target{i}.test",
            frozen_sender_address="s@acme.test",
            due_at=datetime.now(UTC) + timedelta(days=1),
            schedule_generation=1,
        )
        if written
        else dict(status="PLANNED")
    )
    su.execute("SET session_replication_role = replica")
    _insert(
        su,
        "messages",
        id=mid,
        workspace_id=w.ws,
        purpose="CAMPAIGN",
        campaign_id=w.campaign_id,
        enrollment_id=enroll(su, w, i),
        sequence_id=w.sequence_id,
        step_id=step,
        mailbox_id=w.mailbox_id,
        address_id=w.address_ids[i],
        anchor_at=datetime.now(UTC),
        **extra,
    )
    su.execute("SET session_replication_role = origin")
    return mid


def step_row(su, step_id) -> dict:
    cur = su.execute(
        "SELECT position, kind, email_subject, wait_duration_minutes, version FROM public.sequence_steps WHERE id=%s",
        [step_id],
    )
    return dict(zip([d.name for d in cur.description], cur.fetchone(), strict=True))


def edit(session_factory, w: World, role: str, fn):
    session, ctx = api(session_factory, w, role)
    try:
        out = fn(SequenceService(session), ctx)
        session.commit()
        return out
    finally:
        session.rollback()
        session.close()


def update_step(
    session_factory, su, w: World, step_id, role: str = "MANAGER", **fields
):
    return edit(
        session_factory,
        w,
        role,
        lambda svc, ctx: svc.update_step(
            ctx,
            w.campaign_id,
            step_id,
            SequenceStepUpdateIn(
                expected_version=step_row(su, step_id)["version"], **fields
            ),
        ),
    )


def sequence(session_factory, w: World):
    return edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.get_sequence(ctx, w.campaign_id),
    )


def add_email(session_factory, w: World, position: int):
    return edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.add_step(
            ctx,
            w.campaign_id,
            SequenceStepCreateIn(
                kind="EMAIL",
                position=position,
                email_subject="One more nudge",
                email_body_html="<p>Hi {{first_name}}, a last note.</p>",
                leading_wait_minutes=60,
            ),
        ),
    )


def count_steps(su, w: World) -> int:
    return su.execute(
        "SELECT count(*) FROM public.sequence_steps WHERE sequence_id=%s",
        [w.sequence_id],
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# what may change
# ---------------------------------------------------------------------------


def test_a_step_with_nothing_written_can_have_its_reference_edited(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    before = step_row(su, w.step2)["version"]
    update_step(
        session_factory,
        su,
        w,
        w.step2,
        email_subject="A sharper subject",
        email_body_html=BETTER,
    )

    row = step_row(su, w.step2)
    assert row["email_subject"] == "A sharper subject" and row["version"] == before + 1
    assert audit_count(su, w, "campaign.sequence_edited_while_paused") == 1


def test_a_step_can_be_reworded_even_when_some_of_its_emails_are_written(
    session_factory, su, producer
) -> None:
    """The written email keeps its text; only emails written after the resume use the
    new reference. The counts say how many of each there are."""
    w = hyper_world(su)
    written = message(su, w, 0, w.step1, written=True)
    message(su, w, 1, w.step1, written=False)

    update_step(session_factory, su, w, w.step1, email_subject="Better opener")

    assert step_row(su, w.step1)["email_subject"] == "Better opener"
    assert su.execute(
        "SELECT content_subject FROM public.messages WHERE id=%s", [written]
    ).fetchone() == ("Hi A",)
    first = next(s for s in sequence(session_factory, w).steps if s.id == w.step1)
    assert (first.written_count, first.unwritten_count) == (1, 1)
    assert sequence(session_factory, w).paused_edit_allowed is True
    # The database guard agrees on its own.
    su.execute(
        "UPDATE public.sequence_steps SET email_subject='x' WHERE id=%s", [w.step1]
    )


def test_a_written_email_itself_stays_frozen(session_factory, su, producer) -> None:
    w = hyper_world(su)
    written = message(su, w, 0, w.step1, written=True)
    with pytest.raises(psycopg.errors.CheckViolation, match="snapshot is immutable"):
        su.execute(
            "UPDATE public.messages SET content_subject='changed' WHERE id=%s",
            [written],
        )


def test_waits_can_be_changed_even_after_leads_passed_them(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=False)
    message(su, w, 1, w.step2, written=False)  # leads have reached the last step
    update_step(session_factory, su, w, w.wait, wait_duration_minutes=5)
    assert step_row(su, w.wait)["wait_duration_minutes"] == 5
    su.execute(
        "UPDATE public.sequence_steps SET wait_duration_minutes=7 WHERE id=%s", [w.wait]
    )


def test_the_type_and_place_of_a_step_leads_reached_never_change(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=False)
    with pytest.raises(psycopg.errors.CheckViolation, match="cannot change"):
        su.execute(
            "UPDATE public.sequence_steps SET kind='WAIT', email_subject=NULL, email_body_html=NULL, wait_duration_minutes=5 WHERE id=%s",
            [w.step1],
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="already reached"):
        su.execute("UPDATE public.sequence_steps SET position=9 WHERE id=%s", [w.step1])


def test_steps_after_the_last_reached_one_can_be_added_removed_copied_and_reordered(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=False)  # leads have reached step 1 only

    added = add_email(session_factory, w, position=4)
    assert count_steps(su, w) == 5 and added.position == 5

    edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.delete_step(
            ctx, w.campaign_id, added.id, with_adjacent_wait=True
        ),
    )
    assert count_steps(su, w) == 3

    edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.duplicate_step(ctx, w.campaign_id, w.step2),
    )
    steps = sequence(session_factory, w).steps
    assert [s.kind for s in steps] == ["EMAIL", "WAIT", "EMAIL", "WAIT", "EMAIL"]

    swapped = [
        SequenceStepReorderEntry(step_id=steps[0].id, position=1),
        SequenceStepReorderEntry(step_id=steps[1].id, position=2),
        SequenceStepReorderEntry(step_id=steps[4].id, position=3),
        SequenceStepReorderEntry(step_id=steps[3].id, position=4),
        SequenceStepReorderEntry(step_id=steps[2].id, position=5),
    ]
    edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.reorder_steps(
            ctx, w.campaign_id, SequenceStepsReorderIn(steps=swapped)
        ),
    )
    assert step_row(su, steps[4].id)["position"] == 3
    assert audit_count(su, w, "campaign.sequence_edited_while_paused") == 4


def test_steps_leads_have_reached_stay_protected(session_factory, su, producer) -> None:
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=False)
    ops = {
        "delete": lambda svc, ctx: svc.delete_step(ctx, w.campaign_id, w.step1),
        "add before": lambda svc, ctx: svc.add_step(
            ctx,
            w.campaign_id,
            SequenceStepCreateIn(
                kind="EMAIL",
                position=1,
                email_subject="x",
                email_body_html="<p>x</p>",
                leading_wait_minutes=60,
            ),
        ),
        "move": lambda svc, ctx: svc.reorder_steps(
            ctx,
            w.campaign_id,
            SequenceStepsReorderIn(
                steps=[
                    SequenceStepReorderEntry(step_id=w.step1, position=3),
                    SequenceStepReorderEntry(step_id=w.wait, position=2),
                    SequenceStepReorderEntry(step_id=w.step2, position=1),
                ]
            ),
        ),
    }
    for name, fn in ops.items():
        with pytest.raises(AppError) as info:
            edit(session_factory, w, "MANAGER", fn)
        assert info.value.code == "step_locked", name
    assert count_steps(su, w) == 3
    # And the database refuses the same things on its own.
    with pytest.raises(psycopg.errors.CheckViolation, match="already reached"):
        su.execute("DELETE FROM public.sequence_steps WHERE id=%s", [w.step1])
    with pytest.raises(psycopg.errors.CheckViolation, match="already reached"):
        su.execute(
            "INSERT INTO public.sequence_steps (workspace_id, sequence_id, campaign_id, position, kind, email_subject, email_body_html) "
            "VALUES (%s,%s,%s,1,'EMAIL','x','<p>x</p>')",
            [w.ws, w.sequence_id, w.campaign_id],
        )


def test_the_resulting_sequence_must_stay_valid(session_factory, su, producer) -> None:
    """Removing only the email of an Email/Wait pair would leave the sequence ending on a wait."""
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=False)
    with pytest.raises(AppError) as info:
        edit(
            session_factory,
            w,
            "MANAGER",
            lambda svc, ctx: svc.delete_step(ctx, w.campaign_id, w.step2),
        )
    assert info.value.code == "sequence_invalid" and info.value.status_code == 422
    assert count_steps(su, w) == 3


def test_removing_a_step_takes_its_sample_previews_with_it(
    session_factory, su, producer
) -> None:
    """Previews are made before launch and live for a week; they must not stop a step
    from being removed (migration 0046 cascades them)."""
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=False)
    su.execute("SET session_replication_role = replica")
    _insert(
        su,
        "personalization_previews",
        id=uuid.uuid4(),
        workspace_id=w.ws,
        campaign_id=w.campaign_id,
        batch_id=uuid.uuid4(),
        audience_member_id=w.member_ids[0],
        lead_id=w.lead_ids[0],
        step_id=w.step2,
        config_digest="a" * 64,
        created_by=w.manager,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    su.execute("SET session_replication_role = origin")

    edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.delete_step(
            ctx, w.campaign_id, w.step2, with_adjacent_wait=True
        ),
    )
    assert count_steps(su, w) == 1
    assert su.execute(
        "SELECT count(*) FROM public.personalization_previews WHERE step_id=%s",
        [w.step2],
    ).fetchone() == (0,)


def test_a_step_with_attachments_cannot_be_removed_or_copied(
    session_factory, su, producer
) -> None:
    """The attachments of a personalized campaign stay frozen, and they would go with
    (or be copied with) the step."""
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=False)
    su.execute("SET session_replication_role = replica")
    _insert(
        su,
        "campaign_step_attachments",
        workspace_id=w.ws,
        campaign_id=w.campaign_id,
        sequence_id=w.sequence_id,
        step_id=w.step2,
        disposition="ATTACHMENT",
        content_id="attach12345",
        storage_key="k",
        filename="a.pdf",
        content_type="application/pdf",
        size_bytes=10,
        sha256="c" * 64,
    )
    su.execute("SET session_replication_role = origin")
    for fn in (
        lambda svc, ctx: svc.delete_step(
            ctx, w.campaign_id, w.step2, with_adjacent_wait=True
        ),
        lambda svc, ctx: svc.duplicate_step(ctx, w.campaign_id, w.step2),
    ):
        with pytest.raises(AppError) as info:
            edit(session_factory, w, "MANAGER", fn)
        assert info.value.code == "step_locked" and "attachments" in info.value.message
    assert count_steps(su, w) == 3


# ---------------------------------------------------------------------------
# who and when
# ---------------------------------------------------------------------------


def test_member_cannot_edit_and_row_level_security_agrees(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    with pytest.raises(AppError) as info:
        update_step(
            session_factory, su, w, w.step2, role="MEMBER", email_subject="Nope"
        )
    assert info.value.status_code == 403
    session, _ = api(session_factory, w, "MEMBER")
    try:
        result = session.execute(
            text("UPDATE public.sequence_steps SET email_subject='x' WHERE id=:id"),
            {"id": str(w.step2)},
        )
        assert result.rowcount == 0
    finally:
        session.rollback()
        session.close()


@pytest.mark.parametrize("status", ["RUNNING", "SCHEDULED"])
def test_only_a_paused_campaign_opens_the_sequence(
    session_factory, su, producer, status: str
) -> None:
    w = hyper_world(su, status=status)
    with pytest.raises((AppError, psycopg.errors.CheckViolation)):
        update_step(session_factory, su, w, w.step2, email_subject="Sneaky")
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        su.execute(
            "UPDATE public.sequence_steps SET email_subject='x' WHERE id=%s", [w.step2]
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        su.execute("DELETE FROM public.sequence_steps WHERE id=%s", [w.step2])


def test_inline_images_are_refused_in_a_personalized_reference(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    with pytest.raises(AppError) as info:
        update_step(
            session_factory,
            su,
            w,
            w.step2,
            email_body_html='<p>Hi</p><img src="cid:logo123">',
        )
    assert (
        info.value.code == "reference_inline_image_unsupported"
        and info.value.status_code == 422
    )


def test_attachments_of_a_personalized_campaign_stay_frozen(su) -> None:
    w = hyper_world(su)
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        su.execute(
            "INSERT INTO public.campaign_step_attachments (workspace_id, campaign_id, sequence_id, step_id, disposition, content_id, storage_key, filename, content_type, size_bytes, sha256) "
            "VALUES (%s,%s,%s,%s,'ATTACHMENT','cid1','k','a.pdf','application/pdf',10,%s)",
            [w.ws, w.campaign_id, w.sequence_id, w.step2, "c" * 64],
        )


def test_a_draft_edit_is_unaffected_by_the_paused_rules(su) -> None:
    """The replacement guard still treats a DRAFT sequence as freely editable."""
    w = seed_world(
        su,
        running=False,
        campaign_type="HYPER_PERSONALIZED",
        members=1,
        enroll_first=False,
    )
    su.execute(
        "UPDATE public.sequence_steps SET email_subject='draft edit' WHERE id=%s",
        [w.step2],
    )
    assert step_row(su, w.step2)["email_subject"] == "draft edit"
