"""Reference-email edits on a PAUSED personalized (HYPER_PERSONALIZED) campaign --
ADR-0020 / migration 0044, against a REAL PostgreSQL (pgserver).

A personalized campaign writes each lead's email from its step's reference just
before sending. While paused, a manager may improve the wording of a reference, but
only for a step none of whose emails has been written; written emails, waits, step
order and the set of steps stay frozen. The service and the database guard must agree.
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


def test_planned_but_unwritten_messages_do_not_freeze_the_step(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=False)
    update_step(session_factory, su, w, w.step1, email_subject="Better opener")
    assert step_row(su, w.step1)["email_subject"] == "Better opener"

    seq = edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.get_sequence(ctx, w.campaign_id),
    )
    first = next(s for s in seq.steps if s.id == w.step1)
    assert (first.written_count, first.unwritten_count) == (0, 1)
    assert seq.paused_edit_allowed is True


def test_a_step_with_a_written_email_is_frozen_in_the_service_and_the_database(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    message(su, w, 0, w.step1, written=True)

    with pytest.raises(AppError) as info:
        update_step(session_factory, su, w, w.step1, email_subject="Too late")
    assert info.value.status_code == 409 and info.value.code == "step_locked"
    assert step_row(su, w.step1)["email_subject"] != "Too late"
    with pytest.raises(
        psycopg.errors.CheckViolation, match="already been written"
    ):  # the trigger agrees
        su.execute(
            "UPDATE public.sequence_steps SET email_subject='x' WHERE id=%s", [w.step1]
        )

    # The other step is still open, and the counts say why.
    update_step(session_factory, su, w, w.step2, email_subject="Still editable")
    seq = edit(
        session_factory,
        w,
        "MANAGER",
        lambda svc, ctx: svc.get_sequence(ctx, w.campaign_id),
    )
    assert next(s for s in seq.steps if s.id == w.step1).written_count == 1


def test_waits_kinds_and_positions_never_change(session_factory, su, producer) -> None:
    w = hyper_world(su)
    with pytest.raises(AppError) as info:
        update_step(session_factory, su, w, w.wait, wait_duration_minutes=5)
    assert info.value.code == "step_locked"
    for sql in (
        "UPDATE public.sequence_steps SET wait_duration_minutes=5 WHERE id=%s",
        "UPDATE public.sequence_steps SET kind='WAIT', email_subject=NULL, email_body_html=NULL, wait_duration_minutes=5 WHERE id=%s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            su.execute(sql, [w.step2 if "kind" in sql else w.wait])
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute("UPDATE public.sequence_steps SET position=9 WHERE id=%s", [w.step2])


def test_steps_cannot_be_added_removed_duplicated_or_reordered(
    session_factory, su, producer
) -> None:
    w = hyper_world(su)
    ops = {
        "add": lambda svc, ctx: svc.add_step(
            ctx,
            w.campaign_id,
            SequenceStepCreateIn(
                kind="EMAIL",
                position=4,
                email_subject="x",
                email_body_html="<p>x</p>",
                leading_wait_minutes=60,
            ),
        ),
        "delete": lambda svc, ctx: svc.delete_step(ctx, w.campaign_id, w.step2),
        "duplicate": lambda svc, ctx: svc.duplicate_step(ctx, w.campaign_id, w.step2),
        "reorder": lambda svc, ctx: svc.reorder_steps(
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
    assert (
        su.execute(
            "SELECT count(*) FROM public.sequence_steps WHERE sequence_id=%s",
            [w.sequence_id],
        ).fetchone()[0]
        == 3
    )
    # And the database refuses the same things on its own.
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute("DELETE FROM public.sequence_steps WHERE id=%s", [w.step2])
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute(
            "INSERT INTO public.sequence_steps (workspace_id, sequence_id, campaign_id, position, kind, email_subject, email_body_html) "
            "VALUES (%s,%s,%s,4,'EMAIL','x','<p>x</p>')",
            [w.ws, w.sequence_id, w.campaign_id],
        )


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
def test_only_a_paused_campaign_opens_the_reference(
    session_factory, su, producer, status: str
) -> None:
    w = hyper_world(su, status=status)
    with pytest.raises((AppError, psycopg.errors.CheckViolation)):
        update_step(session_factory, su, w, w.step2, email_subject="Sneaky")
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        su.execute(
            "UPDATE public.sequence_steps SET email_subject='x' WHERE id=%s", [w.step2]
        )


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


def test_a_draft_standard_style_edit_is_unaffected_by_the_hyper_branch(su) -> None:
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
