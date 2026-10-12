# Pause/resume hardening, edits while paused and late enrollment

## Status

Accepted by the product owner on 2026-10-08 (decisions below were chosen explicitly). Migrations 0040, 0041 (guard-function ownership repair), 0042, 0043, 0044, 0045 and 0046 are prepared and must be reviewed and applied by the project owner; this ADR does not apply them. The follow-up amendments (production-readiness review, 2026-10-09) are marked *Amendment* below.

This ADR **supersedes** the "activation freezes audience/content" and "only PAUSED activated campaigns may change schedule/limits" decisions in the headers of [CAMPAIGN_ENGINE](../architecture/CAMPAIGN_ENGINE.md) and [CAMPAIGN_STATE_MACHINE](../architecture/CAMPAIGN_STATE_MACHINE.md), for the PAUSED state only. RUNNING, SCHEDULED, COMPLETED and ARCHIVED campaigns stay immutable.

## Context

Pause and resume already exist (`CampaignActivationService.pause/resume`), and the send path already refuses to send for a campaign that is not RUNNING (scheduler discovery and claim, send gates, progression, just-in-time personalization). What was missing:

1. Resume did no revalidation and did not recompute work that came due during the pause, so a long pause released a burst on resume.
2. Pause and resume were not idempotent (a repeat returned 409), gave no count of in-flight attempts and kept no pause metadata.
3. Nothing could be edited while paused: the settings API accepted only DRAFT although the database already allowed PAUSED settings versions, and sequence steps, attachments and the audience are frozen by triggers.
4. `campaigns.schedule_generation` exists and is compared by the scheduler, but nothing ever changes it, and three code paths do not stamp it on the messages they create.

## Decision

### Wait rule

**Waits are elapsed real time and keep running while the campaign is paused.** A follow-up is due `accepted_at + wait` as before. On resume, work that became overdue is spread by the existing window, pacing and mailbox-cap machinery instead of becoming due together, and **nothing is ever sent earlier than its wait**. `anchor_at` and every historical timestamp are never rewritten. Freezing the clock was rejected: it needs a pause-interval history and pause-aware anchors in the progression sweeper, and the extra moving parts are a risk to a feature whose first requirement is never to duplicate or skip an email.

### Idempotent state machine

Pause and resume lock the campaign row, then: already in the target state → return it unchanged (no second audit row, no second generation bump); illegal state → 409 `state_conflict`; stale `expected_version` → 409 `conflict`; else transition. Pause is allowed from RUNNING and SCHEDULED, resume from PAUSED only. Commands carry no separate receipt: status plus version already make them idempotent.

### Resume fence

`campaigns.schedule_generation` is the resume fence. Resume validates, then in one UPDATE sets the status and increments the generation. The scheduler only claims messages whose `schedule_generation` equals the campaign's, so after a resume **no pre-existing unsent message can be claimed until a reconcile pass has re-validated and re-stamped it**. Losing the reconcile task therefore fails closed (nothing sends) and a scheduler sweep re-dispatches it. The send worker additionally refuses a message whose generation is stale and hands it back (existing `defer_for_capacity` mechanism) so a message claimed before the pause cannot send stale content or stale timing. New messages stamp the campaign's current generation.

The reconcile pass (`campaigns.reconcile_resume_chunk`) is idempotent and level-triggered. Per unsent SCHEDULED/RETRY_SCHEDULED message it re-renders the snapshot when the step content changed (only if no attempt could have reached the provider), recomputes `due_at` (never earlier than `anchor + wait`, never before `next_retry_at`, spread per mailbox when overdue) and stamps the new generation. QUEUED, SENDING and UNKNOWN_OUTCOME messages are never touched.

### What may change while PAUSED

Requires `campaigns.execute` and an activated, PAUSED campaign.

