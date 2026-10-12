"""Migration 0041 against the production-shaped state: guards owned by postgres.

On the production database every public.app_guard_* function is owned by `postgres`
(0003's ALTER OWNER never took effect), so 0042's ownership postcondition rolled it
back. These tests rebuild that state locally, prove 0042 fails there with that exact
message and changes nothing, prove 0041 repairs the two functions it must, and that
0042-0045 then apply.
"""

# ruff: noqa: E402, E501
from __future__ import annotations

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from tests.support.real_pg import apply_range, throwaway_database

pytestmark = pytest.mark.filterwarnings("ignore")

_TWO = ("app_guard_frozen_sequence_steps", "app_guard_frozen_step_attachments")
_OWNER_SQL = (
    "SELECT p.proname, pg_get_userbyid(p.proowner), p.prosecdef FROM pg_proc p "
    "JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname = 'public' AND p.proname LIKE 'app\\_guard\\_%' ORDER BY 1"
)


def _drift_to_postgres(conn) -> None:
    """Production state: the owner of every guard function is postgres."""
    for (signature,) in conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname LIKE 'app\\_guard\\_%'"
    ).fetchall():
        conn.execute(f"ALTER FUNCTION {signature} OWNER TO postgres")


def _owners(conn) -> dict[str, tuple[str, bool]]:
    return {
        name: (owner, definer)
        for name, owner, definer in conn.execute(_OWNER_SQL).fetchall()
    }


@pytest.fixture()
def drifted():
    # Build the correct chain, then drift the owners: this is production's shape.
    with throwaway_database(up_to=45) as uri:
        conn = psycopg.connect(uri, autocommit=True)
        _drift_to_postgres(conn)
        yield uri, conn
        conn.close()


def test_0042_rolls_back_on_postgres_owned_guards_and_0041_fixes_it() -> None:
    with throwaway_database(up_to=40) as uri:
        conn = psycopg.connect(uri, autocommit=True)
        _drift_to_postgres(conn)
        assert _owners(conn)["app_guard_frozen_sequence_steps"][0] == "postgres"

        with pytest.raises(
            Exception,
            match="0042 postcondition failed: public.app_guard_frozen_sequence_steps",
        ):
            apply_range(uri, 42, 42)
        # The failure left nothing behind: the 0042 policy it creates first is absent.
        assert conn.execute(
            "SELECT count(*) FROM pg_policies WHERE policyname = 'messages_app_integrity_guard_frontier_read'"
        ).fetchone() == (0,)

        apply_range(uri, 41, 41)
        owners = _owners(conn)
        for name in _TWO:
            assert owners[name] == ("app_integrity_guard", True)

        apply_range(uri, 42, 45)  # now the whole stack applies
        owners = _owners(conn)
        for name in _TWO:
            assert owners[name] == ("app_integrity_guard", True)
        conn.close()


def test_only_the_two_needed_guards_change_owner(drifted) -> None:
    uri, conn = drifted
    before = _owners(conn)
    apply_range(uri, 41, 41)
    after = _owners(conn)
    for name, state in after.items():
        if name in _TWO:
            assert state == ("app_integrity_guard", True)
        else:
            assert state == before[name], name  # everything else stays as it was


def test_0041_is_idempotent_and_leaves_no_elevated_access(drifted) -> None:
    uri, conn = drifted
    apply_range(uri, 41, 41)
    apply_range(uri, 41, 41)
    assert conn.execute(
        "SELECT has_schema_privilege('app_integrity_guard', 'public', 'CREATE')"
    ).fetchone() == (False,)
    assert conn.execute(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
        "JOIN pg_roles u ON u.oid = m.member WHERE r.rolname = 'app_integrity_guard' AND u.rolname <> 'postgres'"
    ).fetchone() == (0,)


def test_0041_is_a_noop_on_a_correctly_owned_database() -> None:
    with throwaway_database(up_to=45) as uri:
        conn = psycopg.connect(uri, autocommit=True)
        before = _owners(conn)
        apply_range(uri, 41, 41)
        assert _owners(conn) == before
        conn.close()


def test_0041_refuses_and_changes_nothing_if_the_guard_role_lacks_privileges(
    drifted,
) -> None:
    uri, conn = drifted
    conn.execute(
        "REVOKE SELECT ON public.campaign_enrollments FROM app_integrity_guard"
    )
    with pytest.raises(
        Exception,
        match="0041 preflight failed: app_integrity_guard lacks SELECT on campaign_enrollments",
    ):
        apply_range(uri, 41, 41)
    assert _owners(conn)["app_guard_frozen_sequence_steps"][0] == "postgres"
    assert conn.execute(
        "SELECT has_schema_privilege('app_integrity_guard', 'public', 'CREATE')"
    ).fetchone() == (False,)


