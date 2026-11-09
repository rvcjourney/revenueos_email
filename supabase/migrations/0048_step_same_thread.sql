-- Follow-up emails sent in the same thread as the lead's previous email.
-- Forward migration per CLAUDE.md/AGENTS.md. See docs/adr/0021-follow-up-same-thread.md.
-- Requires 0046 (it replaces the function 0046 last replaced). Apply after 0046 and 0047,
-- BEFORE deploying the code that uses these columns. Never edit 0042/0046: they are applied
-- or prepared separately.
--
-- Migration review
--   Purpose:        A follow-up EMAIL step can be sent as a reply to the lead's previous
--                   email (ADR-0021). The step carries one switch; the follow-up message
--                   records which message it replies to, so the send worker can set
--                   In-Reply-To / References and the Gmail thread.
--   Creates:        sequence_steps.email_same_thread (boolean NOT NULL DEFAULT false);
--                   messages.reply_to_message_id (uuid NULL); constraint
--                   messages_reply_to_fkey; constraint messages_reply_to_self_check;
--                   index messages_reply_to_idx.
--   Modifies:       constraint sequence_steps_payload_shape_check (re-created: an EMAIL
--                   step needs email_subject unless email_same_thread; a WAIT is never
--                   threaded); function app_guard_message_snapshot() (reply_to_message_id
--                   joins the immutable snapshot); function app_guard_frozen_sequence_steps()
--                   (email_same_thread may change while PAUSED only on steps after the
--                   frontier). Both CREATE OR REPLACE: same signature, security mode and owner.
--   Deletes:        nothing.
--   Constraints:    messages_reply_to_fkey FOREIGN KEY (workspace_id, mailbox_id,
--                   reply_to_message_id) REFERENCES messages (workspace_id, mailbox_id, id)
--                   (the existing messages_mailbox_id_key): the parent is always in the same
--                   workspace and the same mailbox. ON DELETE NO ACTION, not RESTRICT: every
--                   command that deletes messages (app_delete_campaign, app_delete_mailbox,
--                   app_delete_workspace) deletes a parent and its replies in ONE statement,
--                   and NO ACTION is checked at the end of that statement, when both are gone.
--                   A parent deleted on its own is still refused. CASCADE / SET NULL were
--                   rejected: SET NULL would rewrite a frozen snapshot (the guard refuses it)
--                   and CASCADE would silently delete sent history. Erasure only UPDATEs
--                   messages and never touches this column. Both messages constraints are
--                   added NOT VALID and then validated by separate VALIDATE CONSTRAINT
--                   statements (SHARE UPDATE EXCLUSIVE: sends keep going while the existing
--                   rows are checked -- when the statements run one at a time; inside this
--                   one-transaction file the ADD COLUMN lock is held to COMMIT regardless).
--   Indexes:        messages_reply_to_idx (workspace_id, mailbox_id, reply_to_message_id)
--                   WHERE reply_to_message_id IS NOT NULL -- the FK's referencing side, so a
--                   parent delete does not scan messages. Partial: existing rows are all NULL.
--                   CREATE INDEX (not CONCURRENTLY: it runs in this transaction) takes a SHARE
--                   lock on messages, which BLOCKS WRITES (sends, planning) for the build, a
--                   full scan of messages. Run the migration at a quiet time.
--   RLS/security:   no policy changes. Column grants: app_api INSERT/UPDATE (email_same_thread)
--                   on sequence_steps; app_worker_general INSERT (reply_to_message_id) on
--                   messages. Nobody gets UPDATE on reply_to_message_id. Every role that
--                   reads either table already has a table-level SELECT grant, which covers
--                   the new columns.
--   Existing data:  none rewritten. ADD COLUMN ... DEFAULT false (constant) and a NULL column
--                   are metadata-only. The re-created sequence_steps CHECK is validated under
--                   its ALTER (a scan of the small sequence_steps table); every existing step
--                   already satisfies it. The messages constraints are validated separately
--                   (see Constraints); every existing message has reply_to_message_id NULL.
--   App impact:     the sequence API, progression, reconcile, test send and send worker use
--                   these columns (slice 1 of ADR-0021). Code deployed before this migration
--                   fails on the missing columns; this migration before the code changes
--                   nothing visible (the flag defaults false).
--   Risk:           low-medium; two guard rewrites (narrow, covered by real-database tests in
--                   backend/tests/test_same_thread_real_db.py); the index build blocks writes
--                   to messages and the validations scan it -- apply at a quiet time.
--   Elevation:      app_guard_frozen_sequence_steps is owned by app_integrity_guard (0041).
--                   The migration user takes that role's membership for this transaction
--                   only (GRANT ... WITH INHERIT TRUE, SET TRUE) and revokes it again before
--                   the postcondition, exactly as 0042/0044/0046 do.
--                   app_guard_message_snapshot is SECURITY INVOKER and owned by the migration
--                   user: no migration changes its owner (0004/0033/0042 create or replace it;
--                   0039/0041 do not list it), so it is replaced outside that window, as 0042
--                   did on production. Verified by applying this file as a non-superuser
--                   CREATEROLE role owning the tables and that function (Supabase's postgres).
--   Rollback:       CREATE OR REPLACE both functions with their 0042/0046 bodies; DROP INDEX
--                   messages_reply_to_idx; ALTER TABLE messages DROP CONSTRAINT
--                   messages_reply_to_fkey, DROP CONSTRAINT messages_reply_to_self_check,
--                   DROP COLUMN reply_to_message_id; UPDATE sequence_steps SET
--                   email_subject = 'Follow-up' WHERE email_subject IS NULL AND kind = 'EMAIL'
--                   (only if threaded steps exist); re-create sequence_steps_payload_shape_check
--                   with its 0003 body; ALTER TABLE sequence_steps DROP COLUMN email_same_thread.
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
    ), 'personalized campaigns share the paused structure rules') = 0 THEN
        RAISE EXCEPTION '0048 requires 0046_hyper_paused_structure_edits.sql to already be applied';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
         WHERE attrelid = 'public.sequence_steps'::pg_catalog.regclass
           AND attname = 'email_same_thread' AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '0048 already applied: sequence_steps.email_same_thread exists';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'messages_mailbox_id_key'
           AND conrelid = 'public.messages'::pg_catalog.regclass
    ) THEN
        RAISE EXCEPTION '0048 preflight failed: messages_mailbox_id_key is missing';
    END IF;
