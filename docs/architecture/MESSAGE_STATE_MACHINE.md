# Message State Machine

## Purpose and invariants

This document owns one outbound message intent's execution lifecycle. It refines [SYSTEM_ARCHITECTURE §§33–35, 107, 118](SYSTEM_ARCHITECTURE.md) and [MVP §§22–24](../product/MVP.md). [CAMPAIGN_ENGINE](CAMPAIGN_ENGINE.md) owns recipient progression. A message has one stable ID and one enrollment/email-step identity; a retry creates an attempt, not another message.

Invariant: only a newly authorized SENDING attempt can invoke the provider. SENT means provider submission accepted, not recipient delivery. Delivery, bounce, open, click, reply, unsubscribe and complaint are separate events/facts; none resets SENT to unsent. Do not promise exactly-once external delivery where provider guarantees do not support it.

### Controlled-send foundation

MVP §38 requires one controlled Gmail send before campaign-scale implementation. A message therefore has purpose CAMPAIGN or CONTROLLED_TEST. CONTROLLED_TEST has an authorized requester, explicit test-recipient approval, mailbox/address, immutable content and command identity; it has no fake campaign/enrollment/sequence. The same execution states, attempts, suppression, workspace/mailbox gates, rate limits and unknown-outcome rules apply. Replace campaign/enrollment/window gates only with current requester permission, unrevoked test authorization and its bounded execution window. No sequence progression follows test acceptance. Test requests remain asynchronous durable work and never become a bulk-send API loophole. Scheduler dispatches them through the same message service; uniqueness is test command identity rather than enrollment/step.

## States

| State | Meaning / persisted requirements | Terminal? |
|---|---|---|
| PLANNED | Durable intent exists; render/due calculation incomplete. | No |
| SCHEDULED | Content snapshot complete; eligible no earlier than `due_at`; may have a temporary hold reason. | No |
| QUEUED | Database dispatch generation committed with outbox. Broker receipt is not implied. Claim expires at a bounded dispatch lease. | No |
| SENDING | Durable attempt authorized and committed; only owning invocation may perform external I/O within its authorization deadline. | No; never lease-reset into a sendable state blindly. |
| RETRY_SCHEDULED | Definitively rejected/proven-not-invoked attempt, bounded retry budget remains; `due_at` and reason persisted. | No |
| UNKNOWN_OUTCOME | Invocation may have been accepted; evidence insufficient. Reconciliation only. | No; blocks recipient progression/completion. |
| SENT | Provider acceptance evidence committed with accepted time. | Yes for execution |
| FAILED | Definitive nonacceptance with permanent error or exhausted safe retry budget. | Yes |
| SKIPPED | No unresolved invocation; recipient safety/eligibility prevents sending. | Yes |
| CANCELLED | No unresolved invocation; campaign archive or permitted removal invalidated intent. | Yes |

`TEMPORARY_FAILURE` is an attempt result/event, not a separately committed message state: record rejection and RETRY_SCHEDULED in one transaction. Campaign pause, closed window and mailbox disconnection are hold reasons, not terminal cancellation.

## Complete transition relation

This adjacency matrix enumerates every allowed edge. All unlisted edges are forbidden, including terminal-to-sendable transitions. Repeated observation of the same state/effect is a no-op with evidence deduplication.

| From | Allowed destination | Actor and mandatory guard |
|---|---|---|
| PLANNED | SCHEDULED | Planner: validated immutable rendering and valid due time. |
| PLANNED | SKIPPED / CANCELLED / FAILED | Domain service: safety stop / explicit invalidation / unrecoverable render error. |
| SCHEDULED | QUEUED | Scheduler: due, eligible campaign, valid dispatch generation, atomic outbox. |
| SCHEDULED | SKIPPED / CANCELLED / FAILED | Domain: current safety / explicit invalidation / definitive permanent preparation failure. |
| RETRY_SCHEDULED | QUEUED | Scheduler: due, retry evidence/budget still valid and all eligibility checks. |
| RETRY_SCHEDULED | SKIPPED / CANCELLED / FAILED | Domain: new stop / invalidation / exhausted or permanent safe failure. |
| QUEUED | SENDING | Send service: generation matches, exclusive claim, full current eligibility and capacity, attempt committed. |
| QUEUED | SCHEDULED / RETRY_SCHEDULED | Scheduler/worker: no attempt authorized, hold or expired dispatch; restore remembered dispatch origin. |
| QUEUED | SKIPPED / CANCELLED / FAILED | Send service: no unresolved attempt; safety / invalidation / permanent validation failure. |
| SENDING | SENT | Result service: positive acceptance evidence for this exact attempt. |
| SENDING | RETRY_SCHEDULED / FAILED | Result service: definitive nonacceptance or conclusive proof no invocation; policy permits retry / does not. |
| SENDING | UNKNOWN_OUTCOME | Result/reconciler: timeout, process loss, expired ownership or uncertain result. |
| SENDING | SKIPPED / CANCELLED | Owner only: conclusive evidence invocation did not occur and a stop/invalidation won before invocation; retain attempt as NOT_INVOKED. |
| UNKNOWN_OUTCOME | SENT | Reconciler: authoritative positive evidence matches exact intended message. |
| UNKNOWN_OUTCOME | RETRY_SCHEDULED / FAILED | Reconciler: definitive evidence of nonacceptance AND original invoker cannot still invoke; retry policy permits / rejects. |
| UNKNOWN_OUTCOME | SKIPPED / CANCELLED | Reconciler: same nonacceptance/quiescence proof plus new safety stop / explicit invalidation. |
| SENT / FAILED / SKIPPED / CANCELLED | None | Retain history. Late events attach outcomes; no restart. |