def test_sequence_editing_still_works_after_the_repair(drifted) -> None:
    """A draft sequence step is written through the (now guard-owned) trigger."""
    uri, conn = drifted
    from tests.support.real_seed import seed_world

    apply_range(uri, 41, 41)
    world = seed_world(
        conn, running=False, campaign_type="STANDARD", members=1, config={}
    )
    sequence_id = conn.execute(
        "SELECT id FROM public.campaign_sequences WHERE campaign_id=%s",
        [world.campaign_id],
    ).fetchone()[0]
    conn.execute(
        "UPDATE public.sequence_steps SET email_subject = 'edited' WHERE sequence_id = %s AND kind = 'EMAIL'",
        [sequence_id],
    )
    assert (
        conn.execute(
            "SELECT count(*) FILTER (WHERE email_subject = 'edited') FROM public.sequence_steps "
            "WHERE sequence_id = %s AND kind = 'EMAIL'",
            [sequence_id],
        ).fetchone()[0]
        >= 1
    )


# --------------------------------------------------------------------------------------
# The migration user on a managed database is NOT a superuser: it owns the schema objects
# but cannot act as app_integrity_guard unless a migration grants that to it. A superuser
# (the local default) hides every "must be owner of function" failure, so these tests run
# the migrations as such a role.
# --------------------------------------------------------------------------------------


def _become_a_managed_migration_user(conn) -> None:
    conn.execute("CREATE ROLE mig NOSUPERUSER NOCREATEDB NOLOGIN")
    # What production showed: a member of the guard role, with admin rights, but not an
    # inheriting one (pg_has_role(.., 'USAGE') = false).
    conn.execute(
        "GRANT app_integrity_guard TO mig WITH ADMIN TRUE, INHERIT FALSE, SET FALSE"
    )
    conn.execute("GRANT app_worker_general TO mig WITH INHERIT FALSE, SET FALSE")
    for (statement,) in conn.execute(
        "SELECT format('ALTER TABLE %s OWNER TO mig', c.oid::regclass) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "AND c.relkind IN ('r', 'p') AND pg_get_userbyid(c.relowner) = 'postgres'"
    ).fetchall():
        conn.execute(statement)
    for (statement,) in conn.execute(
        "SELECT format('ALTER FUNCTION %s OWNER TO mig', p.oid::regprocedure) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
        "AND p.prokind = 'f' AND pg_get_userbyid(p.proowner) = 'postgres'"
    ).fetchall():
        conn.execute(statement)
    conn.execute("ALTER SCHEMA public OWNER TO mig")


def _run_as_mig(uri: str, sql: str) -> None:
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute("SET ROLE mig")
        conn.execute(sql)


def _migration_text(number: int) -> str:
    from tests.support.real_pg import MIGRATIONS, _migration_sql

    (path,) = MIGRATIONS.glob(f"{number:04d}_*.sql")
    return _migration_sql(path)


def test_0042_without_the_temporary_membership_fails_for_a_non_superuser() -> None:
    """The failure reported on production: 'must be owner of function ...'."""
    with throwaway_database(up_to=41) as uri:
        with psycopg.connect(uri, autocommit=True) as conn:
            _become_a_managed_migration_user(conn)
        without = _migration_text(42).replace(
            "GRANT app_integrity_guard TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;", ""
        )
        assert without != _migration_text(42)
        with pytest.raises(
            Exception, match="must be owner of function app_guard_frozen_sequence_steps"
        ):
            _run_as_mig(uri, without)


def test_0042_to_0045_apply_for_a_non_superuser_and_leave_no_elevation() -> None:
    with throwaway_database(up_to=41) as uri:
        with psycopg.connect(uri, autocommit=True) as conn:
            _become_a_managed_migration_user(conn)
            for number in (42, 43, 44, 45):
                _run_as_mig(uri, _migration_text(number))
            owners = _owners(conn)
            for name in _TWO:
                assert owners[name] == ("app_integrity_guard", True)
            # The replaced bodies are the new ones (0044 adds the personalized branch).
            assert conn.execute(
                "SELECT position('HYPER_PERSONALIZED' in prosrc) > 0 FROM pg_proc "
                "WHERE proname = 'app_guard_frozen_sequence_steps'"
            ).fetchone() == (True,)
            # The temporary membership is gone again.
            assert conn.execute(
                "SELECT pg_has_role('mig', 'app_integrity_guard', 'USAGE')"
            ).fetchone() == (False,)
