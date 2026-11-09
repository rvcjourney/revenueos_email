# Campaign Engine

## Approved migration review decisions

Activation freezes audience/content. Captures persist their list/manual selection manifest before processing; list membership is fixed under capture gates, contact fields are sampled during bounded capture. Current settings reference an immutable version. Only PAUSED activated campaigns may change schedule/limits. No activated sender reassignment in this MVP. Completed/archived campaigns are duplicated for new outreach.

These decisions supersede corresponding proposals/Open Decisions below. Other release-policy decisions remain open.

**Amended by [ADR-0020](../adr/0020-pause-resume-paused-edits-and-late-enrollment.md) (PAUSED state only):** while a campaign is PAUSED, a user with `campaigns.execute` may change schedule/limits, unsent email content and waits (standard campaigns), step structure after the frontier, and may add leads through a supplement audience. Waits keep elapsing during a pause; resume bumps `schedule_generation`, and a reconcile pass re-validates and re-times unsent messages. All other states keep the immutability above.

## Purpose, ownership and scope

The backend campaigns module orchestrates authorized intent into recipient progress and durable message work. It implements the Email/Wait MVP in [MVP §§19–22](../product/MVP.md), existing [UF-13–UF-30 flows](../product/USER_FLOWS.md), and [SYSTEM_ARCHITECTURE §§31–35, 118–120](SYSTEM_ARCHITECTURE.md). See [DOMAIN_MODEL](DOMAIN_MODEL.md) for the source-file discrepancy and design status.

The engine owns configuration validation, audience capture, enrollment, sequence interpretation, activation, pause/resume, completion and recovery. It delegates transport to providers, due discovery to scheduler, sending authorization to messages, and address prohibitions to suppression. It does not implement Gmail conditionals, HTTP bulk sending, arbitrary workflows, analytics-dependent decisions or a second worker domain model.

## Configuration and snapshots

A draft has a revision, metadata, chosen audience revision, ordered Email/Wait sequence, selected workspace mailbox IDs, campaign timezone/windows, optional future start and configured limits. Required variables must have values or explicit authored fallbacks. Missing data is a review error, never an invented personalization value.

Draft content is a copy of a selected template version or direct authored content. Activation freezes sequence/content, audience revision and initial settings. The backend validates exactly the revision submitted by Review; a stale revision returns conflict. API command idempotency binds workspace, actor, operation, key and payload hash; reusing a key with another payload is rejected.

### Audience materialization

Selection produces a durable audience-capture job attached to the draft. Before capture begins, acquire the selected list rows in sorted order, increment their capture counters and commit a manifest of list IDs/revisions. List membership mutations lock the same list row and are deferred while a capture is active. Materialize candidate IDs and contact revisions in bounded chunks into a draft audience revision with unique campaign/revision/address identity. A lead edit after its capture does not change captured variables; capture timestamp is exposed. This deliberately defines membership as fixed at capture and contact data as sampled during capture, not a fictitious atomic snapshot of millions of contacts.

Completion marks audience revision READY and releases list capture counters in one transaction; failed/abandoned captures are made non-resumable and release counters through a recovery command before another capture starts. Explicit/manual selections use the same manifest and deduplication. Capture status is independent of campaign lifecycle. Activation cannot use a partial revision. A “refresh audience” creates a new revision and requires review; it does not silently refresh an activated campaign. This avoids long database snapshots and makes capture restarts recoverable, at the cost of briefly preventing edits to selected lists.

Filter invalid, archived or suppressed candidates and report exclusions; preserve counts/reasons without claiming final send eligibility. Reject activation if no eligible recipient remains at review. Later suppression may legitimately stop every enrollment and allow completion with zero sends.

## Activation and planning flow

