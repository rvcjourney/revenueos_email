"""Which migrations does this database actually have? (read-only)

Production is changed by hand, with `psql -f`, so the history table may be absent or
incomplete and several migrations may be prepared but not applied. This module does
not trust any history: it probes the *objects* each migration creates (a column, a
table, a policy, a function and its owner) through the catalogs only, which every
database user may read, so the answer does not depend on row-level security.

Used by `python -m app.core.preflight`. Every statement is a SELECT.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Connection, text

if TYPE_CHECKING:  # preflight imports this module lazily; avoid an import cycle
    from app.core.preflight import Check

Severity = Literal["FAIL", "WARN"]

_TABLE = "SELECT pg_catalog.to_regclass(:name) IS NOT NULL"
_COLUMN = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a "
    "WHERE a.attrelid = pg_catalog.to_regclass(:table) AND a.attname = :column "
    "AND NOT a.attisdropped AND a.attnum > 0)"
)
_POLICY = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_policies "
    "WHERE schemaname = 'public' AND tablename = :table AND policyname = :policy)"
)
_CONSTRAINT = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint "
    "WHERE conrelid = pg_catalog.to_regclass(:table) AND conname = :name)"
)
_INDEX = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_indexes "
    "WHERE schemaname = 'public' AND indexname = :name)"
)
_FUNCTION = "SELECT pg_catalog.to_regprocedure(:signature) IS NOT NULL"
_FUNCTION_SOURCE = (
    "SELECT COALESCE(position(:needle in p.prosrc) > 0, false) "
    "FROM pg_catalog.pg_proc p WHERE p.oid = pg_catalog.to_regprocedure(:signature)"
)


@dataclass(frozen=True)
class Probe:
    label: str
    run: Callable[[Connection], bool]


def table(name: str) -> Probe:
    return Probe(
        f"table {name}",
        lambda c: bool(c.execute(text(_TABLE), {"name": f"public.{name}"}).scalar()),
    )


def column(table_name: str, column_name: str) -> Probe:
    return Probe(
        f"column {table_name}.{column_name}",
        lambda c: bool(
            c.execute(
                text(_COLUMN), {"table": f"public.{table_name}", "column": column_name}
            ).scalar()
        ),
    )


def policy(table_name: str, policy_name: str) -> Probe:
    return Probe(
        f"policy {table_name}.{policy_name}",
        lambda c: bool(
            c.execute(
                text(_POLICY), {"table": table_name, "policy": policy_name}
            ).scalar()
        ),
    )


def constraint(table_name: str, name: str) -> Probe:
    return Probe(
        f"constraint {name}",
        lambda c: bool(
            c.execute(
                text(_CONSTRAINT), {"table": f"public.{table_name}", "name": name}
            ).scalar()
        ),
    )


def index(name: str) -> Probe:
    return Probe(
        f"index {name}",
        lambda c: bool(c.execute(text(_INDEX), {"name": name}).scalar()),
    )


def function(signature: str) -> Probe:
    return Probe(
        f"function {signature}",
        lambda c: bool(
            c.execute(text(_FUNCTION), {"signature": f"public.{signature}"}).scalar()
        ),
    )


def function_privilege(role: str, signature: str) -> Probe:
    return Probe(
        f"{role} can execute {signature}",
        lambda c: bool(
            c.execute(
                text(
                    "SELECT pg_catalog.has_function_privilege("
                    ":role, :signature, 'EXECUTE')"
                ),
                {"role": role, "signature": f"public.{signature}"},
            ).scalar()
        ),
    )


def function_owner(signature: str, owner: str) -> Probe:
    return Probe(
        f"{signature} owned by {owner}",
        lambda c: bool(
            c.execute(
                text(
                    "SELECT COALESCE("
                    "pg_catalog.pg_get_userbyid(p.proowner) = :owner, false) "
                    "FROM pg_catalog.pg_proc p "
                    "WHERE p.oid = pg_catalog.to_regprocedure(:signature)"
                ),
                {"signature": f"public.{signature}", "owner": owner},
            ).scalar()
        ),
    )


def function_mentions(signature: str, needle: str) -> Probe:
    """The function exists and its body contains `needle` (tells a replaced
    function body from the older one without comparing whole bodies)."""
    return Probe(
        f"function {signature} (current version)",
        lambda c: bool(
            c.execute(
                text(_FUNCTION_SOURCE),
                {"signature": f"public.{signature}", "needle": needle},
            ).scalar()
        ),
    )


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    probes: tuple[Probe, ...]
    # What a missing migration costs. FAIL = code that runs today needs it.
    severity: Severity
    why: str
    # False when the probes describe a state a database may be in WITHOUT this migration
    # (a repair migration whose target is already right on a chain-built database): its
    # presence then says nothing about how far the chain was applied.
    proves_progress: bool = True


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        21,
        "lead profile fields",
        (column("leads", "phone"), column("leads", "company_founded_year")),
        "FAIL",
        "lead capture and import select the profile columns",
    ),
    Migration(
        22,
        "frozen variables size cap",
        (
            constraint(
                "campaign_enrollments", "campaign_enrollments_frozen_variables_check"
            ),
        ),
        "WARN",
        "no size cap on a recipient's frozen variables",
    ),
    Migration(
        24,
        "step pre-header",
        (column("sequence_steps", "email_preheader"),),
        "FAIL",
        "saving a sequence step writes email_preheader",
    ),
    Migration(
        25,
        "step attachments",
        (table("campaign_step_attachments"),),
        "FAIL",
        "every send reads the step's attachments",
    ),
    Migration(
        26,
        "campaign type + objective",
        (
            column("campaigns", "campaign_type"),
            column("campaign_sequences", "personalization_config"),
        ),
        "FAIL",
        "campaign creation and the paused-edit guards read campaign_type",
    ),
    Migration(
        27,
        "personalization research + usage",
        (table("personalization_research_cache"), table("personalization_usage_daily")),
        "WARN",
        "personalized campaigns",
    ),
    Migration(
        28,
        "message generations",
        (table("message_generations"),),
        "WARN",
        "personalized campaigns",
    ),
    Migration(
        29,
        "personalization previews + approvals",
        (
            table("personalization_previews"),
            table("campaign_personalization_approvals"),
        ),
        "WARN",
        "personalized campaigns",
    ),
    Migration(
        30,
        "message tracking",
        (table("message_events"),),
        "FAIL",
        "open tracking and reply sync write message events",
    ),
    Migration(
        31,
        "reply sync effect grants",
        (policy("domain_events", "domain_events_worker_sync_select"),),
        "FAIL",
        "reply sync reads its domain events",
    ),
    Migration(
        32,
        "open classification",
        (column("message_events", "qualified_at"),),
        "WARN",
        "open counts are not qualified against automated opens",
    ),
    Migration(
        33,
        "archive / purge / erasure",
        (column("campaigns", "erased_at"), function("app_erase_lead(uuid,uuid)")),
        "WARN",
        "erasure; and the paused-edit guards (0042) read campaigns.erased_at",
    ),
    Migration(
        34,
        "workspace delete",
        (function("app_delete_workspace(uuid,text)"),),
        "WARN",
        "deleting a workspace",
    ),
    Migration(
        35,
        "campaign / mailbox hard delete",
        (function("app_delete_campaign(uuid,uuid)"),),
        "WARN",
        "deleting a campaign or mailbox",
    ),
    Migration(
        36,
        "erasure identity grant",
        (
            function_privilege("app_erasure", "app_current_user_id()"),
            function_privilege("app_erasure", "app_current_workspace_role()"),
        ),
        "WARN",
        "erasure and delete commands (identity helpers)",
    ),
    Migration(
        37,
        "deliverability read grants",
        (policy("safety_holds", "safety_holds_api_analytics_select"),),
        "FAIL",
        "the dashboard deliverability page answers 500 without it",
    ),
    Migration(
        40,
        "campaign pause metadata",
        (column("campaigns", "paused_at"), column("campaigns", "paused_total_seconds")),
        "FAIL",
        "pause and resume write the pause columns (a fallback keeps Pause working, "
        "but without it the pause time is not recorded)",
    ),
    Migration(
        41,
        "frozen-sequence guard ownership",
        (
            function_owner("app_guard_frozen_sequence_steps()", "app_integrity_guard"),
            function_owner(
                "app_guard_frozen_step_attachments()", "app_integrity_guard"
            ),
        ),
        "WARN",
        "0042 and later refuse to apply while these guards are owned by someone else",
        proves_progress=False,
    ),
    Migration(
        42,
        "paused campaign content edits",
        (
            policy("messages", "messages_app_integrity_guard_frontier_read"),
            function_mentions("app_guard_frozen_sequence_steps()", "campaign_kind"),
        ),
        "WARN",
        "editing emails of a paused campaign",
    ),
    Migration(
        43,
        "supplemental audience",
        (
            column("campaign_audiences", "kind"),
            index("campaign_planning_jobs_supplement_identity_idx"),
        ),
        "WARN",
        "adding leads to a paused campaign",
    ),
    Migration(
        44,
        "hyper paused reference edits",
        (function_mentions("app_guard_frozen_sequence_steps()", "HYPER_PERSONALIZED"),),
        "WARN",
        "editing the reference emails of a paused personalized campaign",
    ),
    Migration(
        45,
        "planning job scheduler discovery",
        (
            policy(
                "campaign_planning_jobs",
                "campaign_planning_jobs_scheduler_discovery_select",
            ),
        ),
        "WARN",
        "automatic recovery of stuck planning jobs",
    ),
)

# Functions whose owner matters (SECURITY DEFINER bodies run with their owner's rights).
_GUARDS = (
    "app_guard_frozen_sequence_steps()",
    "app_guard_frozen_step_attachments()",
    "app_guard_sequence()",
    "app_guard_campaign_snapshot()",
    "app_guard_campaign_config()",
)
_ERASURE_FUNCTIONS = (
    "app_erasure_authorize(uuid)",
    "app_erase_lead(uuid,uuid)",
    "app_purge_campaign(uuid,uuid)",
    "app_erase_campaign(uuid,uuid)",
    "app_delete_workspace(uuid,text)",
    "app_delete_campaign(uuid,uuid)",
    "app_delete_mailbox(uuid,uuid)",
)

_OWNER = (
    "SELECT pg_catalog.pg_get_userbyid(p.proowner), p.prosecdef "
    "FROM pg_catalog.pg_proc p WHERE p.oid = pg_catalog.to_regprocedure(:signature)"
)


def _migration_result(connection: Connection, migration: Migration) -> list[str]:
    """Labels of the probes that fail (empty = the migration is in place)."""
    return [p.label for p in migration.probes if not p.run(connection)]


def ownership_problems(connection: Connection) -> list[str]:
    problems: list[str] = []
    for signature in _GUARDS:
        row = connection.execute(
            text(_OWNER), {"signature": f"public.{signature}"}
        ).first()
        if row is None:
            continue
        owner, definer = row
        if owner != "app_integrity_guard" or not definer:
            problems.append(
                f"{signature} is owned by {owner}"
                f"{'' if definer else ' and is not SECURITY DEFINER'}"
                " (expected app_integrity_guard, SECURITY DEFINER)"
            )
    snapshot = connection.execute(
        text(_OWNER), {"signature": "public.app_guard_message_snapshot()"}
    ).first()
    if snapshot is not None and snapshot[1]:
        problems.append(
            "app_guard_message_snapshot() is SECURITY DEFINER (must be INVOKER: its "
            "exemptions test CURRENT_USER)"
        )
    for signature in _ERASURE_FUNCTIONS:
        row = connection.execute(
            text(_OWNER), {"signature": f"public.{signature}"}
        ).first()
        if row is not None and row[0] != "app_erasure":
            problems.append(f"{signature} is owned by {row[0]} (expected app_erasure)")
    return problems


def migration_checks(
    connection: Connection, *, personalization_enabled: bool = False
) -> list[Check]:
    from app.core.preflight import Check

    checks: list[Check] = []
    status: dict[int, list[str]] = {}
    for migration in MIGRATIONS:
        missing = _migration_result(connection, migration)
        status[migration.number] = missing
        label = f"Migration {migration.number:04d}"
        if not missing:
            checks.append(Check(label, "OK", migration.name))
            continue
        severity = migration.severity
        if migration.number in (27, 28, 29) and personalization_enabled:
            severity = "FAIL"
        checks.append(
            Check(
                label,
                severity,
                f"{migration.name}: not applied (missing {', '.join(missing)}); "
                f"needed for: {migration.why}",
                remedy=(
                    "review and apply "
                    f"supabase/migrations/{migration.number:04d}_*.sql "
                    "(in order; never edit an applied migration)"
                ),
            )
        )

    # A later migration present while an earlier one is not means the chain was
    # applied out of order or partly: later bodies can depend on earlier ones.
    evidence = {m.number for m in MIGRATIONS if m.proves_progress}
    applied = [n for n, missing in status.items() if not missing and n in evidence]
    gaps = [
        n
        for n, missing in status.items()
        if missing and any(later > n for later in applied)
    ]
    if gaps:
        checks.append(
            Check(
                "Migration chain",
                "FAIL",
                "migration(s) "
                + ", ".join(f"{n:04d}" for n in gaps)
                + " are missing although a later one is applied",
                remedy="apply the missing migrations first, in numeric order",
            )
        )
    else:
        checks.append(
            Check("Migration chain", "OK", "no gaps among the probed migrations")
        )

    problems = ownership_problems(connection)
    if problems:
        checks.append(
            Check(
                "Function ownership",
                "FAIL",
                "; ".join(problems),
                remedy="apply 0039_erasure_function_ownership.sql; frozen-sequence "
                "guards: apply 0041_guard_function_ownership.sql before 0042; other "
                "guards: ALTER FUNCTION ... OWNER TO app_integrity_guard (see 0003)",
            )
        )
    else:
        checks.append(Check("Function ownership", "OK", "guards and erasure functions"))

    history = connection.execute(
        text(
            "SELECT pg_catalog.to_regclass("
            "'supabase_migrations.schema_migrations') IS NOT NULL"
        )
    ).scalar()
    checks.append(
        Check(
            "Migration history table",
            "INFO",
            "present (not relied on)"
            if history
            else "absent (expected when applied by hand)",
        )
    )
    return checks