END;
$preflight$;

-- ---------------------------------------------------------------------------
-- sequence_steps.email_same_thread
-- ---------------------------------------------------------------------------

ALTER TABLE public.sequence_steps
    ADD COLUMN email_same_thread boolean NOT NULL DEFAULT false;

ALTER TABLE public.sequence_steps
    DROP CONSTRAINT sequence_steps_payload_shape_check,
    ADD CONSTRAINT sequence_steps_payload_shape_check CHECK (
        (kind = 'EMAIL' AND (email_subject IS NOT NULL OR email_same_thread)
            AND email_body_html IS NOT NULL AND wait_duration_minutes IS NULL)
        OR (kind = 'WAIT' AND wait_duration_minutes IS NOT NULL AND wait_duration_minutes > 0
            AND email_subject IS NULL AND email_body_html IS NULL AND NOT email_same_thread)
    );

GRANT INSERT (email_same_thread) ON public.sequence_steps TO app_api;
GRANT UPDATE (email_same_thread) ON public.sequence_steps TO app_api;

-- ---------------------------------------------------------------------------
-- messages.reply_to_message_id
-- ---------------------------------------------------------------------------

-- The constraints are added NOT VALID (no scan under the ADD's lock) and validated in
-- separate statements, which only need SHARE UPDATE EXCLUSIVE and so let sends continue
-- -- when the statements are run one at a time. In this single-transaction file the
-- ADD COLUMN's ACCESS EXCLUSIVE lock is held until COMMIT anyway.
ALTER TABLE public.messages
    ADD COLUMN reply_to_message_id uuid,
    ADD CONSTRAINT messages_reply_to_self_check CHECK (reply_to_message_id IS DISTINCT FROM id) NOT VALID,
    ADD CONSTRAINT messages_reply_to_fkey FOREIGN KEY (workspace_id, mailbox_id, reply_to_message_id)
        REFERENCES public.messages (workspace_id, mailbox_id, id) ON UPDATE RESTRICT ON DELETE NO ACTION
        NOT VALID;

ALTER TABLE public.messages VALIDATE CONSTRAINT messages_reply_to_self_check;

ALTER TABLE public.messages VALIDATE CONSTRAINT messages_reply_to_fkey;

-- Blocks writes to messages (SHARE lock) while it is built: run at a quiet time.
CREATE INDEX messages_reply_to_idx
    ON public.messages (workspace_id, mailbox_id, reply_to_message_id)
    WHERE reply_to_message_id IS NOT NULL;

GRANT INSERT (reply_to_message_id) ON public.messages TO app_worker_general;

-- ---------------------------------------------------------------------------
-- app_guard_message_snapshot: the 0042 body, with reply_to_message_id frozen
-- together with the rendered snapshot (and held fixed by both exemptions).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.app_guard_message_snapshot() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = ''
AS $function$
DECLARE
    exempt boolean := false;
BEGIN
    IF OLD.rendered_at IS NOT NULL AND
       (NEW.content_subject, NEW.content_body_html, NEW.content_digest, NEW.renderer_version, NEW.rendered_at,
        NEW.frozen_destination, NEW.frozen_sender_address, NEW.frozen_sender_name, NEW.reply_to_message_id)
       IS DISTINCT FROM
       (OLD.content_subject, OLD.content_body_html, OLD.content_digest, OLD.renderer_version, OLD.rendered_at,
        OLD.frozen_destination, OLD.frozen_sender_address, OLD.frozen_sender_name, OLD.reply_to_message_id) THEN
        -- Erasure may replace the recipient-specific parts of the snapshot, and
        -- nothing else: the timing, renderer, sender identity and thread stay fixed.
        -- (COALESCE: an unset custom setting is NULL, and NOT NULL would skip the
        -- RAISE below -- every exemption must fail closed.)
        exempt := COALESCE(
            CURRENT_USER = 'app_erasure'
            AND COALESCE(pg_catalog.current_setting('app.erasure', true), '') = 'on'
            AND NEW.rendered_at IS NOT DISTINCT FROM OLD.rendered_at
            AND NEW.renderer_version = OLD.renderer_version
            AND NEW.frozen_sender_address IS NOT DISTINCT FROM OLD.frozen_sender_address
            AND NEW.frozen_sender_name IS NOT DISTINCT FROM OLD.frozen_sender_name
            AND NEW.reply_to_message_id IS NOT DISTINCT FROM OLD.reply_to_message_id,
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
                AND NEW.reply_to_message_id IS NOT DISTINCT FROM OLD.reply_to_message_id
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

-- Temporary migration-only elevation, revoked again before the postcondition (same as
-- 0042/0044/0046). The guard is owned by app_integrity_guard (0041); CREATE OR REPLACE
-- FUNCTION needs the migration user to hold that role's privileges.
GRANT app_integrity_guard TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;

-- ---------------------------------------------------------------------------
-- app_guard_frozen_sequence_steps: the 0046 body, plus the same-thread switch
-- following the frontier rule (ADR-0021 D7).
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
            -- 0048: whether a step replies in the same thread decides which message its
            -- emails point at, so it is structural: only after the frontier.
            IF NEW.email_same_thread IS DISTINCT FROM OLD.email_same_thread
               AND (OLD.position <= frontier OR NEW.position <= frontier) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Whether a step leads have already reached is sent in the same thread cannot change';
            END IF;
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Frozen sequence steps are immutable';
END;
$function$;

REVOKE app_integrity_guard FROM CURRENT_USER;

DO $postcondition$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_sequence_steps()'::pg_catalog.regprocedure
           AND p.prosecdef
           AND pg_catalog.pg_get_userbyid(p.proowner) = 'app_integrity_guard'
    ) THEN
        RAISE EXCEPTION '0048 postcondition failed: app_guard_frozen_sequence_steps is not SECURITY DEFINER owned by app_integrity_guard';
    END IF;
    IF pg_catalog.strpos((
        SELECT p.prosrc FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_frozen_sequence_steps()'::pg_catalog.regprocedure
    ), 'email_same_thread') = 0 THEN
        RAISE EXCEPTION '0048 postcondition failed: the frozen-steps guard body is not in place';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_message_snapshot()'::pg_catalog.regprocedure
           AND (p.prosecdef OR pg_catalog.strpos(p.prosrc, 'reply_to_message_id') = 0)
    ) THEN
        RAISE EXCEPTION '0048 postcondition failed: the message snapshot guard body is not in place';
    END IF;
    IF NOT pg_catalog.has_column_privilege('app_api', 'public.sequence_steps', 'email_same_thread', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege('app_worker_general', 'public.messages', 'reply_to_message_id', 'INSERT')
       OR pg_catalog.has_column_privilege('app_worker_general', 'public.messages', 'reply_to_message_id', 'UPDATE')
       OR pg_catalog.has_column_privilege('app_worker_send', 'public.messages', 'reply_to_message_id', 'UPDATE') THEN
        RAISE EXCEPTION '0048 postcondition failed: column grants are not as intended';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'messages_reply_to_fkey'
           AND conrelid = 'public.messages'::pg_catalog.regclass
           AND confdeltype = 'a' AND convalidated
    ) THEN
        RAISE EXCEPTION '0048 postcondition failed: messages_reply_to_fkey is missing, not NO ACTION or not validated';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'messages_reply_to_self_check'
           AND conrelid = 'public.messages'::pg_catalog.regclass
           AND convalidated
    ) THEN
        RAISE EXCEPTION '0048 postcondition failed: messages_reply_to_self_check is missing or not validated';
    END IF;
END;
$postcondition$;

COMMIT;
