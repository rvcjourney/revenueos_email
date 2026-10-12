-- Reference-email edits for a PAUSED HYPER_PERSONALIZED campaign. Forward migration
-- per CLAUDE.md/AGENTS.md. See docs/adr/0020-pause-resume-paused-edits-and-late-enrollment.md.
-- Requires 0042 (it replaces two of its functions); numbered after 0043 on purpose.
--
-- Migration review
--   Purpose:        a hyper-personalized campaign stores one REFERENCE email per step;
--                   each lead's email is written from it just before sending. Let a
--                   user with campaigns.execute improve a reference while the campaign
--                   is PAUSED, for the steps whose emails have not been written yet.
--                   Steps with even one written email stay frozen, so nothing a lead has
--                   already been shown (or is about to be sent) silently changes meaning.
--   Creates:        nothing.
--   Modifies:       functions app_guard_frozen_sequence_steps() and
--                   app_guard_frozen_step_attachments() (CREATE OR REPLACE; same
--                   signature, SECURITY DEFINER, ownership and DELETE/erasure bypass as
--                   0042). Rewritten with COALESCE-hardened comparisons.
--   Deletes:        nothing.
--   Constraints:    none.   Indexes: none (the check reads messages through
--                   messages_step_fkey).
--   RLS/security:   none changed. Who may write steps stays with the 0042 policies
--                   (campaigns.execute while PAUSED + activated); this migration only
--                   widens WHAT the trigger accepts for HYPER_PERSONALIZED campaigns.
--   Guard rules for a FROZEN sequence of a PAUSED, activated HYPER_PERSONALIZED campaign:
--                   UPDATE of a step: allowed only if kind, position and
--                   wait_duration_minutes are unchanged AND no message of that step has
--                   been rendered (messages.rendered_at IS NULL for every message that
--                   references it). INSERT / DELETE / reorder / wait change: still
--                   refused. Attachments: still refused (a hyper email's attachments are
--                   part of what is written). STANDARD campaigns: unchanged from 0042.
--   Existing data:  none rewritten.
--   App impact:     the hyper edit path in the sequence service relies on this; apply
--                   BEFORE deploying it. Until applied, hyper steps stay frozen while
--                   paused exactly as today (the API reports it).
--   Risk:           low-medium; guard rewrite, narrow and covered by real-database tests
--                   (backend/tests/test_campaign_hyper_paused_edits_real_db.py).
--   Elevation:      the two functions are owned by app_integrity_guard (0041). The migration
--                   user takes that role's membership for this transaction only
--                   (GRANT ... WITH INHERIT TRUE, SET TRUE) and revokes it again before the
--                   postcondition, exactly as 0033/0034/0042 do.
--   Rollback:       CREATE OR REPLACE the two functions with their 0042 bodies.
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_policies
        WHERE schemaname = 'public' AND tablename = 'messages'
          AND policyname = 'messages_app_integrity_guard_frontier_read'
    ) THEN
        RAISE EXCEPTION '0044 requires 0042_paused_campaign_content_edits.sql to already be applied';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.campaigns'::pg_catalog.regclass
          AND attname = 'campaign_type' AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '0044 requires 0026 (campaigns.campaign_type) to already be applied';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.messages'::pg_catalog.regclass
          AND attname = 'rendered_at' AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '0044 requires messages.rendered_at (0004) to already be applied';
    END IF;
    IF pg_catalog.strpos((
        SELECT p.prosrc FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_sequence_steps()'::pg_catalog.regprocedure
    ), 'HYPER_PERSONALIZED') > 0 THEN
        RAISE EXCEPTION '0044 already applied: app_guard_frozen_sequence_steps already handles HYPER_PERSONALIZED';
    END IF;
END;
$preflight$;

-- Temporary migration-only elevation, revoked again before the postcondition (same as
-- 0033/0034 and 0042). The two step guards are owned by app_integrity_guard (0041);
-- CREATE OR REPLACE FUNCTION needs the migration user to hold that role's privileges.
GRANT app_integrity_guard TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;

CREATE OR REPLACE FUNCTION public.app_guard_frozen_sequence_steps() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ''
AS $function$
DECLARE
    tenant uuid := COALESCE(NEW.workspace_id, OLD.workspace_id);
    sequence_uuid uuid := COALESCE(NEW.sequence_id, OLD.sequence_id);
    campaign_uuid uuid := COALESCE(NEW.campaign_id, OLD.campaign_id);
    parent_status text;
    parent_erased timestamptz;
    campaign_state text;
    campaign_activation uuid;
    campaign_kind text;
    frontier integer;
