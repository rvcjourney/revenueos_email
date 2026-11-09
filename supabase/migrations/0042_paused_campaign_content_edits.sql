-- Edits to an activated campaign's emails while it is PAUSED. Forward migration
-- per CLAUDE.md/AGENTS.md. See docs/adr/0020-pause-resume-paused-edits-and-late-enrollment.md.
-- Depends on 0040 only by deployment order, not by SQL. Its postcondition needs the two guard
-- functions to be owned by app_integrity_guard: on a database where they are not (production
-- was found with postgres as owner), apply 0041_guard_function_ownership.sql first.
--
-- Migration review
--   Purpose:        let a user with campaigns.execute change the content and waits of
--                   a PAUSED, activated, STANDARD campaign's frozen sequence, add
--                   steps, and remove/reorder steps no lead has reached, and let the
--                   post-resume reconcile pass rewrite the rendered snapshot of
--                   messages that were never handed to a provider.
--   Creates:        two read-only helper grants/policies for the guard role (below).
--   Modifies:       functions app_guard_frozen_sequence_steps(),
--                   app_guard_frozen_step_attachments() (frozen -> frozen unless the
--                   campaign is PAUSED), app_guard_message_snapshot() (one narrow
--                   exemption); RLS policies sequence_steps_api_{insert,update,delete}
--                   and campaign_step_attachments_api_{insert,delete} (now also
--                   require the parent campaign to be DRAFT, or PAUSED + activated
--                   with campaigns.execute).
--   Deletes:        nothing (the five policies are altered in place).
--   Constraints:    none.
--   Indexes:        none (the frontier query uses messages_step_fkey /
--                   campaign_enrollments next_step_id foreign-key lookups).
--   RLS/security:   * sequence_steps / campaign_step_attachments: a MEMBER
--                     (campaigns.draft only) can no longer write steps of a campaign
--                     that is not DRAFT; a MANAGER+ (campaigns.execute) can while it
--                     is PAUSED. The triggers below remain the authoritative check.
--                   * app_integrity_guard (non-login definer role that owns the
--                     guard functions) gains SELECT on messages and sequence_steps
--                     with USING(true) policies, solely so the guard can compute the
--                     frontier. No other role is affected.
--   Guard rules while the campaign is PAUSED and activated (STANDARD only; a
--   HYPER_PERSONALIZED campaign's steps stay frozen):
--                   content columns and wait_duration_minutes of any step: free;
--                   step kind: never changes; INSERT/DELETE/position change: only for
--                   steps strictly after the FRONTIER = the highest position that any
--                   message or enrollment pointer already references. Attachments:
--                   insert/delete allowed (they are read live at send time).
--   Message snapshot: content_subject / content_body_html / content_digest may be
--                   rewritten only by app_worker_general with app.reconcile='on', for
--                   a SCHEDULED or RETRY_SCHEDULED message that has no transport
--                   identity (rfc_message_id NULL), was never claimed (dispatch_origin
--                   NULL), has no PREPARED/ACCEPTED/UNKNOWN attempt, and whose campaign
--                   is RUNNING with a newer schedule_generation (i.e. just resumed).
--                   rendered_at, renderer_version, destination and sender stay fixed.
--   Existing data:  none rewritten. Existing campaigns behave as before in every
--                   state except PAUSED.
--   App impact:     the paused-edit services call these paths; apply BEFORE deploying
--                   them. Until applied, edits to a frozen sequence keep failing as
--                   they do today.
--   Risk:           medium. The guards protect frozen send content, so the rules were
--                   written narrowly and are covered by real-database tests
--                   (backend/tests/test_campaign_paused_edits_real_db.py).
--   Elevation:      the two step guards are owned by app_integrity_guard (0041). The migration
--                   user takes that role's membership for this transaction only
--                   (GRANT ... WITH INHERIT TRUE, SET TRUE) and revokes it again before the
--                   postcondition, exactly as 0033/0034 do for the same functions.
--   Rollback:       CREATE OR REPLACE the three functions with their 0034 / 0033
--                   bodies, restore the five policies with their 0003 / 0025
--                   definitions (ALTER POLICY), REVOKE the two grants and DROP the two policies.
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF pg_catalog.to_regclass('public.campaign_step_attachments') IS NULL
       OR pg_catalog.to_regclass('public.sequence_steps') IS NULL THEN
        RAISE EXCEPTION '0042 requires 0003 and 0025 to already be applied';
    END IF;
    -- The replacement guards read these columns on EVERY step write (draft ones
    -- too), so a database that predates them would break all sequence editing.
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.campaigns'::pg_catalog.regclass
          AND attname = 'erased_at' AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '0042 requires 0033_archive_purge_erasure.sql (campaigns.erased_at) to already be applied';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.campaigns'::pg_catalog.regclass
          AND attname = 'campaign_type' AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '0042 requires 0026_campaign_type_and_personalization_config.sql (campaigns.campaign_type) to already be applied';
    END IF;
    IF pg_catalog.to_regclass('public.message_attempts') IS NULL THEN
        RAISE EXCEPTION '0042 requires 0004_messages_events_inbox.sql to already be applied';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_policies
        WHERE schemaname = 'public' AND tablename = 'messages'
          AND policyname = 'messages_app_integrity_guard_frontier_read'
    ) THEN
        RAISE EXCEPTION '0042 already applied: messages_app_integrity_guard_frontier_read exists';
    END IF;
