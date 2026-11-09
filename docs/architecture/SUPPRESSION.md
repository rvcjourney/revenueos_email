# Suppression

## Approved migration review decisions

Normalization v1 trims outer whitespace, validates exactly one ASCII-local-part mailbox, rejects controls/display-name ambiguity, lowercases the local part and validated IDNA domain, and preserves original display. Dots and plus tags are unchanged. Canonical identity is immutable in recipient_addresses. Workspace unsubscribe applies across the workspace. Members and above add/import MANUAL blocks; only Admin/Owner release MANUAL with audit. Other releases are denied. Full parsing/IDNA belongs to the backend; SQL enforces the bounded canonical storage shape.

These decisions supersede corresponding proposals/Open Decisions below. Other release-policy decisions remain open.


## Purpose and authority

Suppression is a shared backend domain service enforcing durable address prohibitions for every customer outreach path, including controlled test sends and retries. It implements [MVP §§13, 27–29](../product/MVP.md), [PROJECT_CONTEXT §§39, 70–71](../product/PROJECT_CONTEXT.md), and [SYSTEM_ARCHITECTURE §§51–55](SYSTEM_ARCHITECTURE.md). Campaign selection, mailbox adapters, imports and frontend controls cannot override it.

This document owns identity, scope, reason and release semantics. Reply stopping is separately owned by [REPLY_SYNC](REPLY_SYNC.md); not every reply is a workspace suppression. Platform account notifications remain a separate communication purpose, never a loophole for campaign sending.

## Identity and scope

Suppression does not require an existing lead. Use a normalized address identity with a normalization version. Proposed MVP normalization: trim outer whitespace, parse exactly one mailbox, reject CR/LF/control characters/display-name ambiguity, canonicalize domain via validated IDNA and lowercase it; case-fold the local part for the workspace contact identity. Preserve original address for authorized display. Do not strip dots, remove plus tags or use provider-specific alias equivalence. Non-ASCII local-part support is not enabled until transport/normalization compatibility is approved.

Case folding can merge rare case-distinct mailbox local parts; this is a deliberate conservative recommendation requiring approval before uniqueness migrations. A normalization change requires collision analysis and lookup across retained versions during migration, not silently changing comparison on live data.

Workspace suppressions apply to that address across all campaigns, lists and sending mailboxes in the workspace. Unsubscribe from one campaign is therefore durable across that workspace. Separate platform block records apply across workspaces only for verified operator-managed safety rules; ordinary customer suppression must never leak to another tenant or create a global block. Global Block is a source concept in PROJECT_CONTEXT §39; exact escalation/removal authority remains open.

## Reasons and reversibility

| Reason | Source and creation | Proposed removal rule |
|---|---|---|
| MANUAL | Authorized workspace command/import with reason and actor. | Explicit authorized release with confirmation/audit; missing RBAC means deny until grants approved. |
| UNSUBSCRIBE | Valid recipient token/request or verified provider evidence. | No ordinary campaign/list/import release. Any re-consent process needs separate approved product design and evidence. |
| HARD_BOUNCE | Definitive permanent recipient failure evidence. | No automatic release; documented corrected-address/false-positive process requires approval. Changing a lead address does not erase old-address suppression. |
| COMPLAINT | Verified supported complaint event. | No ordinary user release. Operator exception requires evidence and separately approved policy. |
| PLATFORM_BLOCK | Operator safety intervention, separate global scope. | Privileged reviewed release, audited; customer users cannot remove or enumerate global records. |

Multiple active reasons coexist. Effective result is blocked if any applicable reason is active. Release of MANUAL must not clear UNSUBSCRIBE/HARD_BOUNCE/COMPLAINT. Store source evidence separately so duplicate events do not create duplicate prohibitions and new sources remain auditable. A later delivery/open/import event never restores eligibility.

Lifecycle: ACTIVE → RELEASED only through permitted command. Repeated active reason is an idempotent upsert plus deduplicated source observation; a later new valid source after release reactivates the prohibition with new audit evidence. Immutable source/release history survives current-state changes. Recipient execution already STOPPED never automatically resumes when suppression is released; new outreach intent requires review.

## Enforcement and race protocol

Planning and import can flag/exclude suppressed addresses to provide useful feedback. The authoritative check occurs in the final message authorization transaction, against PostgreSQL, never a stale “not suppressed” cache.

An absent suppression row must still be lockable. Maintain a durable address gate keyed `(workspace_id, normalized_address, normalization_version)`; create/upsert it before taking its row lock, including for an address with no lead. Send authorization locks the address gate and evaluates all active reasons. Suppression creation takes the same gate. Full shared lock order is platform gate → rate-control readiness gate → workspace → address → campaign → enrollment → mailbox → message → rate-scope rows, omitting gates the operation does not require and using stable order for multiple addresses/records.

Platform block mutation acquires the global sending gate exclusively; authorizations acquire it shared before workspace gates. This serializes new global prohibitions without allowing customer access to the global list. It may briefly delay all authorizations; global block mutation volume is expected to be low. Use restricted provider-independent boolean eligibility access to platform blocks.

If suppression commits before send authorization, that send must be denied. If authorization commits first, an in-flight invocation may complete under the explicitly bounded [message contract](MESSAGE_STATE_MACHINE.md). Suppression response confirms durable prohibition for future authorizations; it does not promise recall of an already authorized email. No PostgreSQL transaction is held during provider I/O.

