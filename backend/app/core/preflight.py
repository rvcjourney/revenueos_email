"""Go-live preflight: is this deployment ready to send real campaign email?

    python -m app.core.preflight

Read-only. It checks the environment variables and, if it can reach the database,
the state the send path depends on, then prints one line per check and exits 1 if
any check FAILED (so it can gate a deploy). It changes nothing: every database
statement is a SELECT inside a READ ONLY transaction that is always rolled back.
It never sends email and never touches a mailbox provider.

Run it with the same environment the backend runs with. The row checks need to see
every workspace, so the database user must be one that bypasses row-level security
(for example the owner/migration role). With any other user they are reported as
unverified rather than guessed at.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, text

from app.core.config import Settings
from app.modules.rate_limit.policy_service import MIN_CAP_WINDOW_SECONDS
from app.modules.unsubscribe.compliance import unsubscribe_ready

Status = Literal["OK", "FAIL", "WARN", "INFO"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    # What to do about a FAIL/WARN, when there is one specific thing to do.
    remedy: str = ""


def _ok(name: str, detail: str) -> Check:
    return Check(name, "OK", detail)


def _fail(name: str, detail: str) -> Check:
    return Check(name, "FAIL", detail)


def _warn(name: str, detail: str) -> Check:
    return Check(name, "WARN", detail)


def _info(name: str, detail: str) -> Check:
    return Check(name, "INFO", detail)


def settings_checks(settings: Settings) -> list[Check]:
    """Checks that need nothing but the environment."""
    checks: list[Check] = []

    if settings.app_env == "production":
        checks.append(_ok("APP_ENV", "production"))
    else:
        checks.append(_warn("APP_ENV", f"{settings.app_env!r}, not 'production'"))

    if settings.sending_worker_enabled:
        checks.append(_ok("SENDING_WORKER_ENABLED", "true"))
    else:
        checks.append(
            _fail(
                "SENDING_WORKER_ENABLED",
                "false: the send worker will not send any email",
            )
        )

    if settings.scheduler_enabled:
        checks.append(_ok("SCHEDULER_ENABLED", "true"))
    else:
        checks.append(
            _fail("SCHEDULER_ENABLED", "false: due messages will never be queued")
        )

    if unsubscribe_ready(settings):
        checks.append(_ok("Unsubscribe link", "UNSUBSCRIBE_BASE_URL and key are set"))
    else:
        checks.append(
            _fail(
                "Unsubscribe link",
                "UNSUBSCRIBE_BASE_URL and UNSUBSCRIBE_SIGNING_KEY must both be set "
                "(campaign email is never sent without a working opt-out)",
            )
        )

    if settings.mailbox_encryption_key.strip():
        checks.append(_ok("MAILBOX_ENCRYPTION_KEY", "set"))
    else:
        checks.append(
            _fail(
                "MAILBOX_ENCRYPTION_KEY",
                "empty: mailbox credentials would be encrypted with a publicly "
                "known development key. It must be the same key existing "
                "mailboxes were connected with",
            )
        )

    placeholders = settings.placeholder_operator_emails
    if placeholders:
        checks.append(
            _fail(
                "PLATFORM_OPERATOR_EMAILS",
                f"still has placeholder address(es): {', '.join(placeholders)}",
            )
        )
    else:
        checks.append(_ok("PLATFORM_OPERATOR_EMAILS", "customised"))

    if settings.sequence_progression_enabled:
        checks.append(_ok("SEQUENCE_PROGRESSION_ENABLED", "true"))
    else:
        checks.append(
            _warn(
                "SEQUENCE_PROGRESSION_ENABLED",
                "false: only the first email of each sequence is sent; follow-ups "
                "are not planned until this is true",
            )
        )

    checks.append(
        _info(
            "Default mailbox limit",
            f"{settings.mailbox_default_daily_cap} per day, "
            f"{settings.mailbox_default_min_spacing_seconds} s apart",
        )
    )
    return checks


# (role, table, privilege, why). `has_any_column_privilege` is used because most
# of these are column-level grants.
_REQUIRED_PRIVILEGES: Sequence[tuple[str, str, str, str]] = (
    ("app_api", "public.safety_holds", "SELECT", "dashboard deliverability (0037)"),
    ("app_api", "public.safety_holds", "UPDATE", "releasing a hold (0038)"),
    ("app_api", "public.tenant_rate_policies", "INSERT", "setting mailbox limits"),
    ("app_api", "public.tenant_rate_policies", "UPDATE", "setting mailbox limits"),
    (
        "app_connection",
        "public.tenant_rate_policies",
        "INSERT",
        "default limit on connect (0038)",
    ),
    ("app_worker_send", "public.messages", "UPDATE", "sending and rescheduling"),
    ("app_worker_sync", "public.safety_holds", "INSERT", "automatic mailbox hold"),
    ("app_worker_general", "public.message_events", "SELECT", "bounce counts"),
    (
        "app_worker_general",
        "public.tenant_rate_policies",
        "SELECT",
        "spreading a campaign",
    ),
    ("app_worker_general", "public.suppressions", "INSERT", "unsubscribe"),
)

_EXPECTED_POLICIES = (
    ("tenant_rate_policies", "tenant_rate_policies_api_mailbox_insert"),
    ("tenant_rate_policies", "tenant_rate_policies_api_mailbox_update"),
    ("tenant_rate_policies", "tenant_rate_policies_connection_insert"),
    ("safety_holds", "safety_holds_api_release"),
)


def database_checks(
    connection: Connection, settings: Settings | None = None
) -> list[Check]:
    """Checks against the database. Only SELECTs; the caller rolls back."""
    from app.core.migration_state import migration_checks

    checks: list[Check] = []
    connection.execute(text("SET TRANSACTION READ ONLY"))

    # Which migrations exist here at all? Catalog-only, so it works for any user
    # and runs before the row-level-security gate below.
    checks.extend(
        migration_checks(
            connection,
            personalization_enabled=bool(settings and settings.personalization_enabled),
        )
    )

    # Privileges and policies do not depend on row-level security.
    missing = [
        f"{role} {privilege} on {table} ({why})"
        for role, table, privilege, why in _REQUIRED_PRIVILEGES
        if not connection.execute(
            text(
                "SELECT pg_catalog.has_any_column_privilege(:role, :table, :privilege) "
                "OR pg_catalog.has_table_privilege(:role, :table, :privilege)"
            ),
            {"role": role, "table": table, "privilege": privilege},
        ).scalar()
    ]
    if missing:
        checks.append(
            _fail("Database grants", "missing: " + "; ".join(missing))
        )
    else:
        checks.append(_ok("Database grants", f"{len(_REQUIRED_PRIVILEGES)} checked"))

    present = {
        (row[0], row[1])
        for row in connection.execute(
            text(
                "SELECT tablename, policyname FROM pg_catalog.pg_policies "
                "WHERE schemaname = 'public'"
            )
        )
    }
    absent = [f"{t}.{p}" for t, p in _EXPECTED_POLICIES if (t, p) not in present]
    if absent:
        checks.append(
            _fail(
                "Migration 0038",
                "not applied (missing policies: " + ", ".join(absent) + ")",
            )
        )
    else:
        checks.append(_ok("Migration 0038", "policies present"))

    sees_everything = connection.execute(
        text(
            "SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE rolname = current_user"
        )
    ).scalar()
    if not sees_everything:
        checks.append(
            _fail(
                "Row checks",
                "unverified: this database user is subject to row-level security "
                "and cannot see every workspace. Run with the owner/migration role",
            )
        )
        return checks

    status = connection.execute(
        text("SELECT status FROM public.rate_control LIMIT 1")
    ).scalar()
    if status == "READY":
        checks.append(_ok("Rate control", "READY"))
    else:
        checks.append(
            _fail(
                "Rate control",
                f"{status or 'not initialised'}: the rate controller must be "
                "running and READY, or every send is deferred",
            )
        )

    connected = connection.execute(
        text(
            "SELECT count(*) FROM public.mailboxes WHERE connection_state = 'CONNECTED'"
        )
    ).scalar() or 0
    if connected:
        checks.append(_ok("Mailboxes", f"{connected} connected"))
    else:
        checks.append(_warn("Mailboxes", "none connected yet"))

    unlimited = connection.execute(
        text(
            """
            SELECT count(*) FROM public.mailboxes m
            WHERE m.connection_state <> 'DISCONNECTED'
              AND NOT EXISTS (
                  SELECT 1 FROM public.tenant_rate_policies p
                  WHERE p.workspace_id = m.workspace_id AND p.mailbox_id = m.id
                    AND p.kind = 'MAILBOX' AND p.window_seconds >= :min_window
              )
            """
        ),
        {"min_window": MIN_CAP_WINDOW_SECONDS},
    ).scalar() or 0
    if unlimited:
        checks.append(
            _fail(
                "Mailbox limits",
                f"{unlimited} mailbox(es) have no daily sending limit and will not "
                "send. Apply migration 0038 or set a limit on each mailbox page",
            )
        )
    else:
        checks.append(_ok("Mailbox limits", "every connected mailbox has one"))

    held = connection.execute(
        text("SELECT count(*) FROM public.safety_holds WHERE status = 'ACTIVE'")
    ).scalar() or 0
    if held:
        checks.append(
            _warn("Safety holds", f"{held} active: those mailboxes are not sending")
        )
    else:
        checks.append(_ok("Safety holds", "none active"))
    return checks


def redis_check(settings: Settings) -> Check:
    import redis

    try:
        redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5).ping()
    except Exception as exc:  # any failure to reach it is the finding
        return _fail("Redis", f"unreachable: {type(exc).__name__}")
    return _ok("Redis", "reachable")


def run(settings: Settings) -> list[Check]:
    from sqlalchemy import create_engine

    checks = settings_checks(settings)
    checks.append(redis_check(settings))

    if not settings.database_url or settings.database_url.startswith("sqlite"):
        checks.append(_fail("Database", "DATABASE_URL is not a PostgreSQL URL"))
        return checks

    engine = create_engine(settings.database_url, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as connection:
            try:
                checks.extend(database_checks(connection, settings))
            finally:
                connection.rollback()
    except Exception as exc:
        checks.append(_fail("Database", f"unreachable: {type(exc).__name__}"))
    finally:
        engine.dispose()
    return checks


def render(checks: Iterable[Check]) -> str:
    checks = list(checks)
    width = max((len(c.name) for c in checks), default=0)
    return "\n".join(
        f"[{c.status:<4}] {c.name:<{width}}  {c.detail}"
        + (f"  -> {c.remedy}" if c.remedy and c.status != "OK" else "")
        for c in checks
    )


def main() -> int:
    checks = run(Settings.current())
    print(render(checks))
    failed = sum(1 for c in checks if c.status == "FAIL")
    warned = sum(1 for c in checks if c.status == "WARN")
    print(f"\n{failed} failed, {warned} warning(s).")
    if failed:
        print("NOT ready to send real campaigns.")
        return 1
    print("Ready. Still do the manual rehearsal in docs/operations/DEPLOYMENT.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
