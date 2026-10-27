# Provider Architecture

## Approved migration review decisions

One provider account may be active in only one workspace. Protected connection commands use a dedicated user-scoped connection role, not ordinary app_api. Refresh creates a new encrypted generation and conditionally moves the current pointer; disconnect invalidates the pointer/generation before secret destruction. Nonsecret generation history survives.

These decisions supersede corresponding proposals/Open Decisions below. Other release-policy decisions remain open.

## Purpose and boundaries

The backend provider package implements transport and account capabilities behind ports consumed by mailbox, messages and conversations services. Preserve Gmail → Microsoft → Custom SMTP implementation order, as required by [MVP §§16–18](../product/MVP.md) and [SYSTEM_ARCHITECTURE §§36–44](SYSTEM_ARCHITECTURE.md). Adapters do not decide campaign lifecycle, suppression, tenant authorization or retry eligibility; they supply evidence for those decisions.

Platform transactional communication uses a separate provider interface, credentials and workload. No campaign fallback sends through that provider. Direct-to-MX, MTA ownership and provider-limit evasion are excluded.

## Capability contract

Capabilities are resolved per connection from adapter support, granted scopes and tested account configuration. Never equate the provider brand with a working permission. Unsupported returns a typed capability result before attempting I/O.

| Operation | Input / result | Gmail | Microsoft | SMTP |
|---|---|---|---|---|
| Validate connection | Protected credential reference; normalized account identity, scopes, safe diagnostic stages | OAuth/API | OAuth/Graph | DNS/TCP/TLS/auth stages |
| Refresh credentials | Expected credential generation; new encrypted generation or classified failure | OAuth where authorized | OAuth where authorized | Only configured authentication mechanism supports it; no invented refresh for passwords |
| Send | Immutable content, envelope (including verified attachments and `cid:` inline images), attempt ID, optional correlation/thread context, bounded deadline | Gmail API | Graph | Authenticated submission |
| Account status | Safe health/capability facts and observation timestamp | Available facts only | Available facts only | Successful connection does not prove future delivery |
| Message lookup | Scoped IDs/correlation plus evidence strength | Account/scopes dependent | Account/scopes dependent | Not supplied by SMTP itself |
| Reply sync | Cursor → bounded normalized page + checkpoint | Gmail history/full sync (`gmail.readonly`) | Graph Inbox delta (`Mail.ReadWrite`) | IMAP, only for mailboxes saved with IMAP settings |
| Provider events | Verified receipt → normalized evidence | Do not assume full delivery/complaint feed | Do not assume full delivery/complaint feed | Provider-specific feed/DSN where supported |
| Native idempotency | Verified key scope, expiry and replay contract | Default unsupported until proven | Default unsupported until proven | No deduplication guarantee from Message-ID |

`send` returns ACCEPTED, DEFINITIVELY_REJECTED, or UNKNOWN with safe category, provider IDs if supplied, provider timestamp if reliable, Retry-After if present, and minimal reconciliation evidence. Never represent “no message ID returned” as rejection.

Every send must report the Message-ID the recipient will see (`ProviderSendResult.rfc_message_id`) because replies and bounces quote it: Gmail sends our generated Message-ID and reads the stored value back (falling back to ours if the read scope is missing); SMTP sets it; Graph creates a draft (`POST /me/messages`, which returns `internetMessageId` and `conversationId`) and then sends it (`POST /me/messages/{id}/send`) — a failure creating the draft is a definitive non-send, a timeout while sending is UNKNOWN, and a rejected send discards the draft. Reading and draft creation are why the OAuth scopes now include `gmail.readonly` and `Mail.ReadWrite`; connections made earlier must be reconnected (reply sync is parked as `RECONNECT_REQUIRED` until then).

