-- Give the two frozen-sequence guards their intended owner (fix for 0003 / 0025).
--
-- Migration:      0041_guard_function_ownership.sql
-- Purpose:        0003 and 0025 end the creation of app_guard_frozen_sequence_steps()
--                 and app_guard_frozen_step_attachments() with `ALTER FUNCTION ... OWNER
--                 TO app_integrity_guard`. On the production database those statements
--                 did not take effect: every public.app_guard_* function is owned by
--                 `postgres`. 0042 (and 0044) replace these two functions and assert,
--                 afterwards, that they are SECURITY DEFINER and owned by
--                 app_integrity_guard; against the production state that assertion
--                 fails and rolls the whole migration back. This migration puts the
--                 two functions in the state 0003/0025 describe, so 0042 can apply.
-- Order:          Apply after 0040 and BEFORE 0042 (0042 and 0044 assert this state). It
--                 depends only on 0003 and 0025 and is idempotent, so it is a no-op on
--                 a database whose owners are already right (every database built from
--                 the committed chain).
-- Creates:        Nothing.
-- Modifies:       The owner of two functions becomes app_integrity_guard (a function
--                 already owned by it is skipped). Their bodies, EXECUTE grants,
--                 triggers and SECURITY DEFINER flag are unchanged.
-- Deletes:        Nothing.
-- Constraints/Indexes/RLS/Triggers: none changed.
-- Security:       app_integrity_guard is NOLOGIN, NOBYPASSRLS, NOINHERIT and has no
--                 runtime member (asserted below). Membership of that role and CREATE
--                 on schema public are granted to the migration user only for this
--                 transaction, exactly as 0003/0025/0039 do, and removed before COMMIT.
--                 The functions stop running as `postgres` (which bypasses RLS) and run
--                 as the restricted guard role instead, as designed. Only the two
--                 functions needed by 0042/0044 are changed; the other app_guard_*
--                 functions keep working as postgres and are left alone on purpose.
-- Existing data:  Unchanged. No data is read or written.
-- Application:    Until now every write to sequence_steps / campaign_step_attachments
--                 ran these guards with postgres's rights. After this migration they
--                 run with app_integrity_guard's rights: SELECT on campaigns,
--                 campaign_sequences and campaign_enrollments (+ row policies) and
--                 UPDATE(status) on campaigns / campaign_sequences, all granted by
--                 0003. The preflight below proves, BEFORE changing anything, that the
--                 database really has those privileges and policies; if it does not,
--                 the migration stops with nothing changed rather than breaking
--                 sequence editing.
-- Risk:           Low to medium. Not verifiable against Supabase from this repository
--                 (the local server is a superuser). Verification after applying:
--                 edit one email step of a DRAFT campaign in the app (must still
--                 save), then repeat the owner query in the Recovery section.
-- Recovery:       ALTER FUNCTION public.app_guard_frozen_sequence_steps() OWNER TO postgres;
--                 ALTER FUNCTION public.app_guard_frozen_step_attachments() OWNER TO postgres;
--                 (restores the previous, working state).
-- Needs:          0003 and 0025 applied.
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
DECLARE
    tbl text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_integrity_guard')
       OR pg_catalog.to_regprocedure('public.app_guard_frozen_sequence_steps()') IS NULL
       OR pg_catalog.to_regprocedure('public.app_guard_frozen_step_attachments()') IS NULL THEN
        RAISE EXCEPTION '0041 requires 0003 and 0025 to already be applied';
    END IF;
    -- The guards lock the campaign and sequence rows (FOR UPDATE) and read the
    -- enrollments: the role they will run as must be able to do exactly that.
    FOREACH tbl IN ARRAY ARRAY['campaigns', 'campaign_sequences', 'campaign_enrollments'] LOOP
        IF NOT pg_catalog.has_table_privilege('app_integrity_guard', 'public.' || tbl, 'SELECT') THEN
            RAISE EXCEPTION '0041 preflight failed: app_integrity_guard lacks SELECT on % (0003 incomplete); nothing was changed', tbl;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_policies
            WHERE schemaname = 'public' AND tablename = tbl
              AND cmd IN ('SELECT', 'ALL') AND 'app_integrity_guard' = ANY (roles)
        ) THEN
            RAISE EXCEPTION '0041 preflight failed: no row policy lets app_integrity_guard read % (0003 incomplete); nothing was changed', tbl;
        END IF;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['campaigns', 'campaign_sequences'] LOOP
        IF NOT pg_catalog.has_any_column_privilege('app_integrity_guard', 'public.' || tbl, 'UPDATE') THEN
            RAISE EXCEPTION '0041 preflight failed: app_integrity_guard lacks UPDATE on % (needed for FOR UPDATE); nothing was changed', tbl;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_policies
            WHERE schemaname = 'public' AND tablename = tbl
              AND cmd IN ('UPDATE', 'ALL') AND 'app_integrity_guard' = ANY (roles)
        ) THEN
            RAISE EXCEPTION '0041 preflight failed: no row policy lets app_integrity_guard lock % rows; nothing was changed', tbl;
        END IF;
    END LOOP;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_sequence_steps()'::pg_catalog.regprocedure AND p.prosecdef
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_step_attachments()'::pg_catalog.regprocedure AND p.prosecdef
    ) THEN
        RAISE EXCEPTION '0041 preflight failed: a frozen-sequence guard is not SECURITY DEFINER; nothing was changed';
    END IF;
END;
$preflight$;

-- Temporary migration-only elevation, revoked again before COMMIT.
GRANT app_integrity_guard TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;
GRANT CREATE ON SCHEMA public TO app_integrity_guard;

DO $own$
DECLARE
    sig text;
BEGIN
    FOREACH sig IN ARRAY ARRAY[
        'public.app_guard_frozen_sequence_steps()',
        'public.app_guard_frozen_step_attachments()'
    ] LOOP
        IF pg_catalog.pg_get_userbyid(
               (SELECT p.proowner FROM pg_catalog.pg_proc p WHERE p.oid = sig::pg_catalog.regprocedure)
           ) <> 'app_integrity_guard' THEN
            EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO app_integrity_guard', sig);
        END IF;
    END LOOP;
END;
$own$;

REVOKE CREATE ON SCHEMA public FROM app_integrity_guard;
REVOKE app_integrity_guard FROM CURRENT_USER;

DO $postflight$
DECLARE
    sig text;
BEGIN
    FOREACH sig IN ARRAY ARRAY[
        'public.app_guard_frozen_sequence_steps()',
        'public.app_guard_frozen_step_attachments()'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc p
             WHERE p.oid = sig::pg_catalog.regprocedure
               AND p.prosecdef
               AND pg_catalog.pg_get_userbyid(p.proowner) = 'app_integrity_guard'
        ) THEN
            RAISE EXCEPTION '0041 postcondition failed: % is not SECURITY DEFINER owned by app_integrity_guard', sig;
        END IF;
    END LOOP;
    IF pg_catalog.has_schema_privilege('app_integrity_guard', 'public', 'CREATE') THEN
        RAISE EXCEPTION '0041 postcondition failed: app_integrity_guard still has CREATE on schema public';
    END IF;
    IF pg_catalog.pg_has_role('app_api', 'app_integrity_guard', 'MEMBER')
       OR pg_catalog.pg_has_role('app_worker_send', 'app_integrity_guard', 'MEMBER')
       OR pg_catalog.pg_has_role('app_worker_general', 'app_integrity_guard', 'MEMBER') THEN
        RAISE EXCEPTION '0041 forbids runtime membership in app_integrity_guard';
    END IF;
END;
$postflight$;

COMMIT;
