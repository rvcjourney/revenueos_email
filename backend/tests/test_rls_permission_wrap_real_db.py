"""Migration 0047 against a REAL PostgreSQL (disposable, never production).

0047 wraps `app_has_permission(...)` in RLS policies in a sub-select so it runs
once per statement instead of once per row. These tests prove the rewrite
changes only that, is idempotent, can be undone with the documented recovery
script, and leaves the access rules intact for real roles and tenants.
"""

# ruff: noqa: E402, E501 -- imports follow importorskip; long lines are SQL.
from __future__ import annotations

import re

import pytest

pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.context import set_transaction_context
from tests.support.real_pg import MIGRATIONS, apply_range, throwaway_database
from tests.support.real_seed import seed_world

pytestmark = pytest.mark.filterwarnings("ignore")

MIGRATION = MIGRATIONS / "0047_rls_permission_check_once_per_statement.sql"
WRAPPER = re.compile(
    r"\( SELECT (app_has_permission\('[a-z._]+'::text\)) AS app_has_permission\)"
)
BARE = re.compile(r"(?<!SELECT )app_has_permission\(")

DUMP_SQL = """
    SELECT c.relname, p.polname, p.polcmd, p.polroles::regrole[]::text,
           pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
    FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
    ORDER BY 1, 2
"""


def dump(conn) -> list[tuple]:
    return conn.execute(DUMP_SQL).fetchall()


def normalized(rows: list[tuple]) -> list[tuple]:
    """Policies with the 0047 wrapper removed, i.e. their meaning."""
    return [
        (*row[:4], *(WRAPPER.sub(r"\1", e) if e else e for e in row[4:])) for row in rows
    ]


def bare_calls(rows: list[tuple]) -> int:
    return sum(
        len(BARE.findall(WRAPPER.sub("", e) if e else ""))
        for row in rows
        for e in row[4:]
    )


def recovery_script() -> str:
    """The DO block documented under `Recovery:` in the migration header."""
    lines = MIGRATION.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("-- Recovery:"))
    end = next(i for i, line in enumerate(lines) if line.startswith("-- Needs:"))
    body = [line[2:].strip() for line in lines[start + 1 : end]]
    return "\n".join(body)


@pytest.fixture()
def before_uri():
    """A database with everything up to 0046 applied (0047 NOT applied yet)."""
    with throwaway_database(up_to=46) as uri:
        yield uri


def test_rewrite_changes_only_the_call_site_and_keeps_every_policy(before_uri):
    with psycopg.connect(before_uri, autocommit=True) as conn:
        before = dump(conn)
        assert bare_calls(before) > 100  # the problem really exists before
        apply_range(before_uri, 47, 47)
        after = dump(conn)

    assert bare_calls(after) == 0
    assert len(after) == len(before)
    # Same names, commands and roles; expressions equal once the wrapper is removed.
    assert normalized(after) == normalized(before)
    assert after != before  # ...and something did change


def test_second_run_changes_nothing(before_uri):
    with psycopg.connect(before_uri, autocommit=True) as conn:
        apply_range(before_uri, 47, 47)
        once = dump(conn)
        apply_range(before_uri, 47, 47)
        assert dump(conn) == once


def test_documented_recovery_restores_the_original_policies(before_uri):
    with psycopg.connect(before_uri, autocommit=True) as conn:
        original = dump(conn)
        apply_range(before_uri, 47, 47)
        assert dump(conn) != original
        conn.execute(recovery_script())
        assert dump(conn) == original


def test_nothing_is_applied_if_a_policy_cannot_be_rewritten(before_uri):
    """The migration is all-or-nothing: a failure leaves every policy as it was."""
    with psycopg.connect(before_uri, autocommit=True) as conn:
        original = dump(conn)
        sql = MIGRATION.read_text(encoding="utf-8").replace(
            "'(SELECT public.app_has_permission(\\2))'",
            "'(SELECT public.app_has_permission(\\2) AND false)'",
        )
        assert "AND false" in sql
        with pytest.raises(psycopg.Error):
            conn.execute(sql)
        # The script's own BEGIN is left open by the error; rolling back is all
        # an operator would do, and nothing may have been applied.
        conn.execute("ROLLBACK")
        assert dump(conn) == original


def test_access_rules_still_hold_for_real_roles_and_tenants(before_uri):
    with psycopg.connect(before_uri, autocommit=True) as su:
        mine = seed_world(su, running=True, members=1)
        other = seed_world(su, running=True, members=1)
    apply_range(before_uri, 47, 47)

    engine = create_engine("postgresql+psycopg://" + before_uri.split("://", 1)[1], future=True)
    factory = sessionmaker(bind=engine, future=True)

    def visible_messages(user, workspace) -> int:
        with factory() as session:
            session.execute(text("RESET ROLE; SET LOCAL ROLE app_api"))
            set_transaction_context(session, user_id=user, workspace_id=workspace)
            return session.execute(text("SELECT count(*) FROM public.messages")).scalar_one()

    assert visible_messages(mine.owner, mine.ws) >= 1  # a member reads their workspace
    assert visible_messages(mine.member, mine.ws) >= 1  # ...at any role
    assert visible_messages(other.owner, mine.ws) == 0  # not a member of this workspace
    assert visible_messages(mine.owner, other.ws) == 0  # nor of the other one
    assert visible_messages(other.owner, other.ws) >= 1
    engine.dispose()


def test_a_database_built_from_the_full_chain_has_no_per_row_permission_calls():
    with throwaway_database() as uri, psycopg.connect(uri, autocommit=True) as conn:
        assert bare_calls(dump(conn)) == 0