Same-thread follow-ups ([ADR-0021](../adr/0021-follow-up-same-thread.md)): the send worker loads the parent's Message-ID, every ancestor's Message-ID and the parent's latest ACCEPTED `provider_thread_ref`, and adds `In-Reply-To` / `References` through `extra_headers`. SMTP sends the headers only. Gmail sends them plus `threadId`; if Gmail refuses the thread (404, or 400 naming the thread — inferred, not documented by Google), which proves nothing was sent, it is sent once more without `threadId` (`ProviderSendResult.thread_dropped`). Microsoft Graph ignores extra headers and threads natively instead, from `OutboundMessageEnvelope.in_reply_to`: it finds the parent's sent copy by `internetMessageId` across the whole mailbox (`GET /me/messages`, drafts skipped; the filter is percent-encoded by hand and quotes are doubled), `POST /me/messages/{parent}/createReply`, then `PATCH`es that draft to the frozen snapshot (recipient = the frozen destination only, no cc/bcc, our subject, our composed HTML replacing the quoted original) and uploads the attachments to it before the unchanged `POST /me/messages/{draft}/send`. The reply draft's `internetMessageId` and `conversationId` are recorded like any Graph draft's. No sent parent (empty result, only drafts, or 404 on createReply) means an ordinary unthreaded draft and send (`thread_dropped`, audited as `provider_thread_not_found`); a 429/5xx/timeout or missing scope on any of these calls is the usual definitive pre-send rejection (nothing sent, retryable as classified), and a reply draft already made is deleted best-effort. Reconciliation's `lookup_message` is unchanged: it still searches Sent Items only, so a draft is never taken as proof of a send. Every unthreaded fallback is audited as `message.same_thread_unavailable`; the message is never failed for it.

