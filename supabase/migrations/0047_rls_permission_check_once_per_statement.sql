-- Evaluate the RLS permission check once per statement instead of once per row.
--
-- Migration:      0047_rls_permission_check_once_per_statement.sql
-- Purpose:        Every API read of a tenant table pays for RLS. The row policies
--                 are `workspace_id = (SELECT app_current_workspace_id()) AND
--                 app_has_permission('product.read')`. The first term is wrapped in
--                 a sub-select, so Postgres evaluates it once per statement (an
--                 InitPlan). The second is not, so Postgres calls the function once
--                 for EVERY row it reads. app_has_permission() only reads per-
--                 request settings (it is STABLE), so its answer is the same for
--                 the whole statement; calling it per row only costs time.
--                 Measured on a local Postgres with 15,000 messages in one
--                 campaign: `SELECT count(*) FROM messages` as app_api took
--                 1,689 ms before and 4 ms after. The existing campaign analytics
--                 query took 4.6 s on the same data; the campaign Activity feed
--                 took 8-12 s. The same cost applies to leads, inbox and every
--                 other list that reads many rows.
-- Order:          Independent of 0040-0046 (no object they create is referenced).
--                 Apply after 0046. Idempotent: a policy that is already wrapped
--                 is left alone, so a second run changes nothing.
-- Creates:        Nothing.
-- Modifies:       Every policy in schema public whose USING / WITH CHECK
--                 expression calls app_has_permission(...) unwrapped (137 calls in
--                 126 expressions on a database built from the committed chain)
--                 is altered so the call is `(SELECT public.app_has_permission(..))`.
--                 Nothing else about a policy changes: its name, command, roles,
--                 and every other part of its expression stay byte-for-byte as
--                 Postgres stores them.
-- Deletes:        Nothing.
-- Constraints/Indexes/Functions/Triggers/Grants: none changed.
-- RLS/security:   The ACCESS RULE is unchanged; only when the function is called
--                 changes. Inside one statement the function's inputs (the
--                 app.* settings and the role) cannot change, so the result is
--                 identical for every row. The migration proves this itself: for
--                 each altered policy it reads the stored expression back,
--                 removes the new wrapper and requires the result to equal the
--                 original text; any difference, or any policy that still calls
--                 the function unwrapped afterwards, raises and rolls EVERYTHING
--                 back. It also asserts the policy count is unchanged.
--                 Policies for non-API roles (connection, scheduler, workers,
--                 erasure ...) are wrapped too; they have the same shape and the
--                 same semantics.
-- Existing data:  Untouched. No rows are read or written.
-- Application:    No code change is needed. Queries return the same rows; they
--                 stop paying a per-row function call.
-- Locking:        ALTER POLICY takes a short ACCESS EXCLUSIVE lock on each table,
--                 held until COMMIT because this is one transaction over ~100
--                 tables. lock_timeout is set to 5 s so that, if a long-running
--                 query or worker transaction holds a table, the migration fails
--                 cleanly (nothing changed) instead of queueing and blocking
--                 live traffic. Apply at a quiet moment; retry if it times out.
-- Risk:           Low. Metadata-only change, fully transactional, self-verifying.
--                 Not verifiable against Supabase from this repository (the local
--                 server is a superuser). After applying, as a normal workspace
--                 member open Campaigns > Analytics for a large campaign (it must
--                 load and show the same numbers), and confirm a user of another
--                 workspace still cannot read it (the Cross-tenant tests in
--                 backend/tests cover this against a real Postgres).
-- Recovery:       Run the following (restores the unwrapped form in every policy):
--                   DO $undo$ DECLARE pol record; BEGIN
--                     SET LOCAL search_path = '';
--                     FOR pol IN SELECT p.polname, c.relname,
--                         pg_catalog.pg_get_expr(p.polqual, p.polrelid) AS q,
--                         pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) AS w
--                       FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c
--                         ON c.oid = p.polrelid
--                       WHERE c.relnamespace = 'public'::pg_catalog.regnamespace
--                     LOOP
--                       IF pol.q ~ 'SELECT public\.app_has_permission\(' THEN
--                         EXECUTE pg_catalog.format('ALTER POLICY %I ON public.%I USING (%s)',
--                           pol.polname, pol.relname, pg_catalog.regexp_replace(pol.q,
--                           '\( SELECT (public\.app_has_permission\(''[a-z._]+''::text\)) AS app_has_permission\)',
--                           '\1', 'g'));
--                       END IF;
--                       IF pol.w ~ 'SELECT public\.app_has_permission\(' THEN
--                         EXECUTE pg_catalog.format('ALTER POLICY %I ON public.%I WITH CHECK (%s)',
--                           pol.polname, pol.relname, pg_catalog.regexp_replace(pol.w,
--                           '\( SELECT (public\.app_has_permission\(''[a-z._]+''::text\)) AS app_has_permission\)',
--                           '\1', 'g'));
--                       END IF;
--                     END LOOP; END $undo$;
-- Needs:          Whatever created the policies (0001-0046). The migration user
--                 must own (or be a member of the owner of) every table it alters.
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';
SET LOCAL lock_timeout = '5s';

DO $rewrite$
DECLARE
    pol            record;
    still_bare integer;
    policies_before integer;
    policies_after integer;
    altered        integer := 0;
    new_qual       text;
    new_check      text;
    unowned        text;
    -- A call that is already wrapped prints as `( SELECT public.app_has_permission(...) AS app_has_permission)`.
    wrapper_re CONSTANT text :=
        '\( SELECT (public\.app_has_permission\(''[a-z._]+''::text\)) AS app_has_permission\)';
