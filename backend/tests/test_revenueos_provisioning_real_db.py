"""RevenueOS user provisioning (ADR-0020) against a REAL PostgreSQL.

Same throwaway-pgserver approach as test_workspace_delete_real_db.py: the
service runs as the real `app_api` role with RLS, grants, the bootstrap command
and migration 0038's owner-adds-member command fully enforced. Nothing touches
Supabase; the account lookup is the caller's job and is passed in as an
EnsuredUser.

What is proven here: the acting user ends up the Owner and the person a member
with the requested role; a repeated reference creates nothing; a reference
reused with other content is refused; a revoked member is never re-added by a
replay; each reference gets its own workspace and grants nothing in another
one; and the 0038 command itself refuses anyone but the Owner, the OWNER role,
an unknown user and a previously removed member.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.errors import AppError  # noqa: E402
from app.modules.integrations.revenueos_provisioning import (  # noqa: E402
    PROVISION_OPERATION,
    RevenueOSProvisioningService,
    RevenueOSUserProvisionIn,
    RevenueOSUserProvisionOut,
)
from app.modules.integrations.supabase_auth_admin import EnsuredUser  # noqa: E402
from tests.support.real_pg import throwaway_database  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def uri():
    with throwaway_database() as value:
        yield value


@pytest.fixture()
def su(uri):
    conn = psycopg.connect(uri, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def engine(uri):
    value = create_engine(uri.replace("postgresql://", "postgresql+psycopg://", 1))
    yield value
    value.dispose()


def auth_user(su) -> uuid.UUID:
    # The harness stubs auth.users with an id only; nothing here reads an email
    # from the database (the address is resolved by the Supabase admin API).
    user_id = uuid.uuid4()
    su.execute("INSERT INTO auth.users (id) VALUES (%s)", [user_id])
    return user_id


def unique_email(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}@example.test"


def provision(
    engine, actor: uuid.UUID, user: uuid.UUID, *, created: bool = True, **body: Any
) -> RevenueOSUserProvisionOut:
    """One request: its own transaction as app_api, committed on success."""
    payload = RevenueOSUserProvisionIn.model_validate(body)
    with Session(engine) as session:
        session.execute(text("SET LOCAL ROLE app_api"))
        result = RevenueOSProvisioningService(session).provision(
            actor_id=actor,
            payload=payload,
            user=EnsuredUser(user_id=user, created=created),
        )
        session.commit()
    return result


def scalar(su, sql: str, *params: Any) -> Any:
    return su.execute(sql, list(params)).fetchone()[0]


def roles(su, workspace_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = su.execute(
        "SELECT user_id, role_code FROM public.workspace_memberships "
        "WHERE workspace_id = %s AND status = 'ACTIVE'",
        [workspace_id],
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def revoke(su, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    su.execute(
        "UPDATE public.workspace_memberships SET status = 'REVOKED', "
        "revoked_at = now() WHERE workspace_id = %s AND user_id = %s",
        [workspace_id, user_id],
    )


@pytest.fixture()
def actor(su) -> uuid.UUID:
    return auth_user(su)


def test_new_person_gets_a_workspace_owned_by_the_acting_user(
    engine, su, actor
) -> None:
    email = unique_email("cam")
    person = auth_user(su)

    result = provision(
        engine,
        actor,
        person,
        reference="CLIENT-1",
        email=email,
        workspace_name="Acme Industries",
    )

    assert result.duplicate is False and result.user_created is True
    assert result.user_id == person and result.email == email
    assert result.workspace_name == "Acme Industries" and result.role == "ADMIN"
    assert roles(su, result.workspace_id) == {actor: "OWNER", person: "ADMIN"}
    assert (
        scalar(
            su,
            "SELECT status FROM public.command_receipts "
            "WHERE workspace_id = %s AND operation = %s AND request_key = %s",
            result.workspace_id,
            PROVISION_OPERATION,
            "CLIENT-1",
        )
        == "COMPLETED"
    )
    actions = {
        row[0]
        for row in su.execute(
            "SELECT action FROM public.audit_events WHERE workspace_id = %s",
            [result.workspace_id],
        ).fetchall()
    }
    assert actions == {"workspace.bootstrap", "membership.provisioned"}


def test_workspace_name_defaults_to_the_email_and_role_is_honoured(
    engine, su, actor
) -> None:
    email = unique_email("research")
    person = auth_user(su)

    result = provision(
        engine, actor, person, reference="CLIENT-2", email=email, role="MEMBER"
    )

    assert result.workspace_name == f"{email} Workspace"
    assert roles(su, result.workspace_id) == {actor: "OWNER", person: "MEMBER"}


def test_repeating_a_reference_creates_nothing(engine, su, actor) -> None:
    person = auth_user(su)
    body = {
        "reference": "CLIENT-3",
        "email": unique_email("repeat"),
        "workspace_name": "Repeat Co",
    }
    first = provision(engine, actor, person, **body)
    counts = [
        scalar(su, f"SELECT count(*) FROM public.{table}")
        for table in ("workspaces", "workspace_memberships", "audit_events")
    ]

    again = provision(engine, actor, person, created=False, **body)

    assert again.duplicate is True and again.user_created is False
    assert again.workspace_id == first.workspace_id and again.role == "ADMIN"
    assert counts == [
        scalar(su, f"SELECT count(*) FROM public.{table}")
        for table in ("workspaces", "workspace_memberships", "audit_events")
    ]


def test_a_reference_reused_with_other_content_is_refused(engine, su, actor) -> None:
    person = auth_user(su)
    first = provision(
        engine, actor, person, reference="CLIENT-4", email=unique_email("first")
    )
    other = auth_user(su)

    with pytest.raises(AppError) as excinfo:
        provision(
            engine, actor, other, reference="CLIENT-4", email=unique_email("second")
        )

    assert excinfo.value.status_code == 409
    assert roles(su, first.workspace_id) == {actor: "OWNER", person: "ADMIN"}


def test_a_replay_never_restores_access_that_was_removed(engine, su, actor) -> None:
    person = auth_user(su)
    body = {"reference": "CLIENT-5", "email": unique_email("left")}
    first = provision(engine, actor, person, **body)
    revoke(su, first.workspace_id, person)

    with pytest.raises(AppError) as excinfo:
        provision(engine, actor, person, created=False, **body)

    assert excinfo.value.status_code == 409
    assert roles(su, first.workspace_id) == {actor: "OWNER"}


def test_the_acting_users_own_email_stays_owner_and_adds_no_member(engine, su) -> None:
    actor = auth_user(su)

    result = provision(
        engine,
        actor,
        actor,
        created=False,
        reference="SELF",
        email=unique_email("self"),
    )

    assert result.role == "OWNER"
    assert roles(su, result.workspace_id) == {actor: "OWNER"}


def test_each_reference_is_its_own_workspace_and_grants_nothing_elsewhere(
    engine, su, actor
) -> None:
    person_a, person_b = auth_user(su), auth_user(su)

    a = provision(
        engine, actor, person_a, reference="CLIENT-A", email=unique_email("a")
    )
    b = provision(
        engine, actor, person_b, reference="CLIENT-B", email=unique_email("b")
    )

    assert a.workspace_id != b.workspace_id
    assert roles(su, a.workspace_id) == {actor: "OWNER", person_a: "ADMIN"}
    assert roles(su, b.workspace_id) == {actor: "OWNER", person_b: "ADMIN"}
    # person_a, acting as themselves under RLS, cannot see workspace B at all.
    with Session(engine) as session:
        session.execute(text("SET LOCAL ROLE app_api"))
        session.execute(
            text("SELECT set_config('app.user_id', :u, true)"), {"u": str(person_a)}
        )
        session.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(b.workspace_id)},
        )
        visible = session.execute(
            text("SELECT count(*) FROM workspaces WHERE id = :w"),
            {"w": str(b.workspace_id)},
        ).scalar()
    assert visible == 0


def test_a_different_acting_user_does_not_inherit_references(engine, su, actor) -> None:
    """Receipts and bootstrap keys are per acting user, as ADR-0019 notes for
    campaign intake: another actor's same reference is a separate workspace."""
    person = auth_user(su)
    body = {"reference": "CLIENT-6", "email": unique_email("shared")}
    first = provision(engine, actor, person, **body)
    other_actor = auth_user(su)

    second = provision(engine, other_actor, person, created=False, **body)

    assert second.workspace_id != first.workspace_id and second.duplicate is False
    assert roles(su, second.workspace_id) == {other_actor: "OWNER", person: "ADMIN"}


