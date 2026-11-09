-- Structure, wait and wording edits for a PAUSED HYPER_PERSONALIZED campaign. Forward
-- migration per CLAUDE.md/AGENTS.md. See docs/adr/0020-pause-resume-paused-edits-and-late-enrollment.md.
-- Requires 0044 (it replaces the function 0044 last replaced). Never edit 0044: it is applied.
--
-- Migration review
--   Purpose:        0044 let a manager reword a personalized step's reference email only if
--                   none of its emails had been written, and froze every wait and the step
--                   list. Product decision (2026-10-12): a personalized campaign is edited
--                   like a standard one while paused -- change the wording of any step, change
--                   waits, and add / remove / reorder steps that no lead has reached yet. Emails
--                   already written keep their text (the message snapshot guard still freezes
--                   them); only emails written after the resume use the new reference.
--   Creates:        nothing.
--   Modifies:       function app_guard_frozen_sequence_steps() (CREATE OR REPLACE; same
--                   signature, SECURITY DEFINER, ownership and DELETE/erasure bypass as 0044).
--                   The STANDARD branch and the HYPER_PERSONALIZED branch of 0044 become one
--                   rule for both campaign types. Constraint
--                   personalization_previews_step_fkey (when 0029 is applied) is re-created
--                   ON DELETE CASCADE instead of RESTRICT: previews are 7-day throw-away
--                   samples made before launch, so without this a step could not be removed
--                   for a week after a personalized campaign starts. A cascading delete runs
--                   as the table owner and needs no API grant; the approvals table does not
--                   reference steps and is untouched.
--   Deletes:        nothing.
--   Constraints:    one foreign key re-created (see Modifies); nothing else.   Indexes: none
--                   (the frontier query reads messages through messages_step_fkey and
--                   campaign_enrollments through its next_step FK).
--   RLS/security:   none changed. Who may write steps stays with the 0042 policies
--                   (campaigns.execute while PAUSED + activated). app_guard_frozen_step_attachments()
--                   is NOT touched: the attachments of a personalized campaign stay frozen.
--   Guard rules for a FROZEN sequence of a PAUSED, activated campaign (STANDARD or
--   HYPER_PERSONALIZED):
--                   content columns and wait_duration_minutes of any step: free;
--                   step kind: never changes; INSERT / DELETE / position change: only for steps
--                   strictly after the FRONTIER = the highest position that any message or
--                   enrollment pointer already references.
--   Existing data:  none rewritten.
--   App impact:     the personalized edit path in the sequence service and the just-in-time
--                   writer's wait handling rely on this; apply BEFORE deploying them. Until
--                   applied, personalized steps keep the 0044 rules (the API reports why).
--   Risk:           low-medium; guard rewrite, narrow and covered by real-database tests
--                   (backend/tests/test_campaign_hyper_paused_edits_real_db.py).
--   Elevation:      the guard is owned by app_integrity_guard (0041). The migration user takes
--                   that role's membership for this transaction only (GRANT ... WITH INHERIT
--                   TRUE, SET TRUE) and revokes it again before the postcondition, exactly as
--                   0033/0034/0042/0044 do.
--   Rollback:       CREATE OR REPLACE the function with its 0044 body; re-create
--                   personalization_previews_step_fkey with ON DELETE RESTRICT.
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF pg_catalog.strpos((
        SELECT p.prosrc FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_sequence_steps()'::pg_catalog.regprocedure
    ), 'HYPER_PERSONALIZED') = 0 THEN
        RAISE EXCEPTION '0046 requires 0044_hyper_paused_reference_edits.sql to already be applied';
    END IF;
    IF pg_catalog.strpos((
        SELECT p.prosrc FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_sequence_steps()'::pg_catalog.regprocedure
    ), 'personalized campaigns share the paused structure rules') > 0 THEN
        RAISE EXCEPTION '0046 already applied: app_guard_frozen_sequence_steps already has the shared paused structure rules';
    END IF;
END;
$preflight$;

-- Temporary migration-only elevation, revoked again before the postcondition (same as
-- 0033/0034/0042/0044). The guard is owned by app_integrity_guard (0041); CREATE OR REPLACE
-- FUNCTION needs the migration user to hold that role's privileges.
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

    -- 0046: personalized campaigns share the paused structure rules with standard ones.
    -- (COALESCE: a NULL campaign type or state must fail closed, not fall through.)
    IF COALESCE(parent_status = 'FROZEN' AND campaign_state = 'PAUSED'
                AND campaign_activation IS NOT NULL
                AND campaign_kind IN ('STANDARD', 'HYPER_PERSONALIZED'), false) THEN
        -- The highest step any message or enrollment already points at. Steps up to
        -- it are part of some lead's history; only what lies beyond may be
        -- restructured. Content and waits are never structural.
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

    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Frozen sequence steps are immutable';
END;
$function$;

REVOKE app_integrity_guard FROM CURRENT_USER;

-- Sample previews are disposable: let them go with the step they were made for.
DO $previews$
BEGIN
    IF pg_catalog.to_regclass('public.personalization_previews') IS NOT NULL THEN
        ALTER TABLE public.personalization_previews
            DROP CONSTRAINT personalization_previews_step_fkey,
            ADD CONSTRAINT personalization_previews_step_fkey FOREIGN KEY (workspace_id, step_id)
                REFERENCES public.sequence_steps (workspace_id, id) ON UPDATE RESTRICT ON DELETE CASCADE;
    END IF;
END;
$previews$;

DO $postcondition$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_sequence_steps()'::pg_catalog.regprocedure
           AND p.prosecdef
           AND pg_catalog.pg_get_userbyid(p.proowner) = 'app_integrity_guard'
    ) THEN
        RAISE EXCEPTION '0046 postcondition failed: app_guard_frozen_sequence_steps is not SECURITY DEFINER owned by app_integrity_guard';
    END IF;
    IF pg_catalog.strpos((
        SELECT p.prosrc FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_sequence_steps()'::pg_catalog.regprocedure
    ), 'personalized campaigns share the paused structure rules') = 0 THEN
        RAISE EXCEPTION '0046 postcondition failed: the replacement body is not in place';
    END IF;
    IF pg_catalog.to_regclass('public.personalization_previews') IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'personalization_previews_step_fkey'
           AND conrelid = 'public.personalization_previews'::pg_catalog.regclass
           AND confdeltype = 'c'
    ) THEN
        RAISE EXCEPTION '0046 postcondition failed: previews no longer cascade from steps';
    END IF;
END;
$postcondition$;

COMMIT;