In the creation transaction persist suppression/current reason, source identity, domain event, audit where relevant and cancellation work. Gate establishes immediate safety; batching recipient/message stop updates can follow asynchronously. Pre-send lookup blocks during that delay. For small resolved enrollment sets, stop them in the same transaction using lock order. For broad workspace suppression, batch cancellation locks sorted campaigns/enrollments and leaves SENDING/UNKNOWN evidence intact.

## Unsubscribe endpoint

**As built (see [ADR-0019](../adr/0019-stateless-unsubscribe-and-default-mailbox-limits.md)).** The token is stateless: `u1.` + the workspace and message ids + a truncated HMAC-SHA256 under `UNSUBSCRIBE_SIGNING_KEY`, with no expiry (a link must work as long as the email exists) and nothing stored at send time. It identifies a *message*; the address is looked up from that message, so a link can only ever suppress its own recipient in its own workspace. The signature is checked before a database connection is taken. GET shows a confirmation page and never unsubscribes (link scanners fetch every URL); POST suppresses, and also accepts the RFC 8058 `List-Unsubscribe=One-Click` form post that mail providers send. The side effects (suppression upsert, stopping enrollments, skipping future messages, recording the outcome) run as `app_worker_general` scoped to the token's workspace, because the recipient is anonymous and has no user for the permission-based policies. Every campaign email carries the link in a footer together with the workspace's postal address, and (Gmail API and SMTP) the `List-Unsubscribe` and `List-Unsubscribe-Post` headers; Microsoft Graph mailboxes carry the footer link only. The send path refuses to send a campaign email when the unsubscribe settings are missing, and a campaign cannot be activated without them or without a postal address.

Generate an opaque high-entropy token bound to workspace/address and intended communication purpose; store only digest and protected mapping. Never put raw email or tenant authority in a predictable URL. A token resolves exactly one scope and cannot accept client-supplied replacement address/workspace. GET may display confirmation without causing accidental unsubscribe from link scanning; the explicit supported unsubscribe submission commits suppression before success. One-click protocol behavior, token lifetime and header requirements must be approved and verified before provider release.

Repeated valid requests succeed idempotently. Expired/invalid requests return a neutral safe response with an available supported recovery path, never disclose membership or lead existence. Database failure must not display confirmed unsubscribe. Rate protection must not silently drop valid recipient requests. Unsubscribe rendering/endpoint availability is a preflight requirement, not optional analytics.

## Imports, lists, identity changes and deletion

Import stores suppression indicators/results but does not create active-send authority. Duplicate lead upsert never clears suppression. Suppression import is a separate authorized job with address/reason validation, per-row identity and deduplication; it does not send mail. List removal/addition has no effect on prohibition. Lead identity change freezes old enrollments against old address; new address requires fresh eligibility and does not transfer/remove the old prohibition.

Lead or campaign archival cannot cascade-delete suppression. Erasure policy must balance minimization and preventing recontact without blindly retaining all lead history. Recommend retaining minimal normalized identity or purpose-bound keyed lookup digest plus reason/evidence metadata under approved retention; a digest is still sensitive pseudonymous data, not automatically anonymous. Exact periods and legally required erasure exceptions require product/security review, not an invented legal conclusion.

## Failure, recovery and operations

Process crash before commit leaves no false success; after commit safety persists even if cancellation task is lost. Outbox retries and a periodic enrollment sweep repair visible stop state. Redis restart cannot remove suppression or permit cached negative answers. Database outage fails closed on authorization and unsubscribe confirmation. Duplicate provider/source event is deduplicated. Provider outage limits new external signals but does not weaken existing prohibitions. Deployment must preserve normalization versions. Concurrent release/new complaint serializes on address gate; a new complaint wins as an independently active reason. Stale clients cannot remove a reason using an old version.

Log workspace/address-gate/suppression/source IDs, reason, actor and result; never address/token values. Metrics include blocked authorization count, suppression ingestion/cancellation lag, releases by reason, duplicate sources and global restriction activity. Alert on any authorized send with an earlier applicable active suppression or missing gate. Operators investigate through audited commands, not ad hoc data edits.

## Open Decisions, testing and definition of done

Normalization v1, workspace-wide unsubscribe scope and MANUAL release authority/evidence are approved above and in USER_ROLES.md. Global-block escalation, one-click UX/token policy and retention still require explicit operator/product decisions; no customer platform-block release is permitted. Suppressions are never deleted: when a person is erased under [ADR-0015](../adr/0015-archive-purge-and-erasure.md), an address with an ACTIVE suppression keeps its address record as the opt-out record and everything else about the person is erased.

Tests must cover absent-row suppression/send race, both transaction orders, duplicate sources, independent active reasons, release racing complaint, new suppression between retry attempts, lead/list deletion and reimport, address changes, Unicode/CRLF/alias inputs, cross-tenant tokens/IDs, global-block isolation, database failure during unsubscribe, stale queued tasks, Redis flush and cancellation task loss. Verify no provider or test-send path bypasses the shared service. Related: [events](EVENT_SYSTEM.md), [database](../database/DATABASE.md), [security](../security/SECURITY_ARCHITECTURE.md).