Gmail sends MIME content via its API; Graph `sendMail` returns `202 Accepted` without a response body and this does not prove processing/delivery completed. Adapter acceptance therefore maps to local SENT as submission acceptance, not DELIVERED. See [Gmail sending](https://developers.google.com/workspace/gmail/api/guides/sending) and [Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0), checked 2026-09-09. Exact SDK versions, scopes and quotas must be reverified during implementation.

## Connection, health and eligibility

Persist independent axes:

| Axis | States and transitions |
|---|---|
| Connection | CONNECTING → CONNECTED / RECONNECT_REQUIRED; CONNECTED → RECONNECT_REQUIRED / DISCONNECTED; reconnect validates identity before CONNECTED. DISCONNECTED only reconnects through a new authorized flow. |
| Health | UNKNOWN → HEALTHY on validated success; HEALTHY → DEGRADED on transient failure; repeated dependency failures may open circuit. Health observation never overwrites connection revocation. |
| Policy | ENABLED / RESTRICTED with actor/reason; changes require authorized command. |
| Circuit | CLOSED → OPEN on classified repeated failures → HALF_OPEN for bounded safe probe → CLOSED or OPEN. AUTH_FAILURE requires reconnect, not recurring credential probes. |
| Sync | INITIALIZING / CURRENT / LAGGING / RESYNC_REQUIRED / UNAVAILABLE; freshness threshold is policy, not inferred from connection status. |

Send eligibility is computed from connection, policy, health/circuit, required capabilities, credential generation, schedule and safety gates. A throttle persists `blocked_until`; it does not disconnect OAuth. Connected but missing reply capability cannot silently promise stop-on-reply sequences.

## OAuth and credential lifecycle

Mailbox connection is distinct from Supabase login. Initiation checks current mailbox-manage authority and persists short-lived single-use state bound to user, workspace, provider, approved return path and PKCE verifier where supported. Callback revalidates session/membership and consumes state atomically; token exchange occurs outside a long transaction. A callback claim/recovery status prevents a second exchange. Provider denial leaves connection unusable; uncertain code exchange requires restart rather than replaying codes indefinitely.

Credentials use authenticated encryption with a key identifier, nonce and workspace/mailbox/provider associated data; master keys live outside PostgreSQL. Grant decryption only to sending/connection/sync runtimes. Browser responses contain safe account/scopes/expiry metadata, never token material. Persist credential generation and expected provider account ID.

Refresh acquires a per-connection durable lease/generation, makes one external refresh, then conditionally stores rotated credentials only if generation and connection state still match. Concurrent workers wait/defer. A late refresh cannot revive disconnect. If a provider rotated a refresh token but the local write failed and recovery cannot recover it safely, require reconnect; do not assume the old token is valid. Disconnect first persists policy/connection revocation and advances generation, then destroys secrets and performs optional remote revocation as a tracked external operation. In-flight sends retain the bounded outcome semantics in [MESSAGE_STATE_MACHINE](MESSAGE_STATE_MACHINE.md).

## Error classification and retries

| Evidence category | Domain consequence |
|---|---|
| RATE_LIMIT with known rejection | Persist provider cooldown, safely schedule after Retry-After/backoff. |
| TEMPORARY_PROVIDER_ERROR with known rejection | Bounded retry; circuit can hold other work. |
| AUTH_FAILURE | Reconnect required, visible notification, hold affected unsent work. |
| PERMANENT_RECIPIENT_FAILURE | Terminal message failure and shared suppression rule. |
| POLICY_REJECTION / invalid content | Fail/hold affected scope; no blanket recipient suppression. |
| NETWORK_ERROR before proven request submission | Retry only when adapter proves nonacceptance. |
| Timeout/disconnect after possible submission | UNKNOWN; lookup/reconciliation, no automatic send retry. |
| TLS/DNS/configuration failure | Safe diagnostic, no insecure fallback; classify invocation evidence separately. |

HTTP status or SMTP response class alone is insufficient for precise recipient/policy classification. Preserve safe provider code and stage. Disable SDK/HTTP/SMTP automatic retries for non-idempotent sending. Connect/read/overall timeouts must fit inside worker time limits, with result-persistence margin. Verification and sync may retry independently when reads are safe.

## Reconciliation

Persist local attempt identity before invocation, plus stable RFC Message-ID where transport permits. Positive lookup must match mailbox/account, outbound direction, address/content correlation and exact intent; provider thread alone is insufficient. Do not expose full bodies to telemetry for comparison. Store provenance and observation time. Negative lookup is not proof of nonacceptance; provider indexing can lag and sent copies can be absent. SMTP submission without searchable sent-copy capability may remain unresolved and requires operator visibility. Lease expiry cannot safely resubmit an ambiguous attempt.

## SMTP network boundary

Only approved authenticated submission ports/modes are supported; proposed default 587 with mandatory STARTTLS and 465 with implicit TLS. Validate host syntax, all resolved IPv4/IPv6 addresses, loopback/private/link-local/multicast/unspecified/metadata destinations and IPv4-mapped IPv6 before every connection, including tests and retries. Pin validated address for the socket while using original hostname for certificate/SNI verification; reject rebinding or changed unsafe resolution. No proxy, arbitrary URL scheme, custom resolver, redirects or user-supplied CA bypass. Enforce equivalent egress firewall controls and exclude internal/public-addressed infrastructure through environment deny rules. No plaintext credential fallback. IMAP uses the same network policy.

Connection tests perform DNS, TCP, TLS and authentication with bounded time/resource limits; do not send mail by default. Controlled test send is an explicit message intent through the normal safety pipeline with test-recipient authorization. Diagnostics show stage and sanitized reason, never raw credential-bearing protocol transcript. The compatibility allowlist and exceptions require approval before release.

## Failure, recovery, visibility and tests

Redis loss rebuilds operational circuit/cache state from persisted cooldowns/restrictions; it never grants access to a disconnected account. Database outage prevents credential/state changes and new send authorization. Worker crash during token rotation may require reconnect; crash during send follows unknown-outcome rules. Deployment must preserve adapter evidence schema compatibility. Duplicate callbacks, sync events and tasks remain idempotent in application services.

Observe provider/mailbox/attempt IDs, latency, error category/stage, reconnect count, refresh conflicts, circuit state and sync lag. Alert on persistent auth failures, provider outages and unknown attempts. Redact authorization headers, SMTP passwords, MIME bodies, callback codes and token responses.

Adapter contract tests must cover unsupported capabilities, scope reduction, duplicate callback, revoked membership, account mismatch, concurrent refresh/disconnect, rotated-token persistence failure, positive/negative reconciliation evidence, provider acceptance without ID, DNS rebinding, IPv6/private targets, TLS failures, hidden SDK retries and no real message from connection tests. Use fixtures/sandbox accounts; real sends require explicit controlled authorization.

## Open Decisions and definition of done

Approve OAuth scopes/consent and provider verification requirements, supported SMTP/IMAP combinations, single-workspace account connection policy, circuit/retry/timeouts, and required sync freshness. No numeric sending quota or provider-policy permission is frozen from historical context. Multi-step campaigns should fail preflight without tested reply sync; whether to permit send-only SMTP for single-step campaigns needs explicit product approval. API/adapter implementation is complete only when it satisfies these capabilities and the common [message](MESSAGE_STATE_MACHINE.md), [reply](REPLY_SYNC.md) and [security](../security/SECURITY_ARCHITECTURE.md) contracts.
