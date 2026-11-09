# Rate Limiting

## Purpose and scope

The backend capacity service decides whether an otherwise eligible operation can consume sending/provider capacity now. It enforces [PROJECT_CONTEXT §§109–124](../product/PROJECT_CONTEXT.md), [MVP §23](../product/MVP.md) and [SYSTEM_ARCHITECTURE §§45–46, 76](SYSTEM_ARCHITECTURE.md). It does not decide suppression or replace message state. No limiter is designed to evade provider restrictions.

API abuse protection is separate: endpoint/user/IP/workspace request budgets return safe throttling responses and protect login, OAuth initiation, imports, SMTP tests and costly queries. It does not decrement campaign-email capacity. Recipient unsubscribe is protected from abuse without silently discarding valid suppression requests.

LLM generation and website-research fetches ([ADR-0011](../adr/0011-hyper-personalized-campaign-type.md)/[0012](../adr/0012-llm-port-data-handling-and-validation.md)/[0013](../adr/0013-research-sources-and-outbound-fetch.md)) are a third, separate budget: per-workspace daily caps (generation, preview, fetch) reserved atomically in PostgreSQL before each call, plus a global requests-per-minute limit. They never consume send capacity, and send workers never wait on them.

## Configuration and applicable scopes

PostgreSQL stores versioned limit policies, effective dates, cooldowns and restrictions. Configuration precedence is the intersection of all applicable limits, never one override that erases a stricter scope.

| Scope | Controlled quantity / ownership |
|---|---|
| Platform | Aggregate concurrent submissions, throughput and emergency restriction; operator policy. |
| Provider/project | API operation cost and shared project/account restrictions; adapter-supplied policy metadata. |
| Workspace | Customer/platform allowance and fair share; approved workspace configuration. |
| Mailbox/account | Message/recipient rolling budgets, minimum spacing, concurrency and cooldown; account policy. |
| Campaign | Daily budget and pace; campaign configuration bounded by broader scopes. |
| Optional recipient-domain safety | Only if separately approved; not an implicit MVP domain throttle. |

Track units explicitly: API quota units, messages, recipients and concurrent calls differ. Send, sync and refresh may consume different API costs. Published ceilings are not recommended outreach volume. No historical numeric quota is copied into defaults; provider/account policy and safety thresholds need release review.

## Mailbox limits and pacing (go-live)

Every mailbox has one MAILBOX-kind policy row: **50 messages per rolling 24 hours, at least 60 seconds apart** by default (`MAILBOX_DEFAULT_DAILY_CAP`, `MAILBOX_DEFAULT_MIN_SPACING_SECONDS`; 1-500 per day and 0-3600 s when a person with `mailboxes.manage` edits it). Migration 0038 backfills existing mailboxes and the connect flow creates the row in the same transaction as the mailbox. Only a MAILBOX policy with a window of at least an hour counts as a volume cap.

**Absence is not unlimited for a mailbox.** The general rule above (no row, no cap from that scope) still holds for workspace, campaign and platform scopes, but a CAMPAIGN message whose mailbox has no such cap is deferred (`mailbox_limits_missing`) and never sent.

**A denial reschedules the message.** On `RateLimitDenied`, and on a safety-hold rejection, the send worker returns the claimed message to `SCHEDULED`/`RETRY_SCHEDULED` with `due_at = now + clamp(retry_after, 30 s, 24 h)` plus up to 10% jitter (15 minutes for a held or limit-less mailbox) and clears the claim (`SendingRepository.defer_for_capacity`). It bumps the dispatch generation, so a late duplicate of the task that deferred it is recognised as stale instead of finding a `SCHEDULED` message and skipping it for good, and it never touches a message that has an attempt. Before this, a denied message stayed `QUEUED` until its claim lease expired and was then claimed again at once.

**Planning spreads the load.** When a campaign is rendered, each first email gets its own `due_at` (`campaigns/pacing.py`): its place in its mailbox's queue, `(capture_ordinal - 1) // mailboxes`, gives a sending window (day) and a slot inside it, at least the mailbox spacing apart. A campaign's `daily_limit` is shared equally between its mailboxes and never exceeds a mailbox's own cap. The send-time limiter remains the authority; planning only keeps it from refusing most of a large campaign. Campaigns activated before this keep their old times and rely on the limiter and deferral.

**Backpressure.** The scheduler does not claim more messages than `SCHEDULER_MAX_OUTSTANDING_CLAIMS` (default 200) minus those already `QUEUED`, so claims do not expire unused behind a busy send worker.

## Atomic reservation protocol

Use a Redis token bucket for short-term pacing/concurrency and timestamped rolling-window debits for long-window quantities. Reserve all applicable scopes in one atomic operation: prune/refill against server time, evaluate every capacity/cooldown, then consume all or none and return reservation ID, policy versions, generation, expiry and next eligible time. Reserve once per invocation owner; duplicate reservation ID returns the same decision. Denial returns the maximum blocking time, not permanent failure.