# ---------------------------------------------------------------------------
# Migration 0038: app_provision_workspace_member
# ---------------------------------------------------------------------------


def add_member_as(engine, caller, workspace_id, target, role: str = "MEMBER") -> Any:
    """Call the command directly as `caller` (app_api, RLS on); rolled back."""
    with Session(engine) as session:
        session.execute(text("SET LOCAL ROLE app_api"))
        session.execute(
            text("SELECT set_config('app.user_id', :u, true)"), {"u": str(caller)}
        )
        session.execute(
            text("SELECT set_config('app.workspace_id', :w, true)"),
            {"w": str(workspace_id)},
        )
        return (
            session.execute(
                text("SELECT * FROM public.app_provision_workspace_member(:t, :r)"),
                {"t": str(target), "r": role},
            )
            .mappings()
            .one()
        )


def sqlstate(excinfo: Any) -> str | None:
    return getattr(excinfo.value.orig, "sqlstate", None)


class TestProvisionMemberCommand:
    @pytest.fixture()
    def world(self, engine, su, actor):
        admin = auth_user(su)
        result = provision(
            engine,
            actor,
            admin,
            reference=f"W-{uuid.uuid4().hex[:6]}",
            email=unique_email("admin"),
        )
        outsider = auth_user(su)
        su.execute("INSERT INTO public.profiles (id) VALUES (%s)", [outsider])
        return actor, admin, outsider, result.workspace_id

    def test_only_the_owner_may_add_members(self, engine, su, world) -> None:
        owner, admin, outsider, ws = world
        # An Admin of the workspace and a stranger are both refused.
        for caller in (admin, outsider):
            with pytest.raises(Exception) as excinfo:  # noqa: B017, PT011
                add_member_as(engine, caller, ws, outsider)
            assert sqlstate(excinfo) == "42501"
        assert roles(su, ws) == {owner: "OWNER", admin: "ADMIN"}

    def test_owner_of_another_workspace_cannot_reach_this_one(
        self, engine, su, world
    ) -> None:
        _, _, outsider, ws = world
        other_owner = auth_user(su)
        provision(
            engine,
            other_owner,
            other_owner,
            created=False,
            reference="OTHER",
            email=unique_email("other"),
        )
        with pytest.raises(Exception) as excinfo:  # noqa: B017, PT011
            add_member_as(engine, other_owner, ws, outsider)
        assert sqlstate(excinfo) == "42501"

    def test_owner_role_can_never_be_granted(self, engine, world) -> None:
        owner, _, outsider, ws = world
        with pytest.raises(Exception) as excinfo:  # noqa: B017, PT011
            add_member_as(engine, owner, ws, outsider, role="OWNER")
        assert sqlstate(excinfo) == "22023"

    def test_unknown_user_is_refused(self, engine, world) -> None:
        owner, _, _, ws = world
        with pytest.raises(Exception) as excinfo:  # noqa: B017, PT011
            add_member_as(engine, owner, ws, uuid.uuid4())
        assert sqlstate(excinfo) == "02000"

    def test_existing_member_is_returned_unchanged(self, engine, world) -> None:
        owner, admin, _, ws = world
        row = add_member_as(engine, owner, ws, admin, role="VIEWER")
        assert row["created"] is False and row["role_code"] == "ADMIN"

    def test_removed_member_is_not_reactivated(self, engine, su, world) -> None:
        owner, admin, _, ws = world
        revoke(su, ws, admin)
        with pytest.raises(Exception) as excinfo:  # noqa: B017, PT011
            add_member_as(engine, owner, ws, admin)
        assert sqlstate(excinfo) == "23505"
        assert roles(su, ws) == {owner: "OWNER"}

    def test_other_roles_cannot_execute_it(self, su) -> None:
        for role in ("anon", "authenticated", "service_role", "app_worker_general"):
            assert (
                scalar(
                    su,
                    "SELECT has_function_privilege(%s, "
                    "'public.app_provision_workspace_member(uuid,text)', 'EXECUTE')",
                    role,
                )
                is False
            )