1. Authenticate and authorize action against current membership (exact grant remains unresolved). Lock campaign, compare expected version, verify DRAFT, READY audience, valid content/schedule, workspace safety and selected mailbox ownership/capabilities.
2. In one transaction create activation identity, freeze revisions and write campaign SCHEDULED (future start) or RUNNING (immediate), activation event/audit, and planning outbox record. `planning_status=PENDING` prevents send claims until enrollment initialization is complete. Commit before returning success.
3. Planner locks activation job, processes captured recipients by stable cursor, and creates each enrollment and first PLANNED message once. Freeze target/variables and choose sender from authorized selected accounts. Insert chunk effects and checkpoint atomically.
4. When the manifest is exhausted, mark `planning_status=READY`. Message planner renders bounded work and projects due times into allowed windows. SCHEDULED campaign may be planned early, but cannot send until system start transition.
5. Scheduler dispatches due messages. Send result service records acceptance and calls recipient progression transactionally. Next-step creation and completion cannot depend on a separate lossy analytics event.

Planning can continue while PAUSED to finish recording intent; no message may send. Archive cancels remaining planning. ERROR preserves checkpoints. Retry uses activation and enrollment keys, never starts a second activation. Work proportional to audience size never occurs in the start HTTP transaction.

## Sequence interpretation and sender assignment

MVP validation accepts a nonempty alternating sequence beginning and ending with Email. Wait intervals are positive elapsed durations in minutes/hours/days (day = 24 elapsed hours); calendar-day and lead-local time interpretations are not implicitly supported. First email anchor is campaign start. Follow-up anchor is previous message's persisted provider-acceptance timestamp plus wait duration. Queue time and first-attempt time are not anchors. Project this lower bound into the next permitted sending window; enforce current limits again later.

Only the next email intent is materialized for an active enrollment. The immutable sequence plus persisted progress preserves all future intent without eagerly creating every follow-up. Record rendered subject/body, renderer version, variables, sender fields, signature and content digest before QUEUED. All attempts reuse that snapshot and stable RFC Message-ID where supported. Providers may transform transport; store observed provider identifiers separately.

Choose a mailbox deterministically from eligible selected mailboxes using persisted allocation position and stable recipient ordering. Persist the assignment; retries never reroll. After the first authorized send, pin that enrollment to the mailbox for thread continuity. A disconnected mailbox holds work; it does not trigger sender rotation on an unknown attempt. Before any attempt, reassignment may be an explicit paused command that invalidates rendering and queue generation. This command remains an Open Decision for product approval.

### Implementation notes (follow-up progression, pre-header, attachments)

- **Progression** is implemented as a level-triggered sweeper, not inside the send-result transaction — see [ADR-0009](../adr/0009-sequence-progression-sweeper.md). For each ACTIVE enrollment whose current email is SENT it creates the next email (anchor = persisted acceptance time + summed waits, projected into the window), renders it from the frozen variables, and moves `next_step_id`; the last accepted email completes the enrollment and a permanently FAILED email fails it. It is idempotent (unique `(enrollment,step)` message key, guarded pointer update) and disabled unless `SEQUENCE_PROGRESSION_ENABLED=true`. Campaign-level completion is not part of this change.
- **Pre-header** is stored on the step/template version and folded into the rendered body as a hidden preview-text element before the content digest is computed, so the existing digest and the send gate's recomputation cover it unchanged. Steps without one render and hash exactly as before.
- **Attachments and inline images** are described in [ADR-0010](../adr/0010-step-attachments-and-inline-images.md) and `campaign_step_attachments` in [DATABASE](../database/DATABASE.md). Explicit editor content always wins over a picked template's text; the template reference is provenance only.
- **Hyper-personalized campaigns (proposed, [ADR-0011](../adr/0011-hyper-personalized-campaign-type.md))** keep this engine and every state machine. `campaigns.campaign_type=HYPER_PERSONALIZED` changes only how a message's content snapshot is produced: the message is inserted PLANNED with an intended due time and a `message_generations` row, and becomes SCHEDULED when a validated snapshot is written (`renderer_version=2`, same digest formula) roughly one lead-time before it is due. Activation freezes the objective, reference templates and prompt version, not per-lead output; the per-lead snapshot is frozen before QUEUED as usual. Insufficient context renders the reference template (`renderer_version=1`); a failure after bounded attempts marks the message FAILED and it is never sent. Eligibility is rechecked in the finalizing transaction and by the unchanged send gates. See also [ADR-0012](../adr/0012-llm-port-data-handling-and-validation.md) and [ADR-0013](../adr/0013-research-sources-and-outbound-fetch.md). The reference emails themselves can be AI-drafted from the objective and a company profile ([ADR-0016](../adr/0016-api-process-ai-drafting-and-company-analysis.md)); for an HTML campaign both drafting and per-lead generation wrap content in one platform layout ([ADR-0017](../adr/0017-branded-email-layout-and-sanitizer-profile.md)) before the preheader and digest, so a preview is exactly what is sent. Both paths hold emails to the same craft rules and sequence playbook (structure, length, a question as the ask, a distinct job per follow-up; [ADR-0018](../adr/0018-email-craft-rules-and-sequence-playbook.md)).
- A step-level "start on Day N" in the editor edits the WAIT step before it (whole days), so the documented Email/Wait alternation is unchanged.