BEGIN
    SELECT pg_catalog.count(*) INTO policies_before
    FROM pg_catalog.pg_policy p
    JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
    WHERE c.relnamespace = 'public'::pg_catalog.regnamespace;

    -- Preflight: stop with a clear message, changing nothing, if we cannot alter a table.
    SELECT pg_catalog.string_agg(DISTINCT c.relname, ', ') INTO unowned
    FROM pg_catalog.pg_policy p
    JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
    WHERE c.relnamespace = 'public'::pg_catalog.regnamespace
      AND NOT pg_catalog.pg_has_role(pg_catalog.current_user(), c.relowner, 'MEMBER');
    IF unowned IS NOT NULL THEN
        RAISE EXCEPTION '0047: current user does not own table(s) %; nothing changed', unowned;
    END IF;

    FOR pol IN
        SELECT p.polname,
               c.relname,
               pg_catalog.pg_get_expr(p.polqual, p.polrelid)      AS qual,
               pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) AS wcheck
        FROM pg_catalog.pg_policy p
        JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
        WHERE c.relnamespace = 'public'::pg_catalog.regnamespace
        ORDER BY c.relname, p.polname
    LOOP
        new_qual := NULL;
        new_check := NULL;

        -- 1. protect calls that are already wrapped, 2. wrap the bare ones, 3. unprotect.
        IF pol.qual IS NOT NULL AND pol.qual ~ 'app_has_permission\(' THEN
            new_qual := pg_catalog.replace(
                pg_catalog.regexp_replace(
                    pg_catalog.regexp_replace(
                        pol.qual,
                        'SELECT (public\.)?app_has_permission\(',
                        'SELECT \1app_has_permission_wrapped(', 'g'),
                    '(public\.)?app_has_permission\((''[a-z._]+''::text)\)',
                    '(SELECT public.app_has_permission(\2))', 'g'),
                'app_has_permission_wrapped(', 'app_has_permission(');
        END IF;
        IF pol.wcheck IS NOT NULL AND pol.wcheck ~ 'app_has_permission\(' THEN
            new_check := pg_catalog.replace(
                pg_catalog.regexp_replace(
                    pg_catalog.regexp_replace(
                        pol.wcheck,
                        'SELECT (public\.)?app_has_permission\(',
                        'SELECT \1app_has_permission_wrapped(', 'g'),
                    '(public\.)?app_has_permission\((''[a-z._]+''::text)\)',
                    '(SELECT public.app_has_permission(\2))', 'g'),
                'app_has_permission_wrapped(', 'app_has_permission(');
        END IF;

        IF new_qual IS NOT NULL AND new_qual IS DISTINCT FROM pol.qual THEN
            EXECUTE pg_catalog.format(
                'ALTER POLICY %I ON public.%I USING (%s)', pol.polname, pol.relname, new_qual);
            altered := altered + 1;
        ELSE
            new_qual := NULL;
        END IF;
        IF new_check IS NOT NULL AND new_check IS DISTINCT FROM pol.wcheck THEN
            EXECUTE pg_catalog.format(
                'ALTER POLICY %I ON public.%I WITH CHECK (%s)', pol.polname, pol.relname, new_check);
            altered := altered + 1;
        ELSE
            new_check := NULL;
        END IF;

        -- Equivalence proof: remove the wrapper we added; the stored text must equal the original.
        IF new_qual IS NOT NULL OR new_check IS NOT NULL THEN
            PERFORM 1
            FROM pg_catalog.pg_policy p
            JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
            WHERE c.relnamespace = 'public'::pg_catalog.regnamespace
              AND c.relname = pol.relname AND p.polname = pol.polname
              AND (new_qual IS NULL OR pg_catalog.regexp_replace(
                       pg_catalog.pg_get_expr(p.polqual, p.polrelid), wrapper_re, '\1', 'g')
                   = pg_catalog.regexp_replace(pol.qual, wrapper_re, '\1', 'g'))
              AND (new_check IS NULL OR pg_catalog.regexp_replace(
                       pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), wrapper_re, '\1', 'g')
                   = pg_catalog.regexp_replace(pol.wcheck, wrapper_re, '\1', 'g'));
            IF NOT FOUND THEN
                RAISE EXCEPTION
                    '0047: policy %.% changed meaning when rewritten; nothing applied',
                    pol.relname, pol.polname;
            END IF;
        END IF;
    END LOOP;

    -- Postcondition: nothing in public calls the function unwrapped any more.
    SELECT pg_catalog.count(*) INTO still_bare
    FROM pg_catalog.pg_policy p
    JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
    WHERE c.relnamespace = 'public'::pg_catalog.regnamespace
      AND (   pg_catalog.regexp_replace(
                  COALESCE(pg_catalog.pg_get_expr(p.polqual, p.polrelid), ''),
                  wrapper_re, '', 'g') ~ 'app_has_permission\('
           OR pg_catalog.regexp_replace(
                  COALESCE(pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), ''),
                  wrapper_re, '', 'g') ~ 'app_has_permission\(');
    IF still_bare <> 0 THEN
        RAISE EXCEPTION '0047: % policies still call app_has_permission per row', still_bare;
    END IF;

    SELECT pg_catalog.count(*) INTO policies_after
    FROM pg_catalog.pg_policy p
    JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
    WHERE c.relnamespace = 'public'::pg_catalog.regnamespace;
    IF policies_after <> policies_before THEN
        RAISE EXCEPTION '0047: policy count changed (% -> %)', policies_before, policies_after;
    END IF;

    RAISE NOTICE '0047: altered % policy expression(s) across % policies', altered, policies_after;
END
$rewrite$;

COMMIT;
