-- Owner adds an existing user to their workspace (RevenueOS provisioning, ADR-0020).
--
-- Migration:      0038_provision_workspace_member.sql
-- Purpose:        RevenueOS registers a person and gives them a workspace in one
--                 request. The acting user creates the workspace with the existing
--                 app_bootstrap_workspace() and becomes its Owner; the person then
--                 has to become a member. The invitation commands of 0018 cannot
--                 do that from one server-side transaction (they match the invited
--                 address against auth.users and need the person to accept), and
--                 no runtime role may INSERT into workspace_memberships directly.
--                 This adds exactly one narrowly scoped command for that step.
-- Creates:        Function public.app_provision_workspace_member(uuid, text),
--                 SECURITY DEFINER, owned by app_foundation_reader, EXECUTE for
--                 app_api only. It inserts one ACTIVE membership for an existing
--                 profile in the caller's current workspace.
-- Modifies:       Nothing. No table, column, policy or existing grant changes:
--                 app_foundation_reader already holds the SELECT and INSERT
--                 privileges on workspace_memberships (0001, 0006) and SELECT (id)
--                 on profiles (0001) that the function uses.
-- Deletes:        Nothing.
-- Constraints:    None added. The existing UNIQUE (workspace_id, user_id) makes a
--                 concurrent duplicate fail instead of creating a second row.
-- Indexes:        None.
-- RLS/security:   The function checks, inside, that the caller (app.user_id) is the
--                 ACTIVE OWNER of the current workspace (app.workspace_id); anyone
--                 else gets 42501. It can never grant OWNER, never reactivates a
--                 revoked membership, and adds only a user that already has a
--                 profile. It reads nothing from auth and returns only the
--                 membership id, role and whether it was created. The migration
--                 user is a member of app_foundation_reader only for the duration
--                 of this transaction (the 0006/0018 convention).
--                 New capability to be aware of: a workspace Owner, through the
--                 API role, can add an existing user without that user accepting
--                 an invitation. Only the RevenueOS provisioning route calls it.
-- Functions:      One created (above). No trigger.
-- Existing data:  Unchanged. Nothing is read or written at migration time.
-- Application:    Additive. Needed only by POST /integrations/revenueos/users,
--                 which is off unless REVENUEOS_PROVISIONING_ENABLED is true. Code
--                 that does not call the function is unaffected.
-- Risk:           Low. One new function; the postflight fails the migration if its
--                 owner or grants are not exactly as intended.
-- Recovery:       DROP FUNCTION public.app_provision_workspace_member(uuid, text);
--                 Memberships it created stay valid rows and can be revoked in the
--                 Team page like any other.
-- Needs:          0001 and 0006 applied.
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF pg_catalog.to_regprocedure('public.app_bootstrap_workspace(text,text)') IS NULL THEN
        RAISE EXCEPTION '0038 requires 0006_workspace_bootstrap.sql to already be applied';
    END IF;
    IF pg_catalog.to_regprocedure('public.app_provision_workspace_member(uuid,text)') IS NOT NULL THEN
        RAISE EXCEPTION '0038 already applied: app_provision_workspace_member exists';
    END IF;
END;
$preflight$;

-- Temporary migration-only elevation to own the new function, mirroring 0006
-- and 0018; revoked again before COMMIT.
GRANT app_foundation_reader TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;
GRANT CREATE ON SCHEMA public TO app_foundation_reader;

