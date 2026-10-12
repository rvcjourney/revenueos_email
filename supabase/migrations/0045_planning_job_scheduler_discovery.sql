-- Let the scheduler SEE planning jobs so it can recover lost ones. Forward migration
-- per CLAUDE.md/AGENTS.md. See docs/adr/0020-pause-resume-paused-edits-and-late-enrollment.md.
--
-- Migration review
--   Purpose:        campaign planning (enrolling the audience, rendering the first
--                   emails) runs as Celery tasks that are published AFTER the request
--                   commits. If that publish, or a task's own retry message, is lost
--                   the job sits PENDING (or PROCESSING with an expired lease) for
--                   ever: an activated campaign never leaves planning_status=PENDING,
--                   and leads added to a paused campaign never get their first email.
--                   The scheduler already runs a periodic sweep for other durable work;
--                   it needs to be able to read the planning jobs to include these.
--   Creates:        one SELECT policy on campaign_planning_jobs for app_scheduler.
--   Modifies:       table-level SELECT grant on campaign_planning_jobs for app_scheduler.
--   Deletes:        nothing.
--   Constraints:    none.   Indexes: none (the sweep reads a handful of rows through
--                   campaign_planning_jobs_due_idx on (next_due_at, id)).
--   RLS/security:   READ-ONLY and cross-tenant, exactly like the existing scheduler
--                   discovery policies on campaigns, messages and mailboxes (0011). The
--                   scheduler can only name jobs; the recovery itself is a Celery task
--                   that runs workspace-scoped as app_worker_general, which already
--                   owns every write on this table. No write privilege is added.
--   Existing data:  none touched.
--   App impact:     without this, the recovery sweep logs an error each minute and does
--                   nothing; everything else is unaffected. Apply before deploying the
--                   scheduler that contains the sweep.
--   Risk:           low; additive and read-only.
--   Rollback:       DROP POLICY campaign_planning_jobs_scheduler_discovery_select ON
--                   public.campaign_planning_jobs; REVOKE SELECT ON
--                   public.campaign_planning_jobs FROM app_scheduler.
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF pg_catalog.to_regclass('public.campaign_planning_jobs') IS NULL THEN
        RAISE EXCEPTION '0045 requires 0003_mailboxes_campaigns.sql to already be applied';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_scheduler') THEN
        RAISE EXCEPTION '0045 requires the app_scheduler role (0001/0003)';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_policies
        WHERE schemaname = 'public' AND tablename = 'campaign_planning_jobs'
          AND policyname = 'campaign_planning_jobs_scheduler_discovery_select'
    ) THEN
        RAISE EXCEPTION '0045 already applied: campaign_planning_jobs_scheduler_discovery_select exists';
    END IF;
END;
$preflight$;

GRANT SELECT ON public.campaign_planning_jobs TO app_scheduler;
CREATE POLICY campaign_planning_jobs_scheduler_discovery_select ON public.campaign_planning_jobs
FOR SELECT TO app_scheduler USING (true);

DO $postcondition$
BEGIN
    IF NOT pg_catalog.has_table_privilege('app_scheduler', 'public.campaign_planning_jobs', 'SELECT') THEN
        RAISE EXCEPTION '0045 postcondition failed: app_scheduler cannot SELECT campaign_planning_jobs';
    END IF;
    IF pg_catalog.has_table_privilege('app_scheduler', 'public.campaign_planning_jobs', 'INSERT')
       OR pg_catalog.has_any_column_privilege('app_scheduler', 'public.campaign_planning_jobs', 'UPDATE')
       OR pg_catalog.has_table_privilege('app_scheduler', 'public.campaign_planning_jobs', 'DELETE') THEN
        RAISE EXCEPTION '0045 postcondition failed: app_scheduler must stay read-only on campaign_planning_jobs';
    END IF;
END;
$postcondition$;

COMMIT;