Rescheduling while SCHEDULED/RETRY_SCHEDULED updates due time, hold and schedule generation under version check without creating an execution transition. A retry dispatched into QUEUED records its previous state so expiry never resets attempt count.

## Authorization and external boundary

1. Worker loads message by ID and workspace, validates task schema/generation, and resolves protected provider configuration. Any slow credential refresh occurs before final authorization; refreshed generation must be rechecked.
2. Obtain short-lived atomic capacity reservation per [RATE_LIMITING](RATE_LIMITING.md). Unused tokens may conservatively expire without refund.
3. In a bounded transaction acquire the ordered gates specified by [CAMPAIGN_ENGINE](CAMPAIGN_ENGINE.md). Verify current workspace restrictions and pending safety holds, RUNNING campaign and planning READY, ACTIVE enrollment, target validity, suppression and reply facts, healthy connected mailbox/current credential generation, required sync freshness and no mailbox safety hold, current window, due time, unexpired capacity reservation and no other unresolved attempt. For CONTROLLED_TEST substitute the explicit authorization gates defined above. Fence stale queue generation.
4. Persist attempt ID, ordinal, content digest, provider identity, invocation owner, authorization deadline and durable capacity debit; transition QUEUED → SENDING. Commit. This is the **send authorization linearization point**.
5. Owning worker checks deadline immediately before calling provider. Never intentionally invoke after expiry or after known revocation. Invoke once with explicit timeouts and library automatic send retries disabled. Do not retain database locks across network I/O.
6. Commit result/evidence and state, outcome event, capacity settlement and any next-recipient progression in one transaction. On database failure after acceptance, retry only recording the known result; never repeat the provider call.

Pause, reply, suppression and platform restriction commands use the same gates. If their authoritative transaction commits first, authorization must fail. If authorization commits first, the one in-flight request may complete, even if the worker has not yet entered the network call. This is an explicit, bounded product promise, not a guarantee that a provider can recall an email. A dead process cannot be fenced by a database token at a provider that lacks fencing. Therefore lease expiry never grants a new send attempt until uncertainty and old-invoker quiescence are resolved.

## Attempt evidence and reconciliation

Each attempt records PREPARED at authorization, then ACCEPTED, REJECTED, UNKNOWN or NOT_INVOKED evidence. PREPARED does not prove a provider call occurred. Process death between commit and call is indistinguishable from death after acceptance unless additional evidence exists; treat it as unknown. At most one unresolved attempt is permitted per message.

Positive evidence may be a successful provider response or a uniquely matched sent-item/message lookup. Store provider request/message/thread identifiers separately from RFC Message-ID; providers may not return all of them. Native idempotency is used only if its exact scope/expiry/content behavior is verified by adapter tests. A reused RFC Message-ID alone is not an idempotency guarantee.

An empty sent-folder search, missing webhook, elapsed time, HTTP disconnect or SMTP socket error after submission is not proof of nonacceptance. Reconciliation polls with bounded frequency and escalates after an approved age, preserving UNKNOWN_OUTCOME. A reviewer may attach evidence and restrict/close investigation workflow, but cannot falsely mark FAILED to enable retry. Unresolved messages are held indefinitely rather than silently resent. Manual “resend despite uncertainty” is not part of MVP.