- Name, description, sending windows, timezone and daily limit (a new immutable settings version; `start_at` is unchanged).
- Subject, body, preheader and attachments of any email step, and wait durations. These apply only to emails that have not been sent; sent and transport-pinned snapshots are never changed. Standard campaigns.
- *Amendment (migration 0044; waits, structure and written steps superseded by 0046 below): personalized (HYPER_PERSONALIZED) campaigns.* Each lead's email is written from its step's **reference email** just before sending. While paused a manager may change the *wording* (subject, body, pre-header) of a reference only for a step **none of whose emails has been written** (`messages.rendered_at IS NULL` for every message of the step). A step with even one written email, every wait, step kind and position, the set of steps and all attachments stay frozen, and the setup (objective, company, email style) is read-only. The service and the `app_guard_frozen_sequence_steps` trigger enforce the same rule. A generation that read the old reference before the edit and finishes after a resume is discarded without spending its budget (the finalize step compares `sequence_steps.version`) and written again from the new reference.
- *Amendment 2 (migration 0046, product decision 2026-10-12): a personalized campaign is edited like a standard one.* While paused a manager may change the wording of **any** step (also one that already has written emails), any **wait**, and add, remove, copy or reorder steps **after the frontier**; the same frontier rule, step-kind rule and service/trigger agreement as standard campaigns. What stays frozen: emails already written (the message snapshot guard), steps leads have reached (place and kind), attachments (a step that carries some can't be removed or copied), the setup (objective, company, email style) and inline images in a reference. A personalized email is written from its step's *current* reference just before it is sent, so wording changes reach every email written after the resume; emails already written keep the version they were written with. Approval of sample emails is **not** re-asked for an edit made after launch, and a step added while paused has never been sampled. Sample previews of a step are throw-away rows and are deleted with it (the foreign key now cascades). *Waits:* a planned personalized follow-up is a `PLANNED` message plus a generation job whose `next_attempt_at` (due time minus the lead time) is fixed when it is planned, and the writer only looks at a job once that time arrives. So an edited wait would not reach a follow-up that is days away unless the resume moves it. The post-resume reconcile task therefore has a second phase (`RetimeService`, `campaigns/retime.py`): for a personalized campaign RUNNING at that generation it selects the jobs nobody has started (no attempt, no live lease) whose message is planned and unwritten and which have a WAIT step before their step updated after the job was last updated, and moves both the message's `due_at` and the job's `next_attempt_at` to what the wait implies now, earlier or later. One rule (`campaigns/follow_up_timing.py`): if a wait was edited after the follow-up was planned the new wait decides; otherwise the planned time stands (it may be later than the wait alone implies because overdue follow-ups are spread) and is never earlier than the wait implies. The writer applies the same rule when it finishes an email and, as a backstop, holds back a claim that beat the re-time when the new time is later (no model call spent). Follow-ups that are already written (`SCHEDULED`) are re-timed by the ordinary reconcile pass. Processed jobs have `updated_at` bumped, so a replayed or duplicated task converges. A lost dispatch leaves the old timing (the previous behaviour, not unsafe); pausing and resuming again repeats the pass. There is no scheduler recovery sweep for this: the scheduler role cannot read `sequence_steps`. Not covered: spreading overdue *planned* personalized follow-ups after a long pause (writing is paced by the model rate limit and sending by the mailbox limits, as before), and giving finished leads an appended step (opt-in reopen is standard campaigns only).
- Steps: append at the end, or add/remove/reorder only steps after the **frontier**, the highest step position any message or enrollment pointer has reached (standard campaigns). Enrollments already COMPLETED are terminal **unless the user opts in at resume** (below).
- The sender set and the activated audience are unchanged.

### Finished leads (opt-in at resume)

*Amendment.* A lead that finished the sequence before the pause is not contacted by a step appended during it, **unless** the user ticks "also send the new emails to leads who already finished the sequence" in the resume dialog (`ResumeIn.include_completed`, default false, standard campaigns, resuming straight to RUNNING). The API dispatches `campaigns.reopen_completed_enrollments_chunk` bound to the resume's generation; a worker puts each qualifying COMPLETED enrollment back to ACTIVE pointing at the last email it was sent, and the normal progression sweep then plans the appended email after its wait, spread like any overdue follow-up. Qualifying leads: the last email they were sent is SENT, a later EMAIL step exists, a mailbox is assigned, no `recipient_outcomes` row exists (no reply, unsubscribe, bounce or complaint) and the address is not suppressed. The reopen is guarded on `state = COMPLETED` (replay-safe), does nothing once the campaign is no longer RUNNING at that generation, and is **not persisted**: a lost dispatch reopens nothing, which is the safe outcome. The request is audited (`campaign.reopen_completed_requested`); per-chunk counts are logged.

### Late enrollment

New leads are added as a separate **supplement** audience captured with the existing capture machinery, minus leads or addresses already enrolled in the campaign. The READY activated audience is never modified. Enrollment uses the existing planner with a supplement job; the enrollment guard accepts the activated audience or a READY supplement audience of the same campaign. Resume is refused while a supplement job is unfinished.

*Amendment: planning-job recovery (migration 0045).* An ENROLL/RENDER planning job can fail or lose its task message. A manager can **retry** a FAILED job (`POST …/audience/supplements/{id}/retry`, `POST …/planning/retry`): the worker task `campaigns.retry_planning_job` puts it back to PENDING keeping its cursor, and the chunks are idempotent so already-enrolled leads are not enrolled twice. The scheduler also **re-dispatches** jobs nobody is working on (PENDING for over a minute, or PROCESSING with an expired lease) every minute; RENDER is only offered once its ENROLL is READY, and the RENDER task itself refuses to run before that. The scheduler role gains a read-only discovery policy on `campaign_planning_jobs` and no write privilege. Resume is **also refused while a planning job is FAILED** (`planning_failed`), so leads never silently miss their first email.

### Pause metadata and observability

`campaigns.paused_at`, `paused_total_seconds` and `last_resumed_at`. Audit rows carry before/after state. Log events `CAMPAIGN_PAUSED`, `CAMPAIGN_RESUMED`, `CAMPAIGN_EXECUTION_SKIPPED_PAUSED`, `CAMPAIGN_STEP_RESUMED`, `CAMPAIGN_RESUME_FAILED`; ids only, never content or addresses.

### Starting a scheduled campaign, and unhealthy mailboxes

*Amendment.* The scheduler now starts campaigns whose time has come: a SCHEDULED campaign with `start_at <= now` (database clock) becomes RUNNING in a stale-safe UPDATE (a pause that committed first wins), is audited as `campaign.start`, and dispatches the reconcile pass when its generation is above 1. Discovery and claim also skip messages whose mailbox cannot send (not connected, disabled, degraded, circuit open, safety hold) and the send worker hands such a message back for later instead of holding a 300-second claim.

### Overdue follow-ups after a pause

*Amendment.* The progression sweep places a follow-up that is already overdue (its `accepted_at + wait` is more than two sweep intervals in the past) in the next free slot of **its own mailbox** — within the mailbox daily cap and spacing, after what is already planned (hourly occupancy) — and never earlier than `accepted_at + wait`. The campaign row is locked `FOR NO KEY UPDATE` while slots are chosen, so overlapping sweeps and the reconcile pass never read the same free slot. The reconcile pass uses the same planner.

## Consequences

- In-flight attempts at the moment of a pause cannot be recalled; the pause response reports how many there are.
- A SCHEDULED campaign (future start) that is paused and resumed returns to SCHEDULED and is started by the scheduler when its time comes (amendment above).
- Deploy order: apply 0040 → 0041 → 0042 → 0043 → 0044 → 0045 → 0046 (0041 repairs the owner of the two frozen-sequence guards, which production was found to have as `postgres`; the preflight in `docs/operations/DEPLOYMENT.md` reports which are missing); deploy the worker (new tasks) before or with the API (resume bump); deploy the scheduler last.
- Pause is the emergency stop, so it must work even on a database that has not had 0040: it then changes only the status and the generation fence and skips the pause bookkeeping columns (checked once against the catalog and cached).
- Content edited while paused is not reflected in `campaign_sequences.content_digest`, which stays the activation record. Each edit is audited.
