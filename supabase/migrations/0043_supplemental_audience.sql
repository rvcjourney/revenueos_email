-- Adding leads to an activated campaign while it is PAUSED. Forward migration per
-- CLAUDE.md/AGENTS.md. See docs/adr/0020-pause-resume-paused-edits-and-late-enrollment.md.
--
-- Migration review
--   Purpose:        let a user with campaigns.execute add new leads to a PAUSED,
--                   activated campaign through a SUPPLEMENT audience captured with the
--                   existing capture machinery, without ever touching the READY
--                   activated audience (whose immutability trigger is unchanged).
--   Creates:        campaigns audiences column kind ('ACTIVATION' | 'SUPPLEMENT',
--                   default 'ACTIVATION'); one CHECK; a second unique index for
--                   supplement planning jobs.
--   Modifies:       CHECK campaign_planning_jobs_phase_shape_check (ENROLL/RENDER jobs
--                   may now carry an audience_id, which names the supplement they
--                   work on); unique index campaign_planning_jobs_activation_identity_idx
--                   (restricted to jobs without an audience_id -- same behaviour for
--                   every existing row); function app_guard_enrollment_snapshot();
--                   RLS INSERT policies on campaign_audiences and
--                   campaign_planning_jobs; app_api column grant INSERT (kind).
--   Deletes:        nothing (an index and a constraint are replaced, two policies
--                   altered in place, all in this transaction).
--   Constraints:    campaign_audiences.kind CHECK; planning-job shape CHECK relaxed
--                   for ENROLL/RENDER only.
--   Indexes:        campaign_planning_jobs_supplement_identity_idx (partial, unique
--                   per campaign/activation/phase/audience).
--   RLS/security:   * A SUPPLEMENT audience may only be inserted by campaigns.execute
--                     on a PAUSED, activated campaign; ACTIVATION audiences keep the
--                     existing rule. kind has no UPDATE grant, so it is immutable.
--                   * A supplement ENROLL/RENDER job may only be inserted by
--                     campaigns.execute for a READY SUPPLEMENT audience of a PAUSED
--                     campaign whose activation matches.
--                   * Enrollments: the guard accepts the activated audience exactly as
--                     before, or a READY SUPPLEMENT audience of the same campaign while
--                     the campaign is PAUSED. Frozen destination and variables must
--                     still equal the captured snapshot. The unique (campaign, lead)
--                     and (campaign, address) keys still forbid enrolling anyone twice.
--   Existing data:  none rewritten; every existing audience becomes 'ACTIVATION'.
--   App impact:     the supplement endpoints and worker paths need this; apply BEFORE
--                   deploying them. Existing activation behaviour is unchanged.
--   Risk:           medium; widens one trigger and two policies, covered by real-database
--                   tests (backend/tests/test_campaign_supplement_real_db.py).
--   Rollback:       restore the 0003 function body, shape CHECK, index and the two
--                   policies; DROP the new index; REVOKE the grant; DROP COLUMN kind
--                   (only safe while no SUPPLEMENT audience exists).
--
-- PREPARED ONLY. Do not apply to any shared/staging/production environment
-- without the project owner's separate review and explicit authorization.

BEGIN;
SET LOCAL search_path = '';

DO $preflight$
BEGIN
    IF pg_catalog.to_regclass('public.campaign_audiences') IS NULL
       OR pg_catalog.to_regclass('public.campaign_planning_jobs') IS NULL THEN
        RAISE EXCEPTION '0043 requires 0003_mailboxes_campaigns.sql to already be applied';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.campaign_audiences'::pg_catalog.regclass
          AND attname = 'kind' AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '0043 already applied: campaign_audiences.kind exists';
    END IF;
END;
$preflight$;

-- ---------------------------------------------------------------------------
-- Audience kind
-- ---------------------------------------------------------------------------

ALTER TABLE public.campaign_audiences
    ADD COLUMN kind text NOT NULL DEFAULT 'ACTIVATION',
    ADD CONSTRAINT campaign_audiences_kind_check CHECK (kind IN ('ACTIVATION', 'SUPPLEMENT'));

-- INSERT only: the kind is chosen at creation and never changes.
GRANT INSERT (kind) ON public.campaign_audiences TO app_api;

ALTER POLICY campaign_audiences_api_insert ON public.campaign_audiences
WITH CHECK (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('campaigns.draft')
    AND status = 'CAPTURING'
    AND (
        kind = 'ACTIVATION'
        OR (
            kind = 'SUPPLEMENT'
            AND public.app_has_permission('campaigns.execute')
            AND EXISTS (SELECT 1 FROM public.campaigns c
                         WHERE c.workspace_id = campaign_audiences.workspace_id
                           AND c.id = campaign_audiences.campaign_id
                           AND c.status = 'PAUSED' AND c.activation_id IS NOT NULL)
        )
    )
);

