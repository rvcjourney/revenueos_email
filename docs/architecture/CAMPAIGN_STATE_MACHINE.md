# Campaign State Machine

## Approved migration review decisions

No reopening COMPLETED/ARCHIVED; no activated audience/content mutation; RUNNING requires pause before archive. Exact role grants now live in USER_ROLES.md. Command transactions remain backend-owned.

These decisions supersede corresponding proposals/Open Decisions below. Other release-policy decisions remain open.

**Amended by [ADR-0020](../adr/0020-pause-resume-paused-edits-and-late-enrollment.md):** pause and resume are idempotent (a repeat of an already-achieved state returns it unchanged), resume revalidates and fences unsent work with a `schedule_generation` bump, and PAUSED campaigns accept the edits listed in the ADR. COMPLETED/ARCHIVED campaigns still cannot resume.


## Purpose and authority

This is the sole campaign transition specification. It refines [SYSTEM_ARCHITECTURE](SYSTEM_ARCHITECTURE.md), [MVP §19](../product/MVP.md), and [existing UF-18–UF-20, UF-30, UF-35](../product/USER_FLOWS.md). The seven names come from PROJECT_CONTEXT §9; its §30 `ACTIVE` pseudocode is interpreted as RUNNING, not an eighth persisted state. This terminology inconsistency is explicitly recorded here.

This documentation phase specifies technical transitions; product choices flagged under Open Decisions require review. [CAMPAIGN_ENGINE](CAMPAIGN_ENGINE.md) owns planning/enrollment and edit semantics. Message execution is independently governed by [MESSAGE_STATE_MACHINE](MESSAGE_STATE_MACHINE.md).

## States and permitted operations

| State | Meaning and entry requirement | Allowed operations | Prohibited operations / exit behavior |
|---|---|---|---|
| DRAFT | Not activated; incomplete configuration is valid. | Read, edit, select/materialize audience, review, duplicate, activate, archive. | No send. Activation validates complete current revision. A never-activated campaign may be permanently deleted after archiving or while DRAFT ([ADR-0015](../adr/0015-archive-purge-and-erasure.md)). |
| SCHEDULED | Activation committed with a future start; immutable activation exists. Planning may be pending. | Read, duplicate, pause, archive; system start at/after start time. | No send before RUNNING. No content/audience edits. No second activation. |
| RUNNING | Execution permitted subject to all per-message gates and planning READY. | Read, metadata edit, pause, duplicate; system completion/failure. | No direct content/audience edit, direct archive, arbitrary status update, or bypass of holds. |
| PAUSED | Explicit suspension preserving progress and original start lower bound. | Read, permitted settings edits, resume after revalidation, archive, duplicate. | No new send authorization. Existing attempts finish/reconcile. |
| ERROR | Campaign-level invariant/planning failure requiring correction; failure reason and prior state retained. | Read, diagnose, authorized repair of allowed configuration, pause, recover, archive, duplicate. | No automatic blind restart. Transient mailbox/Redis outages alone do not imply ERROR. |
| COMPLETED | Planning READY; all enrollments terminal and no in-flight/unknown work remains. | Read, duplicate, archive; receive late outcome events. | No resume, new recipients, content edit or new sends. Terminal for execution. |
| ARCHIVED | Removed from ordinary active views; previous state/history retained. | Historical reads, duplicate, late event processing. | No restore-to-active or new sends. Terminal; not synonymous with deletion. An ARCHIVED campaign that was activated keeps its rows and counts and may have its recipients' personal data erased; one that never activated may be purged ([ADR-0015](../adr/0015-archive-purge-and-erasure.md)). Neither is a lifecycle transition. |

## Complete transition matrix

Rows are current state, columns requested next state. `—` means forbidden. `=` is an idempotent repeated command/read of an already-achieved state, never another planning pass. A replay with conflicting payload still fails.

| From / To | DRAFT | SCHEDULED | RUNNING | PAUSED | ERROR | COMPLETED | ARCHIVED |
|---|---|---|---|---|---|---|---|
| DRAFT | = | Activate future (U) | Activate now (U) | — | — | — | Archive (U) |
| SCHEDULED | — | = | Due start (S) | Pause (U) | Fatal preparation (F) | — | Archive (U) |
| RUNNING | — | — | = | Pause (U) | Fatal execution (F) | Complete (S) | — |
| PAUSED | — | Resume future (U) | Resume now (U) | = | — | — | Archive (U) |
| ERROR | — | Recover future (U) | Recover now (U) | Pause (U) | = | — | Archive (U) |
| COMPLETED | — | — | — | — | — | = | Archive (U) |
| ARCHIVED | — | — | — | — | — | — | = |