END;
$preflight$;

-- Temporary migration-only elevation, revoked again before the postcondition (same as
-- 0033/0034, which replace these same guards). The two step guards are owned by
-- app_integrity_guard (0041); CREATE OR REPLACE FUNCTION needs the migration user to hold
-- that role's privileges, and a managed `postgres` is not a superuser.
GRANT app_integrity_guard TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;

-- ---------------------------------------------------------------------------
-- Read access for the guard role (frontier computation only)
-- ---------------------------------------------------------------------------

GRANT SELECT ON public.messages TO app_integrity_guard;
CREATE POLICY messages_app_integrity_guard_frontier_read ON public.messages
FOR SELECT TO app_integrity_guard USING (true);

GRANT SELECT ON public.sequence_steps TO app_integrity_guard;
CREATE POLICY sequence_steps_app_integrity_guard_frontier_read ON public.sequence_steps
FOR SELECT TO app_integrity_guard USING (true);

-- ---------------------------------------------------------------------------
-- Frozen steps: editable while the campaign is PAUSED (ADR-0020)
-- Same signature, ownership and DELETE/erasure bypass as 0034; ownership is
-- preserved by CREATE OR REPLACE and asserted below.
-- ---------------------------------------------------------------------------

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

    IF parent_status = 'FROZEN' AND campaign_state = 'PAUSED'
       AND campaign_activation IS NOT NULL AND campaign_kind = 'STANDARD' THEN
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
    -- Attachments are read live (and checksum-verified) when a message is sent, so
    -- adding or removing one on a paused STANDARD campaign needs no message rewrite.
    IF parent_status = 'FROZEN' AND campaign_state = 'PAUSED'
       AND campaign_activation IS NOT NULL AND campaign_kind = 'STANDARD'
       AND TG_OP IN ('INSERT', 'DELETE') THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Frozen sequence attachments are immutable';
END;
$function$;

-- ---------------------------------------------------------------------------
-- Message snapshot: one more narrow exemption (post-resume reconcile)
-- Unchanged below the new alternative: same body as 0033.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.app_guard_message_snapshot() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = ''
AS $function$
DECLARE
    exempt boolean := false;
BEGIN
    IF OLD.rendered_at IS NOT NULL AND
       (NEW.content_subject, NEW.content_body_html, NEW.content_digest, NEW.renderer_version, NEW.rendered_at,
        NEW.frozen_destination, NEW.frozen_sender_address, NEW.frozen_sender_name)
       IS DISTINCT FROM
       (OLD.content_subject, OLD.content_body_html, OLD.content_digest, OLD.renderer_version, OLD.rendered_at,
        OLD.frozen_destination, OLD.frozen_sender_address, OLD.frozen_sender_name) THEN
        -- Erasure may replace the recipient-specific parts of the snapshot, and
        -- nothing else: the timing, renderer and sender identity stay fixed.
        -- (COALESCE: an unset custom setting is NULL, and NOT NULL would skip the
        -- RAISE below -- every exemption must fail closed.)
        exempt := COALESCE(
            CURRENT_USER = 'app_erasure'
            AND COALESCE(pg_catalog.current_setting('app.erasure', true), '') = 'on'
            AND NEW.rendered_at IS NOT DISTINCT FROM OLD.rendered_at
            AND NEW.renderer_version = OLD.renderer_version
            AND NEW.frozen_sender_address IS NOT DISTINCT FROM OLD.frozen_sender_address
            AND NEW.frozen_sender_name IS NOT DISTINCT FROM OLD.frozen_sender_name,
            false);
        -- ADR-0020: after a resume the reconcile pass re-renders a message that no
        -- provider has ever been asked to send, so edited content reaches it. Only
        -- the subject/body/digest may change, and only while the campaign is
        -- RUNNING at a newer schedule_generation than the message.
        IF NOT exempt THEN
            exempt := COALESCE(
                CURRENT_USER = 'app_worker_general'
                AND COALESCE(pg_catalog.current_setting('app.reconcile', true), '') = 'on'
                AND OLD.status IN ('SCHEDULED', 'RETRY_SCHEDULED')
                AND NEW.status = OLD.status
                AND OLD.rfc_message_id IS NULL
                AND OLD.dispatch_origin IS NULL
                AND NEW.rendered_at IS NOT DISTINCT FROM OLD.rendered_at
                AND NEW.renderer_version = OLD.renderer_version
                AND NEW.frozen_destination IS NOT DISTINCT FROM OLD.frozen_destination
                AND NEW.frozen_sender_address IS NOT DISTINCT FROM OLD.frozen_sender_address
                AND NEW.frozen_sender_name IS NOT DISTINCT FROM OLD.frozen_sender_name
                AND NOT EXISTS (
                    SELECT 1 FROM public.message_attempts a
                     WHERE a.workspace_id = OLD.workspace_id AND a.message_id = OLD.id
                       AND a.evidence_state IN ('PREPARED', 'ACCEPTED', 'UNKNOWN')
                )
                AND EXISTS (
                    SELECT 1 FROM public.campaigns c
                     WHERE c.workspace_id = OLD.workspace_id AND c.id = OLD.campaign_id
                       AND c.status = 'RUNNING'
                       AND c.schedule_generation > OLD.schedule_generation
                ),
                false);
        END IF;
        IF NOT exempt THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Rendered message snapshot is immutable';
        END IF;
    END IF;
    IF OLD.status IN ('SENT','FAILED','SKIPPED','CANCELLED') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Terminal message cannot be reopened';
    END IF;
    IF OLD.rfc_message_id IS NOT NULL AND NEW.rfc_message_id IS DISTINCT FROM OLD.rfc_message_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Transport correlation identity is immutable';
    END IF;
    RETURN NEW;