BEGIN
    SELECT erased_at, status, activation_id, campaign_type
      INTO parent_erased, campaign_state, campaign_activation, campaign_kind
      FROM public.campaigns
      WHERE workspace_id = tenant AND id = campaign_uuid FOR UPDATE;
    IF TG_OP = 'DELETE' AND parent_erased IS NOT NULL THEN
        RETURN OLD;
    END IF;
    SELECT status INTO STRICT parent_status FROM public.campaign_sequences
        WHERE workspace_id = tenant AND campaign_id = campaign_uuid AND id = sequence_uuid FOR UPDATE;
    IF parent_status = 'DRAFT' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    IF COALESCE(parent_status = 'FROZEN' AND campaign_state = 'PAUSED'
                AND campaign_activation IS NOT NULL AND campaign_kind = 'STANDARD', false) THEN
        -- Unchanged from 0042.
        SELECT COALESCE(MAX(ss.position), 0) INTO frontier
          FROM public.sequence_steps ss
         WHERE ss.workspace_id = tenant AND ss.sequence_id = sequence_uuid
           AND (
               EXISTS (SELECT 1 FROM public.messages m
                        WHERE m.workspace_id = ss.workspace_id AND m.step_id = ss.id)
               OR EXISTS (SELECT 1 FROM public.campaign_enrollments e
                           WHERE e.workspace_id = ss.workspace_id AND e.next_step_id = ss.id)
           );
        IF TG_OP = 'INSERT' THEN
            IF NEW.position > frontier THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Steps that leads have already reached cannot be restructured';
        ELSIF TG_OP = 'DELETE' THEN
            IF OLD.position > frontier THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Steps that leads have already reached cannot be restructured';
        ELSE
            IF NEW.kind IS DISTINCT FROM OLD.kind THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'The type of an activated step cannot change';
            END IF;
            IF NEW.position IS DISTINCT FROM OLD.position
               AND (OLD.position <= frontier OR NEW.position <= frontier) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Steps that leads have already reached cannot be restructured';
            END IF;
            RETURN NEW;
        END IF;
    END IF;

    -- HYPER_PERSONALIZED: the reference of a step whose emails are not written yet.
    IF COALESCE(parent_status = 'FROZEN' AND campaign_state = 'PAUSED'
                AND campaign_activation IS NOT NULL
                AND campaign_kind = 'HYPER_PERSONALIZED' AND TG_OP = 'UPDATE', false) THEN
        IF NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.position IS DISTINCT FROM OLD.position
           OR NEW.wait_duration_minutes IS DISTINCT FROM OLD.wait_duration_minutes THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Only the reference email of a personalized step can change while paused';
        END IF;
        IF EXISTS (SELECT 1 FROM public.messages m
                    WHERE m.workspace_id = tenant AND m.step_id = OLD.id
                      AND m.rendered_at IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Emails for this step have already been written and cannot be changed';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Frozen sequence steps are immutable';
END;
$function$;

-- Attachments of a hyper campaign stay frozen; the function is restated only to apply
-- the same COALESCE hardening (a NULL kind must fail closed, not fall through).
CREATE OR REPLACE FUNCTION public.app_guard_frozen_step_attachments() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ''
AS $function$
DECLARE
    tenant uuid := COALESCE(NEW.workspace_id, OLD.workspace_id);
    sequence_uuid uuid := COALESCE(NEW.sequence_id, OLD.sequence_id);
    campaign_uuid uuid := COALESCE(NEW.campaign_id, OLD.campaign_id);
    parent_status text;
    parent_erased timestamptz;
    campaign_state text;
    campaign_activation uuid;
    campaign_kind text;
BEGIN
    SELECT erased_at, status, activation_id, campaign_type
      INTO parent_erased, campaign_state, campaign_activation, campaign_kind
      FROM public.campaigns
      WHERE workspace_id = tenant AND id = campaign_uuid FOR UPDATE;
    IF TG_OP = 'DELETE' AND parent_erased IS NOT NULL THEN
        RETURN OLD;
    END IF;
    SELECT status INTO STRICT parent_status FROM public.campaign_sequences
        WHERE workspace_id = tenant AND campaign_id = campaign_uuid AND id = sequence_uuid FOR UPDATE;
    IF parent_status = 'DRAFT' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF COALESCE(parent_status = 'FROZEN' AND campaign_state = 'PAUSED'
                AND campaign_activation IS NOT NULL AND campaign_kind = 'STANDARD'
                AND TG_OP IN ('INSERT', 'DELETE'), false) THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Frozen sequence attachments are immutable';
END;
$function$;

REVOKE app_integrity_guard FROM CURRENT_USER;

DO $postcondition$
DECLARE
    fn text;
BEGIN
    FOREACH fn IN ARRAY ARRAY[
        'public.app_guard_frozen_sequence_steps()',
        'public.app_guard_frozen_step_attachments()'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc p
             WHERE p.oid = fn::pg_catalog.regprocedure
               AND p.prosecdef
               AND pg_catalog.pg_get_userbyid(p.proowner) = 'app_integrity_guard'
        ) THEN
            RAISE EXCEPTION '0044 postcondition failed: % is not SECURITY DEFINER owned by app_integrity_guard', fn;
        END IF;
    END LOOP;
END;
$postcondition$;

COMMIT;