CREATE FUNCTION public.app_provision_workspace_member(p_user_id uuid, p_role_code text)
RETURNS TABLE (
    membership_id uuid,
    role_code text,
    created boolean
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
    actor uuid;
    ws_id uuid;
    existing_id uuid;
    existing_role text;
    existing_status text;
    new_id uuid;
BEGIN
    actor := public.app_current_user_id();
    ws_id := public.app_current_workspace_id();
    IF actor IS NULL OR ws_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '28000',
            MESSAGE = 'Authentication and active workspace context required';
    END IF;

    -- Read from the table, not app_has_permission(): this function's owner
    -- cannot execute that helper, and ownership is the whole rule here.
    IF NOT EXISTS (
        SELECT 1 FROM public.workspace_memberships AS m
        WHERE m.workspace_id = ws_id AND m.user_id = actor
          AND m.role_code = 'OWNER' AND m.status = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Only the workspace owner may add members';
    END IF;

    IF p_role_code IS NULL OR p_role_code NOT IN ('ADMIN', 'MANAGER', 'MEMBER', 'VIEWER') THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Role must be ADMIN, MANAGER, MEMBER or VIEWER';
    END IF;

    IF p_user_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM public.profiles AS p WHERE p.id = p_user_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '02000',
            MESSAGE = 'User profile not found';
    END IF;

    SELECT m.id, m.role_code, m.status INTO existing_id, existing_role, existing_status
    FROM public.workspace_memberships AS m
    WHERE m.workspace_id = ws_id AND m.user_id = p_user_id;

    IF existing_id IS NOT NULL THEN
        IF existing_status <> 'ACTIVE' THEN
            -- Restoring removed access is a deliberate act in the Team page,
            -- never a side effect of a repeated request.
            RAISE EXCEPTION USING ERRCODE = '23505',
                MESSAGE = 'User was previously removed from this workspace';
        END IF;
        RETURN QUERY SELECT existing_id, existing_role, false;
        RETURN;
    END IF;

    INSERT INTO public.workspace_memberships (workspace_id, user_id, role_code)
        VALUES (ws_id, p_user_id, p_role_code)
        RETURNING id INTO new_id;

    RETURN QUERY SELECT new_id, p_role_code, true;
END;
$function$;

COMMENT ON FUNCTION public.app_provision_workspace_member(uuid, text) IS
    'RevenueOS provisioning only (ADR-0020). Lets the ACTIVE OWNER of the current workspace add an existing profile as ADMIN/MANAGER/MEMBER/VIEWER without an invitation. Never grants OWNER and never reactivates a revoked membership.';

ALTER FUNCTION public.app_provision_workspace_member(uuid, text) OWNER TO app_foundation_reader;
REVOKE ALL ON FUNCTION public.app_provision_workspace_member(uuid, text)
    FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.app_provision_workspace_member(uuid, text) TO app_api;

REVOKE CREATE ON SCHEMA public FROM app_foundation_reader;
REVOKE app_foundation_reader FROM CURRENT_USER;

DO $postflight$
BEGIN
    IF pg_catalog.has_schema_privilege('app_foundation_reader', 'public', 'CREATE') THEN
        RAISE EXCEPTION '0038 must not leave app_foundation_reader with CREATE on schema public';
    END IF;
    IF pg_catalog.pg_has_role('app_api', 'app_foundation_reader', 'MEMBER') THEN
        RAISE EXCEPTION '0038 forbids API membership in the foundation reader role';
    END IF;
    IF (SELECT pg_catalog.pg_get_userbyid(proowner) FROM pg_catalog.pg_proc
        WHERE oid = 'public.app_provision_workspace_member(uuid,text)'::pg_catalog.regprocedure)
       <> 'app_foundation_reader' THEN
        RAISE EXCEPTION '0038 postcondition failed: wrong function owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
            'app_api', 'public.app_provision_workspace_member(uuid,text)', 'EXECUTE')
       OR pg_catalog.has_function_privilege(
            'anon', 'public.app_provision_workspace_member(uuid,text)', 'EXECUTE')
       OR pg_catalog.has_function_privilege(
            'authenticated', 'public.app_provision_workspace_member(uuid,text)', 'EXECUTE')
       OR pg_catalog.has_function_privilege(
            'app_worker_general', 'public.app_provision_workspace_member(uuid,text)', 'EXECUTE') THEN
        RAISE EXCEPTION '0038 postcondition failed: only app_api may execute the command';
    END IF;
END;
$postflight$;

COMMIT;
