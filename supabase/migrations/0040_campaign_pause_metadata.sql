-- Campaign pause metadata. Forward migration per CLAUDE.md/AGENTS.md.
-- See docs/adr/0020-pause-resume-paused-edits-and-late-enrollment.md.
--
-- Migration review
--   Purpose:        record when a campaign was paused, for how long in total and
--                   when it last resumed, so the UI can say "Paused 18 minutes
--                   ago" and operators can see total pause time without reading
--                   the audit log back.
--   Creates:        campaigns.paused_at (timestamptz NULL),
--                   campaigns.paused_total_seconds (bigint NOT NULL DEFAULT 0),
--                   campaigns.last_resumed_at (timestamptz NULL), one CHECK
--                   (paused_total_seconds >= 0).
--   Modifies:       column-level UPDATE grants for app_api on the three new
--                   columns (0003 lists writable columns explicitly). Pause and
--                   resume run as app_api in the same UPDATE that changes status.
--   Deletes:        nothing.
--   Constraints:    paused_total_seconds >= 0.
--   Indexes:        none.
--   RLS/security:   unchanged. The existing campaigns_api_update policy
--                   (campaigns.execute for any activated campaign) already covers
--                   the new columns; app_guard_campaign_snapshot does not restrict
--                   them. No other role is granted write access.
--   Existing data:  none rewritten (ADD COLUMN with a constant default is
--                   metadata-only). A campaign that is PAUSED when this is applied
--                   has paused_at NULL; the UI and API fall back to updated_at.
--                   No backfill UPDATE on purpose: it would bump campaigns.version
--                   and make every open browser tab's expected_version stale.
--   App impact:     pause/resume and the campaign read schemas select the new
--                   columns, so apply this BEFORE deploying the matching backend.
--   Risk:           low; additive.
--   Rollback:       REVOKE the grants, DROP CONSTRAINT campaigns_paused_total_check,
--                   DROP COLUMN the three columns (pause history in audit_events is
--                   unaffected).
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF pg_catalog.to_regclass('public.campaigns') IS NULL THEN
        RAISE EXCEPTION '0040 requires 0003_mailboxes_campaigns.sql to already be applied';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.campaigns'::pg_catalog.regclass
          AND attname IN ('paused_at', 'paused_total_seconds', 'last_resumed_at')
          AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '0040 already applied: a pause metadata column already exists';
    END IF;
END;
$preflight$;

ALTER TABLE public.campaigns
    ADD COLUMN paused_at timestamptz,
    ADD COLUMN paused_total_seconds bigint NOT NULL DEFAULT 0,
    ADD COLUMN last_resumed_at timestamptz,
    ADD CONSTRAINT campaigns_paused_total_check CHECK (paused_total_seconds >= 0);

GRANT UPDATE (paused_at, paused_total_seconds, last_resumed_at) ON public.campaigns TO app_api;

DO $postcondition$
DECLARE
    runtime_role text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'campaigns_paused_total_check'
          AND conrelid = 'public.campaigns'::pg_catalog.regclass
    ) THEN
        RAISE EXCEPTION '0040 postcondition failed: CHECK constraint missing';
    END IF;
    IF NOT pg_catalog.has_column_privilege('app_api', 'public.campaigns', 'paused_at', 'UPDATE') THEN
        RAISE EXCEPTION '0040 postcondition failed: app_api cannot UPDATE campaigns.paused_at';
    END IF;

    -- Only the API (pause/resume commands) may write pause metadata.
    FOREACH runtime_role IN ARRAY ARRAY[
        'app_worker_general', 'app_worker_send', 'app_worker_sync',
        'app_scheduler', 'app_integrity_guard', 'app_outbox_relay'
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = runtime_role)
           AND (
               pg_catalog.has_column_privilege(runtime_role, 'public.campaigns', 'paused_at', 'UPDATE')
               OR pg_catalog.has_column_privilege(runtime_role, 'public.campaigns', 'paused_total_seconds', 'UPDATE')
               OR pg_catalog.has_column_privilege(runtime_role, 'public.campaigns', 'last_resumed_at', 'UPDATE')
           ) THEN
            RAISE EXCEPTION '0040 postcondition failed: % can UPDATE pause metadata', runtime_role;
        END IF;
    END LOOP;
END;
$postcondition$;

COMMIT;
