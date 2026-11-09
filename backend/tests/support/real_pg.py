"""A disposable real PostgreSQL for exercising migrations, RLS, grants and worker
SQL for real -- no Docker, never the production-linked Supabase project.

Uses `pgserver` (bundled Postgres binaries). Optional: tests that need it call
`pytest.importorskip("pgserver")`, so CI without it simply skips them.

Quirks handled here (the migrations themselves are never edited):
- Supabase provides `auth.users` and the `authenticated/anon/service_role` roles;
  they are stubbed.
- Some pre-0026 migrations verify their own grants with `has_table_privilege(...,
  'INSERT')`, which this build reports false for column-level grants, so their
  post-check blocks are stripped from the SQL text in memory. 0026+ checks are
  kept and therefore exercised.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / "supabase" / "migrations"
_POST_CHECK = re.compile(
    r"DO \$(?:postcondition|postflight)\$.*?\$(?:postcondition|postflight)\$;",
    re.S,
)
_FIRST_KEPT_CHECK = 26


def _migration_sql(path: pathlib.Path) -> str:
    sql = path.read_text(encoding="utf-8")
    if int(path.name[:4]) < _FIRST_KEPT_CHECK:
        sql = _POST_CHECK.sub("", sql)
    return sql


def database_uri(admin_uri: str, database: str) -> str:
    """`admin_uri` pointed at another database, keeping everything else.

    On Windows pgserver listens on TCP (`postgresql://postgres:@127.0.0.1:PORT/postgres`);
    on Linux it uses a unix socket and the directory is in the query
    (`postgresql://postgres:@/postgres?host=/tmp/...`). Only the path may change:
    cutting the string at its last "/" breaks the socket form.
    """
    return urlunsplit(urlsplit(admin_uri)._replace(path=f"/{database}"))


@contextmanager
def throwaway_database(up_to: int | None = None) -> Iterator[str]:
    """Yield a superuser connection URI to a fresh database with the migrations
    (through `up_to`, default all) applied. The server is stopped and its data
    directory deleted on exit."""
    import pgserver
    import psycopg

    data_dir = pathlib.Path(tempfile.mkdtemp(prefix="outly_pg_"))
    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    try:
        admin_uri = server.get_uri()
        with psycopg.connect(admin_uri, autocommit=True) as admin:
            admin.execute("CREATE DATABASE outly")
        uri = database_uri(admin_uri, "outly")
        with psycopg.connect(uri, autocommit=True) as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS auth")
            conn.execute("CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY)")
            for role in ("authenticated", "anon", "service_role"):
                conn.execute(f"CREATE ROLE {role} NOLOGIN")
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if up_to is not None and int(path.name[:4]) > up_to:
                    break
                try:
                    conn.execute(_migration_sql(path))
                except Exception as exc:
                    raise RuntimeError(f"{path.name}: {exc}") from exc
        yield uri
    finally:
        server.cleanup()
        shutil.rmtree(data_dir, ignore_errors=True)


def apply_range(uri: str, first: int, last: int) -> None:
    """Apply migrations first..last (inclusive) to an existing database."""
    import psycopg

    with psycopg.connect(uri, autocommit=True) as conn:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            number = int(path.name[:4])
            if first <= number <= last:
                conn.execute(_migration_sql(path))


def connect(uri: str, **kwargs: Any) -> Any:
    import psycopg

    return psycopg.connect(uri, **kwargs)