Atomic scripts execute without interleaving on one Redis execution domain; keep work bounded. This property is documented in [Redis scripting](https://redis.io/docs/latest/develop/programmability/eval-intro/), checked 2026-09-09. Proposed initial limiter uses one primary/shard for the shared multi-scope keys. Do not deploy a clustered cross-slot script and assume atomicity; sharding would require a separate reviewed coordination design.

Redis reservation happens before the final [message authorization transaction](MESSAGE_STATE_MACHINE.md). That transaction rechecks reservation generation/expiry and policy versions, then writes durable capacity debits with the SENDING attempt. The debit ledger supports recovery/audit and conservative hard-cap checks under relevant scope-row locks. Worker invokes provider only after this commit. A reservation without a database commit may waste capacity until expiry; underutilization is preferable to oversending. No unprotected database-to-Redis dual write is required for correctness: a lost Redis settlement can only retain extra consumed capacity, never grant it back.

Scope rows are locked in stable scope-kind/ID order within authorization after common safety gates; policy mutation/recovery uses the same order. PostgreSQL checks long-window caps from indexed active debits while holding the relevant scope lock. Cross-workspace account reuse is not enabled until shared account identity/quota rules are approved. Concurrent send count includes PREPARED/SENDING and UNKNOWN attempts; expired process leases do not free potentially active provider requests.

## Settlement, time and retries

An accepted or unknown attempt consumes message/recipient allowance conservatively. Definitive nonacceptance may release a particular unit only if provider semantics prove it was not counted; API-call cost may remain consumed. Refunds are idempotent per reservation/unit and cannot be based on worker timeout alone. Proposed initial policy avoids refunds for uncertain provider usage.

Rolling windows are measured over UTC instants, not local midnight. A configurable calendar-day campaign/customer budget records its explicit timezone and bucket start/end UTC; it remains separate from a provider rolling-day limit. DST changes bucket duration but not message timestamps. Persist last authorization time for minimum spacing. Out-of-order workers use database/Redis time, not browser clocks; abnormal clock drift stops refill until investigated.

Deferral stores next eligible time projected into the campaign window. It does not increment send-attempt count. Definitive provider rejection may schedule a safe retry with its own new reservation. Respect Retry-After and persisted provider cooldown in addition to platform backoff; repeated scope failures can open the circuit in [PROVIDER_ARCHITECTURE](PROVIDER_ARCHITECTURE.md).

## Redis loss and reconstruction

Sending fails closed if limiter state is unavailable, incomplete or its generation changed. A missing key never means an unused full budget. Use a durable rate-control generation and READY/RECOVERING state. Each authorization transaction verifies READY and expected generation; recovery takes an exclusive gate so no new authorization commits against an old generation.

On Redis restart/failover/data-loss suspicion: enter RECOVERING, invalidate old reservations, account for all authorized attempts from PostgreSQL, and rebuild long-window debits and known cooldowns. Start short token buckets empty and refill at configured rate. Restore minimum spacing from latest durable authorization. Retain uncertain active slots until invocation deadline plus confirmed process quiescence/reconciliation, not mere lease expiry. Queries use a conservative overlap window to avoid a recovery boundary gap. Before READY, verify all required scopes loaded and recovery watermark stable; then atomically expose new generation to workers.

If provider/account usage outside this application is unknown, local counters cannot claim exact remaining provider allowance. Use conservative platform limits, observed provider cooldowns and user-visible capacity estimates; never describe them as authoritative total account quota. After a database restore, disable sending until potential sends beyond the restore point have been reconciled; restoring old counters is not safe evidence.

## Fairness and failures

Scheduler bounds per-workspace/campaign outstanding work and rotates allocations. The limiter guarantees safety, not fair arrival order; a provider-wide cap needs scheduler allocation to avoid one tenant winning every reservation. Do not occupy send workers while waiting for future capacity.

Database failure prevents new authorizations. Redis outage defers sending; unrelated read APIs may remain available. Process crash before authorization wastes only transient tokens; after authorization it leaves durable debit/attempt. Duplicate task cannot debit another invocation against a completed message. Provider outage/cooldown delays relevant scopes. Deployment/policy edit invalidates stale versions. Unknown outcome never triggers token refund and resend. Operator rate reductions apply at the next authorization and are auditable.

## Security, observability and tests

Only authorized configuration commands alter policy. Worker-supplied workspace/mailbox IDs must match durable message ownership. Redis credentials are private; no email addresses or bodies in keys/logs. Log policy scope IDs, reservation/attempt IDs, units, decision/reason and recovery generation. Metrics include deferrals, remaining estimates, reservation expiry, recovery duration, cooldowns, unknown debits and tenant fairness. Alert on missing scopes, generation mismatches and attempted cap violations.

Tests: simultaneous multi-scope acquisition (no partial debit), duplicate reservation/refund, provider versus calendar windows, DST, minimum spacing, policy change race, database commit failure after Redis reservation, Redis flush between reservation/authorization, failover reconstruction with in-flight sends, negative provider evidence, clock drift, restore-point uncertainty, and noisy-tenant fairness. Verify selected Redis topology and bounded script cost under load.

## Open Decisions and definition of done

Approve numerical policies, exact units per provider operation, refund rules, Redis topology, recovery thresholds and operational SLOs. Technical default is conservative accounting and no sending with unknown limiter state. Implementation is complete only when cap/reconstruction tests pass and quota configuration has current provider review. Related: [scheduler](SCHEDULER.md), [workers](WORKERS.md), [database](../database/DATABASE.md).