END;
$function$;

-- ---------------------------------------------------------------------------
-- Row policies: the trigger decides WHAT may change, RLS decides WHO may
-- ---------------------------------------------------------------------------

ALTER POLICY sequence_steps_api_insert ON public.sequence_steps
WITH CHECK (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('campaigns.draft')
    AND EXISTS (SELECT 1 FROM public.campaigns c
                 WHERE c.workspace_id = sequence_steps.workspace_id AND c.id = sequence_steps.campaign_id
                   AND (c.status = 'DRAFT'
                        OR (c.status = 'PAUSED' AND c.activation_id IS NOT NULL
                            AND public.app_has_permission('campaigns.execute'))))
);

ALTER POLICY sequence_steps_api_update ON public.sequence_steps
USING (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('campaigns.draft')
    AND EXISTS (SELECT 1 FROM public.campaigns c
                 WHERE c.workspace_id = sequence_steps.workspace_id AND c.id = sequence_steps.campaign_id
                   AND (c.status = 'DRAFT'
                        OR (c.status = 'PAUSED' AND c.activation_id IS NOT NULL
                            AND public.app_has_permission('campaigns.execute'))))
)
WITH CHECK (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('campaigns.draft')
);

ALTER POLICY sequence_steps_api_delete ON public.sequence_steps
USING (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('campaigns.draft')
    AND EXISTS (SELECT 1 FROM public.campaigns c
                 WHERE c.workspace_id = sequence_steps.workspace_id AND c.id = sequence_steps.campaign_id
                   AND (c.status = 'DRAFT'
                        OR (c.status = 'PAUSED' AND c.activation_id IS NOT NULL
                            AND public.app_has_permission('campaigns.execute'))))
);

ALTER POLICY campaign_step_attachments_api_insert ON public.campaign_step_attachments
WITH CHECK (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('campaigns.draft')
    AND EXISTS (SELECT 1 FROM public.campaigns c
                 WHERE c.workspace_id = campaign_step_attachments.workspace_id
                   AND c.id = campaign_step_attachments.campaign_id
                   AND (c.status = 'DRAFT'
                        OR (c.status = 'PAUSED' AND c.activation_id IS NOT NULL
                            AND public.app_has_permission('campaigns.execute'))))
);

ALTER POLICY campaign_step_attachments_api_delete ON public.campaign_step_attachments
USING (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('campaigns.draft')
    AND EXISTS (SELECT 1 FROM public.campaigns c
                 WHERE c.workspace_id = campaign_step_attachments.workspace_id
                   AND c.id = campaign_step_attachments.campaign_id
                   AND (c.status = 'DRAFT'
                        OR (c.status = 'PAUSED' AND c.activation_id IS NOT NULL
                            AND public.app_has_permission('campaigns.execute'))))
);

REVOKE app_integrity_guard FROM CURRENT_USER;

DO $postcondition$
DECLARE
    fn text;
BEGIN
    -- The two step guards must stay SECURITY DEFINER and owned by the guard role.
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
            RAISE EXCEPTION '0042 postcondition failed: % is not SECURITY DEFINER owned by app_integrity_guard', fn;
        END IF;
    END LOOP;
    -- The message snapshot guard must stay SECURITY INVOKER (its exemptions key on CURRENT_USER).
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_message_snapshot()'::pg_catalog.regprocedure AND p.prosecdef
    ) THEN
        RAISE EXCEPTION '0042 postcondition failed: app_guard_message_snapshot became SECURITY DEFINER';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.pg_policies
         WHERE schemaname = 'public'
           AND policyname IN ('sequence_steps_api_insert', 'sequence_steps_api_update', 'sequence_steps_api_delete',
                              'campaign_step_attachments_api_insert', 'campaign_step_attachments_api_delete')) <> 5 THEN
        RAISE EXCEPTION '0042 postcondition failed: expected 5 policies';
    END IF;
END;
$postcondition$;

COMMIT;