# ---------------------------------------------------------------------------
# Optional SMTP block: connect, update, and failure isolation
# ---------------------------------------------------------------------------

SMTP_PASSWORD = "smtp-secret-do-not-store-in-clear"


def smtp_block(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "host": "smtp.example.test",
        "security_mode": "STARTTLS",
        "port": 587,
        "username": f"sales-{uuid.uuid4().hex[:8]}@acme.test",
        "password": SMTP_PASSWORD,
        "email_address": f"sales-{uuid.uuid4().hex[:8]}@acme.test",
        "sender_display_name": "Acme Sales",
    }
    block.update(overrides)
    return block


@pytest.fixture()
def smtp_ok(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The SMTP server accepts every login; records what it was asked to check."""
    from app.modules.mailboxes.providers.smtp import SmtpProvider

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        SmtpProvider, "validate_connection", lambda self, cred: seen.append(dict(cred))
    )
    return seen


def mailbox_rows(su, workspace_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return su.execute(
        "SELECT id, original_address, connection_state, current_connection_generation "
        "FROM public.mailboxes WHERE workspace_id = %s ORDER BY created_at",
        [workspace_id],
    ).fetchall()


def test_new_user_with_smtp_gets_a_connected_mailbox(
    engine, su, actor, smtp_ok
) -> None:
    block = smtp_block()

    result = provision(
        engine,
        actor,
        auth_user(su),
        reference="SMTP-1",
        email=unique_email("u"),
        smtp=block,
    )

    assert result.mailbox_status == "connected" and result.mailbox_error is None
    assert mailbox_rows(su, result.workspace_id) == [
        (result.mailbox_id, block["email_address"], "CONNECTED", 1)
    ]
    assert smtp_ok[0]["password"] == SMTP_PASSWORD
    # The password is stored encrypted only: never in the readable config.
    config, ciphertext = su.execute(
        "SELECT protected_config::text, credential_ciphertext "
        "FROM public.mailbox_connections WHERE mailbox_id = %s",
        [result.mailbox_id],
    ).fetchone()
    assert SMTP_PASSWORD not in config
    assert SMTP_PASSWORD.encode() not in bytes(ciphertext)


def test_same_reference_with_new_smtp_details_updates_the_mailbox(
    engine, su, actor, smtp_ok
) -> None:
    person, email = auth_user(su), unique_email("u")
    block = smtp_block()
    first = provision(
        engine, actor, person, reference="SMTP-2", email=email, smtp=block
    )

    changed = {**block, "host": "smtp2.example.test", "password": "rotated-password"}
    again = provision(
        engine,
        actor,
        person,
        created=False,
        reference="SMTP-2",
        email=email,
        smtp=changed,
    )

    assert again.duplicate is True and again.mailbox_status == "updated"
    assert again.mailbox_id == first.mailbox_id
    assert mailbox_rows(su, first.workspace_id) == [
        (first.mailbox_id, block["email_address"], "CONNECTED", 2)
    ]
    assert (
        scalar(
            su,
            "SELECT protected_config->>'host' FROM public.mailbox_connections "
            "WHERE mailbox_id = %s AND generation = 2",
            first.mailbox_id,
        )
        == "smtp2.example.test"
    )
    assert smtp_ok[-1]["password"] == "rotated-password"


def test_same_reference_without_smtp_leaves_the_mailbox_alone(
    engine, su, actor, smtp_ok
) -> None:
    person, email = auth_user(su), unique_email("u")
    first = provision(
        engine, actor, person, reference="SMTP-3", email=email, smtp=smtp_block()
    )
    before = mailbox_rows(su, first.workspace_id)
    checks = len(smtp_ok)

    again = provision(
        engine, actor, person, created=False, reference="SMTP-3", email=email
    )

    assert again.duplicate is True
    assert again.mailbox_status is None and again.mailbox_id is None
    assert mailbox_rows(su, first.workspace_id) == before
    assert len(smtp_ok) == checks


def test_a_refused_mailbox_does_not_undo_the_account(
    engine, su, actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.mailboxes.providers.smtp import SmtpProvider

    def refuse(self: Any, cred: Any) -> None:
        raise AppError("auth_failure", "SMTP authentication failed", status_code=401)

    monkeypatch.setattr(SmtpProvider, "validate_connection", refuse)
    person, email = auth_user(su), unique_email("u")
    block = smtp_block()

    result = provision(
        engine, actor, person, reference="SMTP-4", email=email, smtp=block
    )

    assert result.mailbox_status == "failed" and result.mailbox_id is None
    assert result.mailbox_error == "SMTP authentication failed"
    assert roles(su, result.workspace_id) == {actor: "OWNER", person: "ADMIN"}
    assert mailbox_rows(su, result.workspace_id) == []

    # Corrected details on the same reference then connect it.
    monkeypatch.setattr(SmtpProvider, "validate_connection", lambda self, cred: None)
    retry = provision(
        engine,
        actor,
        person,
        created=False,
        reference="SMTP-4",
        email=email,
        smtp=block,
    )
    assert retry.mailbox_status == "connected"
    assert len(mailbox_rows(su, result.workspace_id)) == 1


def test_one_smtp_login_cannot_back_two_sender_addresses(
    engine, su, actor, smtp_ok
) -> None:
    person, email = auth_user(su), unique_email("u")
    block = smtp_block()
    first = provision(
        engine, actor, person, reference="SMTP-5", email=email, smtp=block
    )

    other_sender = {**block, "email_address": unique_email("other-sender")}
    again = provision(
        engine,
        actor,
        person,
        created=False,
        reference="SMTP-5",
        email=email,
        smtp=other_sender,
    )

    assert again.mailbox_status == "failed"
    assert "already connected" in str(again.mailbox_error)
    assert len(mailbox_rows(su, first.workspace_id)) == 1


def test_an_smtp_account_already_used_by_another_workspace_is_refused(
    engine, su, actor, smtp_ok
) -> None:
    block = smtp_block()
    first = provision(
        engine,
        actor,
        auth_user(su),
        reference="SMTP-6A",
        email=unique_email("a"),
        smtp=block,
    )

    second = provision(
        engine,
        actor,
        auth_user(su),
        reference="SMTP-6B",
        email=unique_email("b"),
        smtp=block,
    )

    assert first.mailbox_status == "connected"
    assert second.mailbox_status == "failed"
    assert "already connected" in str(second.mailbox_error)
    assert mailbox_rows(su, second.workspace_id) == []
