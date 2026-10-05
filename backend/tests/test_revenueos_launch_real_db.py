"""RevenueOS launch (ADR-0022) against a REAL PostgreSQL.

Same throwaway-pgserver approach as test_revenueos_provisioning_real_db.py: the
service runs as the real `app_api` role with RLS and grants enforced. The SMTP
login check and the capture-task dispatch are stubbed; nothing leaves the
process.

What is proven here: one request leaves a workspace, a connected mailbox, a
list holding exactly the request's recipients and a DRAFT campaign with a
CAPTURING audience; an address that is already a lead is reused, not
duplicated; a bad address is reported without losing the others; repeating the
reference creates nothing; changed content is refused; no mailbox means no
campaign but the account stands; and the start phase survives the mid-request
commit and reports "pending" while the audience is still being captured.
"""

# Fixtures are imported from the provisioning module and then named as test
# parameters, which ruff reads as unused imports and redefinitions.
# ruff: noqa: F401, F811
from __future__ import annotations

import uuid
from typing import Any

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.deps import WorkspaceContext  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.modules.campaigns.audience_service import AudienceService  # noqa: E402
from app.modules.integrations.revenueos_launch import (  # noqa: E402
    RevenueOSLaunchIn,
    RevenueOSLaunchOut,
    RevenueOSLaunchService,
)
from app.modules.integrations.supabase_auth_admin import EnsuredUser  # noqa: E402
from tests.test_revenueos_provisioning_real_db import (  # noqa: E402, F401
    actor,
    auth_user,
    engine,
    mailbox_rows,
    roles,
    scalar,
    smtp_block,
    smtp_ok,
    su,
    unique_email,
    uri,
)

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(autouse=True)
def no_broker(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The capture task is recorded instead of being sent to a broker."""
    dispatched: list[Any] = []
    monkeypatch.setattr(
        AudienceService,
        "_dispatch_capture_task",
        lambda self, *args, **kwargs: dispatched.append((args, kwargs)),
    )
    return dispatched


def body(reference: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "reference": reference,
        "user": {"email": unique_email("cam"), "workspace_name": "Acme Industries"},
        "smtp": smtp_block(),
        "recipients": [
            {"email": unique_email("lead1"), "first_name": "Asha", "company": "Acme"},
            {"email": unique_email("lead2"), "first_name": "Ravi"},
        ],
        "campaign": {"name": "Acme intro", "description": "from RevenueOS"},
        "emails": [
            {"subject": "Hello", "body_html": "<p>One</p>"},
            {"subject": "Again", "body_html": "<p>Two</p>", "wait_days_before": 3},
        ],
        "schedule": {
            "timezone": "Asia/Kolkata",
            "weekdays": [1, 2, 3, 4, 5],
            "window_start_local": "09:30:00",
            "window_end_local": "17:30:00",
            "daily_limit": 10,
        },
        "auto_start": False,
    }
    value.update(overrides)
    return value


def launch(
    engine,
    actor_id: uuid.UUID,
    person: uuid.UUID,
    payload: dict[str, Any],
    **kwargs: Any,
) -> RevenueOSLaunchOut:
    with Session(engine) as session:
        session.execute(text("SET LOCAL ROLE app_api"))
        result = RevenueOSLaunchService(session).launch(
            actor_id=actor_id,
            payload=RevenueOSLaunchIn.model_validate(payload),
            user=EnsuredUser(user_id=person, created=True),
            **kwargs,
        )
        session.commit()
    return result


def list_emails(su, list_id: uuid.UUID) -> set[str]:
    rows = su.execute(
        "SELECT l.original_address FROM public.lead_list_memberships m "
        "JOIN public.leads l ON l.id = m.lead_id WHERE m.list_id = %s",
        [list_id],
    ).fetchall()
    return {row[0] for row in rows}


def test_one_request_stores_everything(engine, su, actor, smtp_ok, no_broker) -> None:
    person = auth_user(su)
    payload = body("LAUNCH-1")

    result = launch(engine, actor, person, payload)

    assert result.duplicate is False and result.start_status == "not_requested"
    assert roles(su, result.workspace_id) == {actor: "OWNER", person: "ADMIN"}
    assert result.mailbox_status == "connected"
    assert [row[0] for row in mailbox_rows(su, result.workspace_id)] == [
        result.mailbox_id
    ]
    assert result.recipients_created == 2 and result.recipients_rejected == []
    assert list_emails(su, result.list_id) == {
        r["email"] for r in payload["recipients"]
    }
    assert (
        scalar(
            su,
            "SELECT status FROM public.campaigns WHERE id = %s",
            result.campaign_id,
        )
        == "DRAFT"
    )
    assert result.campaign_status == "DRAFT"
    kinds = [
        row[0]
        for row in su.execute(
            "SELECT kind FROM public.sequence_steps WHERE campaign_id = %s "
            "ORDER BY position",
            [result.campaign_id],
        ).fetchall()
    ]
    assert kinds == ["EMAIL", "WAIT", "EMAIL"]
    assert (
        scalar(
            su,
            "SELECT status FROM public.campaign_audiences WHERE campaign_id = %s",
            result.campaign_id,
        )
        == "CAPTURING"
    )
    assert len(no_broker) == 1


def test_existing_lead_in_the_same_workspace_joins_the_new_list(
    engine, su, actor, smtp_ok
) -> None:
    person = auth_user(su)
    payload = body("LAUNCH-3", recipients=[{"email": unique_email("only")}])
    first = launch(engine, actor, person, payload)
    known = payload["recipients"][0]["email"]
    # A person adds another lead to the workspace by hand.
    manual = unique_email("manual")
    su.execute("SET session_replication_role = replica")
    su.execute(
        "INSERT INTO public.leads (workspace_id, original_address, canonical_address) "
        "VALUES (%s, %s, %s)",
        [first.workspace_id, manual, manual],
    )
    su.execute("SET session_replication_role = origin")
    leads_before = scalar(
        su,
        "SELECT count(*) FROM public.leads WHERE workspace_id = %s",
        first.workspace_id,
    )

    with Session(engine) as session:
        session.execute(text("SET LOCAL ROLE app_api"))
        session.execute(
            text("SELECT set_config('app.user_id', :u, true)"), {"u": str(actor)}
        )
        session.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(first.workspace_id)},
        )
        service = RevenueOSLaunchService(session)
        context = WorkspaceContext(
            workspace_id=first.workspace_id, user_id=actor, role_code="OWNER"
        )
        result = RevenueOSLaunchOut(
            reference="x",
            duplicate=False,
            user_id=person,
            email="x@example.test",
            user_created=False,
            workspace_id=first.workspace_id,
            workspace_name="x",
            role="ADMIN",
            start_status="blocked",
        )
        list_id = service._add_recipients(
            context,
            RevenueOSLaunchIn.model_validate(
                body(
                    "LAUNCH-3-NEXT",
                    recipients=[
                        {"email": known.upper()},
                        {"email": manual},
                        {"email": unique_email("brand-new")},
                        {"email": "not-an-email"},
                    ],
                )
            ),
            result,
        )
        session.commit()

    assert result.recipients_existing == 2 and result.recipients_created == 1
    assert [r.email for r in result.recipients_rejected] == ["not-an-email"]
    assert len(list_emails(su, list_id)) == 3
    assert (
        scalar(
            su,
            "SELECT count(*) FROM public.leads WHERE workspace_id = %s",
            first.workspace_id,
        )
        == leads_before + 1
    )


def test_repeating_a_reference_creates_nothing(engine, su, actor, smtp_ok) -> None:
    person = auth_user(su)
    payload = body("LAUNCH-4")
    first = launch(engine, actor, person, payload)
    counts = [
        scalar(su, f"SELECT count(*) FROM public.{table}")
        for table in ("workspaces", "campaigns", "leads", "lead_lists")
    ]

    again = launch(engine, actor, person, payload)

    assert again.duplicate is True and again.campaign_id == first.campaign_id
    assert again.workspace_id == first.workspace_id
    assert counts == [
        scalar(su, f"SELECT count(*) FROM public.{table}")
        for table in ("workspaces", "campaigns", "leads", "lead_lists")
    ]


def test_a_reference_reused_with_other_recipients_is_refused(
    engine, su, actor, smtp_ok
) -> None:
    person = auth_user(su)
    payload = body("LAUNCH-5")
    launch(engine, actor, person, payload)

    changed = {**payload, "recipients": [{"email": unique_email("someone-else")}]}
    with pytest.raises(AppError) as excinfo:
        launch(engine, actor, person, changed)

    assert excinfo.value.status_code == 409


def test_without_a_mailbox_no_campaign_is_stored_but_the_account_stands(
    engine, su, actor
) -> None:
    person = auth_user(su)
    payload = body("LAUNCH-6")
    del payload["smtp"]

    result = launch(engine, actor, person, payload)

    assert result.start_status == "blocked" and result.campaign_id is None
    assert "no connected mailbox" in str(result.start_error)
    assert roles(su, result.workspace_id) == {actor: "OWNER", person: "ADMIN"}
    assert (
        scalar(
            su,
            "SELECT count(*) FROM public.campaigns WHERE workspace_id = %s",
            result.workspace_id,
        )
        == 0
    )


def test_all_recipients_invalid_rolls_the_request_back(
    engine, su, actor, smtp_ok
) -> None:
    person = auth_user(su)
    payload = body("LAUNCH-7", recipients=[{"email": "nope"}, {"email": "also nope"}])
    workspaces = scalar(su, "SELECT count(*) FROM public.workspaces")

    with pytest.raises(AppError) as excinfo:
        launch(engine, actor, person, payload)

    assert excinfo.value.status_code == 422
    assert scalar(su, "SELECT count(*) FROM public.workspaces") == workspaces


def test_start_phase_reports_pending_while_the_audience_is_captured(
    engine, su, actor, smtp_ok
) -> None:
    person = auth_user(su)
    naps: list[float] = []
    ticks = iter([0.0, 5.0, 10.0, 20.0, 30.0])

    result = launch(
        engine,
        actor,
        person,
        body("LAUNCH-8", auto_start=True),
        sleep=naps.append,
        clock=lambda: next(ticks),
    )

    # The mid-request commit kept everything, and each retry re-entered the
    # app_api scope: reading the audience under RLS worked every time.
    assert result.start_status == "pending" and result.campaign_id is not None
    assert "still being captured" in str(result.start_error)
    assert len(naps) == 2
    assert (
        scalar(
            su, "SELECT status FROM public.campaigns WHERE id = %s", result.campaign_id
        )
        == "DRAFT"
    )