U = permission-checked user/domain command; S = system transition; F = classified campaign-level failure. Platform intervention uses a separate durable sending restriction, not an arbitrary campaign-state write. System actor identity and reason are required in audit data.

## Guards and effects

**Activate:** DRAFT, expected revision, current permission, complete audience capture, valid nonempty sequence, usable same-workspace senders, valid timezone/windows, no workspace restriction, and initial eligible recipients. Commit activation, state, planning job, event/outbox and audit atomically. Invalid preflight leaves DRAFT with validation response, not ERROR.

**Due start** (implemented by the scheduler loop, ADR-0020): SCHEDULED, database time at/after scheduled start, still authorized workspace execution and ready configuration. The UPDATE is guarded on `status = 'SCHEDULED' AND start_at <= transaction_timestamp()`, so a pause that committed first wins; it writes a `campaign.start` audit row and dispatches the reconcile pass when the generation is above 1. Planning may still be finishing; RUNNING alone does not permit sends until planning READY. Temporary dependency unavailability retains SCHEDULED with visible hold. Fatal configuration/invariant failure enters ERROR.

**Pause:** locks the campaign gate exclusively. Once committed, no new attempt may authorize against the prior status/version. Already SENDING or UNKNOWN_OUTCOME attempts remain tracked and may later become SENT. Pause response includes outstanding attempt count; it never promises recall. Pausing before initial start preserves that start time.

**Resume/recover:** revalidate current configuration, mailbox availability, safety restrictions and schedule; choose SCHEDULED if original future start has not arrived, otherwise RUNNING. Keep enrollment/message identities and retry budgets. Recovery records the corrected cause and actor; it cannot clear suppression, unknown attempts or duplicate planning. Invalid recovery leaves the previous state with actionable reason.

**Complete:** serialized campaign/enrollment check from engine. No future waits, safe retries, blocked ACTIVE enrollments, pending planning or unresolved attempts may remain. PAUSED/ERROR must resume/recover before completion is evaluated. Completion means no remaining execution, not all deliveries/replies are final.

**Archive:** permitted inactive state; write archive event and invalidate not-yet-authorized work. Bulk cancellation runs in batches but campaign gate prevents sends immediately. In-flight/unknown work retains reconciliation; archival never declares it unsent. RUNNING must first pause explicitly. Archive while a scheduled planner is running fences its future chunks.

## Concurrency, idempotency and interfaces

Frontend invokes domain commands with expected version and idempotency key. It cannot set status. Compare version under lock; racing start/pause calls serialize. Pause against DRAFT is rejected even if a start happens later. If start commits first, pause may succeed against the new version after refresh. Response loss is recovered by command identity, not submitting a new activation.

Command audit, transition event, revision increment and required outbox row share one transaction. Same-state commands produce no duplicate transition event. Database rollback leaves original state. Scheduler/system transitions also compare state/version; stale due-start tasks cannot resume a PAUSED campaign. IDs and capability checks always include workspace ownership.

## Failure/recovery and observability

Process crash or deployment resumes from durable campaign/planning state; Redis restart does not reset status. Lost planning/completion tasks are recovered by the outbox and periodic sweep. Provider outage holds relevant messages; timeout ambiguity blocks completion. A database outage stops transitions and pre-send authorization. Concurrent reply/suppression affects recipient eligibility independently of campaign status. Stale analytics never drives completion.

Record from/to state, version, actor kind/ID, command ID, campaign/workspace ID, reason and timestamp. Monitor age in ERROR, planning lag, paused outstanding attempts and completion-check lag. User activity shows actionable reasons without stack traces or message bodies.

## Open Decisions

Recommended product policy is: no reopening COMPLETED/ARCHIVED, no activated audience/content mutation, and pause-before-archive for RUNNING. The source flows explicitly leave these choices unresolved; owner approval is required before implementing those commands. Missing RBAC matrix must be restored before granting any U action. Permanent deletion and erasure are decided in [ADR-0015](../adr/0015-archive-purge-and-erasure.md): purge for never-activated campaigns, in-place redaction for activated ones, neither represented as a lifecycle transition. No automatic retention expiry is approved.

## Testing requirements and definition of done

Test every matrix cell, permission denial and cross-tenant command; duplicate activation with same/different command keys; stale version and reversed concurrent command ordering; scheduler start racing pause; pause racing send authorization; resume after disconnect/suppression; completion with future wait/retry/unknown attempt; archive during planning; late reply after completion/archive; and command response loss/database rollback. Implementation is complete only when domain/API constraints and UI action availability agree with this matrix and product Open Decisions are resolved.
