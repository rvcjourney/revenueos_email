-- Default per-mailbox sending limits, and a way for a person to release a safety hold.
--
-- Migration:      0038_golive_limits_and_hold_release.sql
-- Purpose:        (1) Every mailbox gets an explicit sending limit. Until now no
--                 rate policy rows existed, so a mailbox could be asked to send an
--                 unbounded number of emails. This lets people with the
--                 mailboxes.manage permission (MANAGER and above) create and change
--                 a mailbox's limit (today only workspace.manage, ADMIN and above,
--                 could), and backfills a conservative default for every existing
--                 mailbox. (2) Automatic protection places a safety hold on a
--                 mailbox whose bounce rate is dangerous; nothing let a person
--                 release it. This adds that, for mailboxes.manage.
-- Creates:        RLS policies tenant_rate_policies_api_limits_select (SELECT),
--                 tenant_rate_policies_api_mailbox_insert and
--                 tenant_rate_policies_api_mailbox_update (INSERT/UPDATE, app_api,
--                 MAILBOX-kind rows only, mailboxes.manage),
--                 tenant_rate_policies_connection_insert (INSERT, app_connection,
--                 MAILBOX-kind rows only, mailboxes.manage: the role that connects
--                 a mailbox also gives it its default limit), and
--                 safety_holds_api_release (UPDATE, app_api). Inserts one default
--                 MAILBOX policy per existing mailbox that has none: 50 messages
--                 per rolling 24 hours, at least 60 seconds apart.
-- Modifies:       Re-states (idempotent) the app_api column grants on
--                 tenant_rate_policies from 0005, and adds
--                 GRANT UPDATE (status, resolved_at) ON public.safety_holds TO
--                 app_api. No table, column, constraint or index changes.
-- Deletes:        Nothing (only this migration's own policies are dropped and
--                 recreated on re-run).
-- Security:       Tenant isolation is by workspace_id under FORCE ROW LEVEL
--                 SECURITY. The new policies only match MAILBOX-kind policy rows
--                 and only let a hold go from ACTIVE to RESOLVED; WORKSPACE and
--                 CAMPAIGN policies stay ADMIN-only. A foreign workspace's rows
--                 are never visible or writable.
-- Existing data: Adds rows to tenant_rate_policies only; no existing row changes.
--                 The default (50 per 24 h, 60 s spacing) duplicates the
--                 MAILBOX_DEFAULT_DAILY_CAP / MAILBOX_DEFAULT_MIN_SPACING_SECONDS
--                 settings used for newly connected mailboxes; it is a one-time
--                 backfill and does not follow later changes to those settings.
-- Application:    Needs the matching application release. Deploy order: apply
--                 this first, then deploy the code. Code deployed before it fails
--                 safe (sends defer with reason mailbox_limits_missing; nothing is
--                 sent unlimited). Mailbox hard delete, campaign purge and
--                 workspace delete already remove tenant_rate_policies rows
--                 (0033-0035), so the new rows do not block deletion.
-- Risk:           Low. Additive and idempotent (safe to re-run). The backfill takes
--                 a short lock on tenant_rate_policies.
-- Recovery:       DROP POLICY tenant_rate_policies_api_mailbox_insert ON
--                 public.tenant_rate_policies; (and _update, and
--                 safety_holds_api_release ON public.safety_holds);
--                 REVOKE UPDATE (status, resolved_at) ON public.safety_holds FROM
--                 app_api; the backfilled rows can be removed with
--                 DELETE FROM public.tenant_rate_policies WHERE kind = 'MAILBOX'
--                 AND unit = 'MESSAGE' AND window_seconds = 86400
--                 AND limit_value = 50 AND min_spacing_seconds = 60;
--                 (only if no debit references them).
-- Needs:          0001-0005 applied (policies, mailboxes, safety_holds, roles,
--                 app_has_permission, app_current_workspace_id).
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF pg_catalog.to_regclass('public.tenant_rate_policies') IS NULL
       OR pg_catalog.to_regclass('public.safety_holds') IS NULL
       OR pg_catalog.to_regclass('public.mailboxes') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_api')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_connection')
       OR pg_catalog.to_regprocedure('public.app_has_permission(text)') IS NULL
       OR pg_catalog.to_regprocedure('public.app_current_workspace_id()') IS NULL THEN
        RAISE EXCEPTION '0038 requires 0001-0005 to already be applied';
    END IF;
END;
$preflight$;

-- 1. Mailbox sending limits: MANAGER and above, MAILBOX-kind rows only ---------