-- ---------------------------------------------------------------------------
-- Planning jobs: ENROLL/RENDER may name the supplement they work on
-- ---------------------------------------------------------------------------

ALTER TABLE public.campaign_planning_jobs DROP CONSTRAINT campaign_planning_jobs_phase_shape_check;
ALTER TABLE public.campaign_planning_jobs ADD CONSTRAINT campaign_planning_jobs_phase_shape_check CHECK (
    (phase = 'CAPTURE' AND audience_id IS NOT NULL AND activation_id IS NULL)
    OR (phase IN ('ENROLL', 'RENDER') AND activation_id IS NOT NULL)
);

DROP INDEX public.campaign_planning_jobs_activation_identity_idx;
CREATE UNIQUE INDEX campaign_planning_jobs_activation_identity_idx
    ON public.campaign_planning_jobs (workspace_id, campaign_id, activation_id, phase)
    WHERE phase IN ('ENROLL', 'RENDER') AND audience_id IS NULL;
CREATE UNIQUE INDEX campaign_planning_jobs_supplement_identity_idx
    ON public.campaign_planning_jobs (workspace_id, campaign_id, activation_id, phase, audience_id)
    WHERE phase IN ('ENROLL', 'RENDER') AND audience_id IS NOT NULL;

ALTER POLICY campaign_planning_jobs_api_insert ON public.campaign_planning_jobs
WITH CHECK (
    workspace_id = (SELECT public.app_current_workspace_id())
    AND public.app_has_permission('campaigns.draft')
    AND (
        audience_id IS NULL
        OR phase = 'CAPTURE'
        OR (
            phase IN ('ENROLL', 'RENDER')
            AND public.app_has_permission('campaigns.execute')
            AND EXISTS (SELECT 1
                          FROM public.campaigns c
                          JOIN public.campaign_audiences a
                            ON a.workspace_id = c.workspace_id AND a.campaign_id = c.id
                         WHERE c.workspace_id = campaign_planning_jobs.workspace_id
                           AND c.id = campaign_planning_jobs.campaign_id
                           AND c.status = 'PAUSED'
                           AND c.activation_id = campaign_planning_jobs.activation_id
                           AND a.id = campaign_planning_jobs.audience_id
                           AND a.kind = 'SUPPLEMENT' AND a.status = 'READY')
        )
    )
);

-- ---------------------------------------------------------------------------
-- Enrollment snapshot guard: the activated audience, or a READY supplement
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.app_guard_enrollment_snapshot() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = ''
AS $function$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.campaigns c
       JOIN public.campaign_audience_members a ON a.workspace_id=c.workspace_id
         AND a.campaign_id=c.id AND a.audience_id=NEW.audience_id
       JOIN public.campaign_audiences au ON au.workspace_id=a.workspace_id
         AND au.campaign_id=a.campaign_id AND au.id=a.audience_id
       JOIN public.recipient_addresses r ON r.workspace_id=a.workspace_id AND r.id=a.address_id
       WHERE c.workspace_id=NEW.workspace_id AND c.id=NEW.campaign_id
         AND c.activated_sequence_id=NEW.sequence_id
         AND (
             c.activated_audience_id=NEW.audience_id
             OR (au.kind='SUPPLEMENT' AND au.status='READY'
                 AND c.activation_id IS NOT NULL AND c.status='PAUSED')
         )
         AND a.id=NEW.audience_member_id AND a.eligibility_status='ACCEPTED'
         AND NEW.frozen_destination=r.canonical_address AND NEW.frozen_variables=a.frozen_variables) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Enrollment must use the activated audience and frozen recipient snapshot';
    END IF;
    RETURN NEW;
END;
$function$;

DO $postcondition$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'campaign_audiences_kind_check'
          AND conrelid = 'public.campaign_audiences'::pg_catalog.regclass
    ) THEN
        RAISE EXCEPTION '0043 postcondition failed: kind CHECK missing';
    END IF;
    IF pg_catalog.has_column_privilege('app_api', 'public.campaign_audiences', 'kind', 'UPDATE') THEN
        RAISE EXCEPTION '0043 postcondition failed: app_api can UPDATE campaign_audiences.kind';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc p
         WHERE p.oid = 'public.app_guard_enrollment_snapshot()'::pg_catalog.regprocedure AND p.prosecdef
    ) THEN
        RAISE EXCEPTION '0043 postcondition failed: app_guard_enrollment_snapshot became SECURITY DEFINER';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM pg_catalog.pg_indexes
         WHERE schemaname = 'public'
           AND indexname IN ('campaign_planning_jobs_activation_identity_idx',
                             'campaign_planning_jobs_supplement_identity_idx')) <> 2 THEN
        RAISE EXCEPTION '0043 postcondition failed: planning job identity indexes missing';
    END IF;
END;
$postcondition$;

COMMIT;