## Three independent lifecycles

Campaign status controls global execution; enrollment controls one recipient's sequence; message status controls one intended email. A provider outage or cooldown is usually a temporary hold, not a failed campaign. A bounce event after SENT does not turn acceptance into an unsent retry.

| Enrollment state | Entry and allowed progress | Terminal behavior |
|---|---|---|
| ACTIVE | Created once; progress points to next email or current intent; optional hold reason for pause/mailbox/uncertainty. | Nonterminal, even when no work can run now. |
| COMPLETED | Last email accepted, progression committed. | No additional sequence message. Late reply/suppression adds outcome facts without reopening. The only way back to ACTIVE is the **opt-in reopen at resume** (below): guarded `state = COMPLETED`, clean leads only, standard campaigns, bound to the resume's generation ([ADR-0020](../adr/0020-pause-resume-paused-edits-and-late-enrollment.md)). |
| STOPPED | Recognized reply, suppression, unsubscribe, complaint, archive or explicit permitted removal. Persist all observed reasons; safety facts are not overwritten. | Cancel/skip not-yet-authorized intent, retain sent/unknown attempts. Never auto-reactivate. |
| FAILED | Permanent execution failure or exhausted safe retry policy before sequence finished. | No next step. Explicit new campaign needed for a new outreach intent. |

Allowed state changes are ACTIVE to any terminal state; terminal to terminal changes are prohibited. Add later outcome facts separately. Replayed ACTIVE-progress updates must compare version/current step. Per-recipient manual pause is a future product decision, not implemented by inventing another state.

## Pause, resume, edits and completion

Use [CAMPAIGN_STATE_MACHINE](CAMPAIGN_STATE_MACHINE.md) for exact commands. Pause commits the global gate and invalidates future dispatch authorization; existing SENDING attempts retain their outcome. UI reports outstanding authorized attempts, because an external request cannot be recalled. Resume revalidates configuration, requires useful healthy sender capacity, increments schedule generation, and recalculates overdue work without catching up in a burst: unsent messages are re-timed by the reconcile pass and overdue follow-ups are placed by the progression sweep in the next free slot of their own mailbox (daily cap, spacing and already-planned load respected), never earlier than `accepted_at + wait`.

Recommended activated edit policy: metadata may change; content/audience remain immutable; PAUSED campaigns may change windows or limits with versioned settings (approved in ADR-0020), edit the wording of unsent emails and add leads; a personalized campaign follows the same rules (emails already written keep their text; its setup and attachments stay frozen). Raising limits still obeys platform/provider policy. Invalidate old queue generations and recompute only unsent intents. Never move an anchor earlier than its sequence wait; never rewrite historical due times or attempts. Completed/archived campaigns are duplicated into fresh drafts to send anew. Product approval is required for this policy; it is not silently imported from the broad context's “Edit” action.