-- Same grants as 0005, restated so this works even if they are missing on the
-- managed database (0037 found 0014's grants missing there).
GRANT SELECT ON public.tenant_rate_policies TO app_api;
GRANT INSERT (window_kind, min_spacing_seconds, id, workspace_id, campaign_id,
              mailbox_id, kind, unit, window_seconds, limit_value, timezone,
              cooldown_seconds)
    ON public.tenant_rate_policies TO app_api;
GRANT UPDATE (limit_value, cooldown_seconds, timezone, min_spacing_seconds)
    ON public.tenant_rate_policies TO app_api;

-- 0005 defines tenant_rate_policies_api_select with the same predicate; repeated
-- under its own name so the limits screen does not depend on 0005 alone (if both
-- exist they are OR'd, which is harmless).
DROP POLICY IF EXISTS tenant_rate_policies_api_limits_select ON public.tenant_rate_policies;
CREATE POLICY tenant_rate_policies_api_limits_select ON public.tenant_rate_policies
FOR SELECT TO app_api
USING (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('product.read')
);

DROP POLICY IF EXISTS tenant_rate_policies_api_mailbox_insert ON public.tenant_rate_policies;
CREATE POLICY tenant_rate_policies_api_mailbox_insert ON public.tenant_rate_policies
FOR INSERT TO app_api
WITH CHECK (
    kind = 'MAILBOX'
    AND workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('mailboxes.manage')
);

DROP POLICY IF EXISTS tenant_rate_policies_api_mailbox_update ON public.tenant_rate_policies;
CREATE POLICY tenant_rate_policies_api_mailbox_update ON public.tenant_rate_policies
FOR UPDATE TO app_api
USING (
    kind = 'MAILBOX'
    AND workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('mailboxes.manage')
)
WITH CHECK (
    kind = 'MAILBOX'
    AND workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('mailboxes.manage')
);

-- The role that connects a mailbox (Gmail/Microsoft/SMTP) gives it its default
-- limit in the same transaction. INSERT only, MAILBOX-kind only; it cannot read,
-- change or delete limits.
GRANT INSERT (window_kind, min_spacing_seconds, id, workspace_id, mailbox_id,
              kind, unit, window_seconds, limit_value)
    ON public.tenant_rate_policies TO app_connection;

DROP POLICY IF EXISTS tenant_rate_policies_connection_insert ON public.tenant_rate_policies;
CREATE POLICY tenant_rate_policies_connection_insert ON public.tenant_rate_policies
FOR INSERT TO app_connection
WITH CHECK (
    kind = 'MAILBOX'
    AND workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('mailboxes.manage')
);

-- 2. Releasing a safety hold ----------------------------------------------------

GRANT SELECT ON public.safety_holds TO app_api;
GRANT UPDATE (status, resolved_at) ON public.safety_holds TO app_api;

DROP POLICY IF EXISTS safety_holds_api_release ON public.safety_holds;
CREATE POLICY safety_holds_api_release ON public.safety_holds
FOR UPDATE TO app_api
USING (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('mailboxes.manage')
    AND status = 'ACTIVE'
)
WITH CHECK (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('mailboxes.manage')
    AND status = 'RESOLVED'
);

-- 3. Default limit for every existing mailbox that has none ----------------------
-- Runs as the migration owner (outside RLS). 50 messages per rolling 24 hours, at
-- least 60 seconds apart. Idempotent through the partial unique index
-- tenant_rate_policies_mailbox_scope_key.

INSERT INTO public.tenant_rate_policies
    (workspace_id, mailbox_id, kind, unit, window_seconds, limit_value,
     window_kind, min_spacing_seconds)
SELECT m.workspace_id, m.id, 'MAILBOX', 'MESSAGE', 86400, 50, 'ROLLING', 60
FROM public.mailboxes m
ON CONFLICT DO NOTHING;

DO $postflight$
BEGIN
    IF NOT pg_catalog.has_column_privilege('app_api', 'public.tenant_rate_policies', 'limit_value', 'INSERT')
       OR NOT pg_catalog.has_column_privilege('app_api', 'public.tenant_rate_policies', 'limit_value', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege('app_api', 'public.tenant_rate_policies', 'min_spacing_seconds', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege('app_connection', 'public.tenant_rate_policies', 'limit_value', 'INSERT')
       OR NOT pg_catalog.has_column_privilege('app_api', 'public.safety_holds', 'status', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege('app_api', 'public.safety_holds', 'resolved_at', 'UPDATE') THEN
        RAISE EXCEPTION '0038 postcondition failed: app_api column grants missing';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.pg_policies
        WHERE schemaname = 'public'
          AND ((tablename = 'tenant_rate_policies' AND policyname IN (
                    'tenant_rate_policies_api_limits_select',
                    'tenant_rate_policies_api_mailbox_insert',
                    'tenant_rate_policies_api_mailbox_update',
                    'tenant_rate_policies_connection_insert'))
            OR (tablename = 'safety_holds' AND policyname = 'safety_holds_api_release'))) <> 5 THEN
        RAISE EXCEPTION '0038 postcondition failed: policies missing';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.mailboxes m
        WHERE NOT EXISTS (
            SELECT 1 FROM public.tenant_rate_policies p
            WHERE p.workspace_id = m.workspace_id AND p.mailbox_id = m.id
              AND p.kind = 'MAILBOX' AND p.unit = 'MESSAGE'
        )
    ) THEN
        RAISE EXCEPTION '0038 postcondition failed: a mailbox still has no sending limit';
    END IF;
END;
$postflight$;

COMMIT;
