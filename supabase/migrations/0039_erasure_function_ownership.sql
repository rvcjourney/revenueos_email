-- Give the erase / purge / delete commands their intended owner (fix for 0033-0035).
--
-- Migration:      0039_erasure_function_ownership.sql
-- Purpose:        0033, 0034 and 0035 each end with `ALTER FUNCTION ... OWNER TO
--                 app_erasure` for their commands. On the production database every
--                 public.app_* function is owned by `postgres` instead, so those
--                 statements did not take effect. The commands are SECURITY DEFINER,
--                 so CURRENT_USER inside them is their owner, and the guards on
--                 recipient_addresses and messages only allow an erasure when
--                 CURRENT_USER = 'app_erasure' (0033). With the wrong owner the guard
--                 raises a check violation, which app_erase_lead reports as
--                 "A list this person belongs to is being captured for a campaign",
--                 so deleting any lead fails.
-- Creates:        Nothing.
-- Modifies:       The owner of 14 functions (listed in the DO block) becomes
--                 app_erasure. Functions that do not exist are skipped.
-- Deletes:        Nothing.
-- Constraints/Indexes/RLS/Triggers: none changed.
-- Security:       app_erasure is NOLOGIN and NOBYPASSRLS and has no runtime member
--                 (asserted below). CREATE on schema public and membership of
--                 app_erasure are granted to the migration user only for this
--                 transaction, exactly as 0033/0034/0035 do, and removed before
--                 COMMIT. EXECUTE grants on the commands are unchanged: ALTER OWNER
--                 keeps them.
-- Existing data:  Unchanged. No data is read or written.
-- Application:    Lead erase starts working. The other commands (purge campaign /
--                 template / list / import / mailbox, erase campaign, delete
--                 campaign / mailbox / workspace) will from now on run with
--                 app_erasure's privileges, as designed, instead of postgres's.
--                 Any table privilege 0033-0035 meant to give app_erasure but that
--                 was skipped would then show up as "permission denied" (SQLSTATE
--                 42501) rather than working by accident; verify each command after
--                 applying (see Verification).
-- Risk:           Low to medium. Idempotent. A rolled-back replay of app_erase_lead
--                 for every lead passed as app_erasure before this migration was
--                 written; the other commands have not been replayed.
-- Verification:   After applying, repeat the rolled-back replay of
--                 public.app_erase_lead as app_api for a workspace owner and expect
--                 would_fail=0. Then try one lead delete in the app.
-- Recovery:       ALTER FUNCTION <name> OWNER TO postgres for the functions below
--                 (this restores the previous, non-working lead erase).
-- Needs:          0033 applied (0034 and 0035 optional).
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_erasure')
       OR pg_catalog.to_regprocedure('public.app_erase_lead(uuid,uuid)') IS NULL THEN
        RAISE EXCEPTION '0039 requires 0033 to already be applied';
    END IF;
END;
$preflight$;

-- Temporary migration-only elevation, revoked again before COMMIT.
GRANT app_erasure TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;
GRANT CREATE ON SCHEMA public TO app_erasure;

DO $own$
DECLARE
    sig text;
BEGIN
    FOREACH sig IN ARRAY ARRAY[
        -- 0033
        'public.app_erasure_authorize(uuid)',
        'public.app_erasure_audit(uuid,uuid,text,text,uuid,jsonb,jsonb)',
        'public.app_erase_recipient_records(uuid,uuid,uuid)',
        'public.app_purge_campaign(uuid,uuid)',
        'public.app_erase_campaign(uuid,uuid)',
        'public.app_erase_lead(uuid,uuid)',
        'public.app_purge_template(uuid,uuid)',
        'public.app_purge_lead_list(uuid,uuid)',
        'public.app_purge_import(uuid,uuid)',
        'public.app_purge_mailbox(uuid,uuid)',
        -- 0034
        'public.app_delete_workspace(uuid,text)',
        -- 0035
        'public.app_delete_send_history(uuid,uuid[],uuid[])',
        'public.app_delete_campaign(uuid,uuid)',
        'public.app_delete_mailbox(uuid,uuid)'
    ] LOOP
        IF pg_catalog.to_regprocedure(sig) IS NOT NULL THEN
            EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO app_erasure', sig);
        END IF;
    END LOOP;
END;
$own$;

REVOKE CREATE ON SCHEMA public FROM app_erasure;
REVOKE app_erasure FROM CURRENT_USER;

DO $postflight$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS p
        WHERE p.oid = ANY (ARRAY[
            pg_catalog.to_regprocedure('public.app_erasure_authorize(uuid)'),
            pg_catalog.to_regprocedure('public.app_erasure_audit(uuid,uuid,text,text,uuid,jsonb,jsonb)'),
            pg_catalog.to_regprocedure('public.app_erase_recipient_records(uuid,uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_purge_campaign(uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_erase_campaign(uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_erase_lead(uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_purge_template(uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_purge_lead_list(uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_purge_import(uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_purge_mailbox(uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_delete_workspace(uuid,text)'),
            pg_catalog.to_regprocedure('public.app_delete_send_history(uuid,uuid[],uuid[])'),
            pg_catalog.to_regprocedure('public.app_delete_campaign(uuid,uuid)'),
            pg_catalog.to_regprocedure('public.app_delete_mailbox(uuid,uuid)')
        ])
          AND p.proowner <> (SELECT r.oid FROM pg_catalog.pg_roles AS r WHERE r.rolname = 'app_erasure')
    ) THEN
        RAISE EXCEPTION '0039 postcondition failed: an erasure command is not owned by app_erasure';
    END IF;
    IF pg_catalog.has_schema_privilege('app_erasure', 'public', 'CREATE') THEN
        RAISE EXCEPTION '0039 postcondition failed: app_erasure still has CREATE on schema public';
    END IF;
    IF NOT pg_catalog.has_function_privilege('app_erasure', 'public.app_current_user_id()', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege('app_erasure', 'public.app_current_workspace_id()', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege('app_erasure', 'public.app_current_workspace_role()', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege('app_erasure', 'public.app_has_permission(text)', 'EXECUTE') THEN
        RAISE EXCEPTION '0039 postcondition failed: app_erasure cannot execute the identity helpers (see 0036)';
    END IF;
    IF NOT pg_catalog.has_function_privilege('app_api', 'public.app_erase_lead(uuid,uuid)', 'EXECUTE') THEN
        RAISE EXCEPTION '0039 postcondition failed: app_api lost EXECUTE on app_erase_lead';
    END IF;
    IF pg_catalog.pg_has_role('app_api', 'app_erasure', 'MEMBER')
       OR pg_catalog.pg_has_role('app_worker_send', 'app_erasure', 'MEMBER')
       OR pg_catalog.pg_has_role('app_worker_general', 'app_erasure', 'MEMBER') THEN
        RAISE EXCEPTION '0039 forbids runtime membership in app_erasure';
    END IF;
END;
$postflight$;

COMMIT;