Completion requires planning READY, every enrollment terminal, and no SENDING/UNKNOWN_OUTCOME message or unfinished progression transaction. A future SCHEDULED or RETRY_SCHEDULED message, mailbox hold or unresolved attempt prevents completion. Lock campaign and check completion after recipient updates; a periodic sweep repairs a missed completion check. Once COMPLETED, late replies/bounces still update inbox/safety/analytics, but do not reopen execution. PAUSED campaigns wait for resume or archive; do not surprise users with an automatic completion transition.

## Transaction and lock boundaries

Acquire relevant gates in a single order: platform sending gate (shared for send, exclusive for global intervention), rate-control readiness gate, workspace gate, address gate, campaign, enrollment, mailbox, message, then applicable rate-scope rows. Ordinary shared gates permit concurrent sends; mutation gates serialize only conflicting scopes. Acquire multiple records of a class in stable ID order. The exact address gate mechanism is owned by [SUPPRESSION](SUPPRESSION.md). Completion and progression follow campaign-before-enrollment order. No provider request occurs while these database locks are held.

Transaction units: activation + outbox; enrollment chunk + cursor; rendered message + schedule; send authorization + attempt; result + progression + next message/outbox; reply match + stop state + event; suppression + event. Outbox relay and provider invocation are outside those transactions. Pre-send authorization is the linearization point described in [MESSAGE_STATE_MACHINE](MESSAGE_STATE_MACHINE.md); pause/unsubscribe committed earlier must win.

## Failure, recovery and observability

| Failure | Required recovery |
|---|---|
| Planner process crash / duplicate task | Resume committed manifest cursor, unique enrollment/message keys prevent duplication. |
| Lost task / Redis restart / publish failure | Outbox relay plus durable job sweep republishes; worker rechecks state. Planning jobs (enroll/render) are covered by the scheduler's planning-job sweep (PENDING for over a minute, or PROCESSING with an expired lease; RENDER only after its ENROLL is READY) and by an explicit retry of a FAILED job; resume is refused while a job is FAILED. A lost post-resume reconcile or reopen dispatch fails closed (nothing sends / nothing reopens); the reconcile is re-dispatched by the stale-generation sweep. |
| Database temporary failure | Roll back chunk/command, do not send; replay original command identity. |
| Provider timeout / send worker crash | Hold affected enrollment until attempt reconciliation; never plan a follow-up from uncertain acceptance. |
| Provider outage | Backoff/cooldown mailbox/provider; retain ACTIVE holds and alert. |
| Scheduler restart / deployment | Recompute due work using current schedule generation; compatible payload versions. |
| Concurrent start/pause/edit or stale UI | Lock/version check gives one serial order; conflicting command returns current state. |
| Reply or suppression races with planning | Shared recipient/safety gate and authoritative pre-send check override the snapshot. |

Log workspace/campaign/activation/enrollment/message/attempt/correlation IDs and safe reason codes. Measure planning age, active holds, completion lag, unknown outcomes and excluded recipients. Alert on stuck planning, unknown attempts, persistent mailbox holds and event-processing lag. Never log recipient variables or content. UI activity derives from durable transitions, not log parsing.

## Open Decisions and implementation gate

Approve RBAC repair; activated edit/reopen policy; audience capture's brief list-edit lock and contact sampling semantics; sender reassignment; exact retry/hold alert thresholds; schedule/normalization defaults. Recommended defaults above are fully specified for review, not already approved product behavior. No arbitrary cross-campaign stop-on-reply option is added: recognized reply stops its matched enrollment; workspace-wide suppression remains separate.

Implementation must test duplicate starts with different request keys, crash after each planning chunk, concurrent list edit/capture recovery, frozen templates, exactly one next intent per accepted step, pause/send and reply/send ordering, suppressed-after-planning recipients, no completion while future/unknown work exists, cross-tenant mailboxes/lists, Redis flush and deployment recovery. Review [scheduler](SCHEDULER.md), [events](EVENT_SYSTEM.md), [reply sync](REPLY_SYNC.md), [database](../database/DATABASE.md) with this document before implementation.