**Late provider results (implemented, `SendingRepository.finalize_attempt_result`).** A send worker's result is applied only while the message is SENDING and *its* attempt is still PREPARED for the message's current dispatch generation; the attempt row, then the message row, are locked first (the order recovery's updates take them), so a concurrent recovery and finalize serialize. One late result is still applied: if recovery already moved this attempt to UNKNOWN and the message to UNKNOWN_OUTCOME and the late result is an acceptance, the message becomes SENT and the attempt ACCEPTED exactly as on the normal path (audited `message.sent`, reason `accepted_after_recovery`). Every other late result — after reconciliation decided the message, a late rejection/ambiguous/not-invoked result after recovery, or any other state — changes neither the message nor the attempt: it is appended to `attempt_evidence` (`source_kind = late_provider_result`, classification = the evidence state it reported) and audited as `message.late_provider_result`; it never schedules a retry. Separately, the Microsoft Graph adapter does not start `/send` when less than one request's worst-case timeout (20 s) remains before `authorization_deadline`: it deletes the draft and returns a definitive TEMPORARY rejection (`send_deadline_budget`), which the normal retry policy handles.

Late positive response for the current unresolved attempt can record SENT even after campaign pause/archive or recipient stop; history must reflect reality. It must not plan a follow-up for a now-stopped recipient. Contradictory positive evidence after an allegedly definitive terminal rejection is an integrity incident: append evidence, block the enrollment, alert, and investigate through a reviewed correction command, not a blind retry.

## Retry and holds

Retry only definitive retryable rejection or proven non-invocation. Persist next retry as the maximum of backoff+jitter lower bound, provider Retry-After/cooldown, sequence lower bound and next allowed window. Limit both attempts and elapsed retry horizon through versioned policy. Capacity deferral, queue expiry and campaign pause do not consume provider-attempt budget. Persist the chosen jittered time so duplicate tasks do not reroll it.

Mailbox AUTH_FAILURE sets reconnect required and holds unsent work; an individual policy rejection may fail the message without suppressing the recipient. Recipient permanent bounce/complaint invokes the shared suppression service. A post-acceptance soft bounce is an event; the original accepted email is never automatically resent by this state machine.

## Failure analysis

| Failure | Safe action |
|---|---|
| Worker crash before authorization transaction | Queue claim eventually expires; requeue from durable intent. |
| Crash after SENDING commit, including before network call | UNKNOWN_OUTCOME; reconcile, no lease-based resend. |
| Provider accepted; local result commit failed | Persist known acceptance on recovery or reconcile; never call send again. |
| Provider timeout/outage with uncertain acceptance | UNKNOWN_OUTCOME. Other definitively unsubmitted work may defer. |
| Duplicate task/late stale worker | Generation/state/attempt checks deny invocation; late evidence may be recorded. |
| Redis flush / task loss | Recover unattempted dispatch from PostgreSQL; reconstruct capacity before allowing sends. |
| Database outage | No new send authorization; in-flight requests remain potential unknowns. |
| Deployment / scheduler restart | Recover by state; preserve task schema compatibility and unresolved attempts. |
| Concurrent pause/reply/unsubscribe/stale plan | Serialized final gate determines permitted effect; recorded safety fact always blocks subsequent authorization. |

## Security, visibility and tests

Only messages application services transition states. Tenant context and composite ownership links protect all attempts and evidence. Secrets, bodies and address data stay out of task payloads/logs. Log message/attempt/campaign/mailbox IDs, transition, safe error category, dispatch generation and correlation. Track oldest unknown, acceptance persistence errors, duplicate-denial count, holds, retry exhaustion and send authorization-to-invocation delay. Alert on any unauthorized duplicate or stale invocation.

Tests must fault-inject every boundary above, including crash before/after provider acceptance, database commit ambiguity, timeout with later sent-item evidence, negative lookup without resend, two consumers of one dispatch, expired lease while worker is alive, archive before late acceptance, new suppression before retry, missed window, revoked mailbox generation, Redis-loss capacity recovery and cross-tenant attempt access. Verify all unlisted state transitions fail and events cannot reset SENT.

## Open Decisions

Approve retry budgets, authorization/claim timeouts, reconciliation age/alert targets and the bounded in-flight pause/stop promise before release. Technical safety does not depend on their exact numerical values. Provider-specific reconciliation coverage is a release gate, especially SMTP's inability to prove nonacceptance from a missing sent copy. This document intentionally offers no unsafe “assume failed after N minutes” default.

Related: [providers](PROVIDER_ARCHITECTURE.md), [scheduler](SCHEDULER.md), [events](EVENT_SYSTEM.md), [suppression](SUPPRESSION.md), [database](../database/DATABASE.md).
