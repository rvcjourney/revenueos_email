# Database Design


## Purpose, status and derivation

This is the logical production database design derived after the [domain model](../architecture/DOMAIN_MODEL.md), [campaign engine](../architecture/CAMPAIGN_ENGINE.md), state machines, scheduler, workers/queues, rate limiting, events, reply synchronization and suppression specifications. It refines [SYSTEM_ARCHITECTURE](../architecture/SYSTEM_ARCHITECTURE.md); it does not create or execute SQL.

**Review status:** the five unapplied drafts in `supabase/migrations/` have been corrected under the approved migration review. [MIGRATION_REVIEW](MIGRATION_REVIEW.md) records the table/requirement matrix, per-file changes and static-only validation limits. SQLAlchemy owns runtime access; Alembic/ORM auto-create remain prohibited. No migration was applied.

The approved [role matrix](../product/USER_ROLES.md) now owns permissions and ownership policy; [USER_FLOWS](../product/USER_FLOWS.md) preserves journeys. The contracts below refine the earlier logical catalogue.

## Approved migration/runtime contracts

The five-file source of truth is `supabase/migrations/`. The following contracts
are approved refinements of the catalogue, not implemented backend commands.

| Capability | Dedicated runtime responsibility |
|---|---|
| app_api | Verified user, selected workspace, current membership and approved action matrix; ordinary commands |
| app_connection | Verified user + Manager-or-higher mailbox permission; protected OAuth/connection operations |
| app_worker_general | Selected workspace imports, capture/planning, outcome processing, notification fanout; no credentials |
| app_worker_send | Selected workspace provider invocation/evidence and capacity accounting; protected credentials |
| app_worker_sync | Selected workspace provider receipts, inbox sync/reconciliation and protected credentials |
| app_scheduler | Selected workspace due scheduling, claims and recovery; no credentials |
| app_outbox_relay | Selected workspace work publication/recovery metadata only |
| app_rate_controller | Global readiness/reconstruction and retained accounting reads; cannot alter configured global limits |

`app_foundation_reader` and `app_integrity_guard` are NOLOGIN, isolated function
owners. Runtime identities never inherit/assume them. Provision a dedicated LOGIN
per runtime capability, without ownership, SUPERUSER, BYPASSRLS, CREATE, CREATEROLE
or membership in unrelated capabilities. No one pooled application login should
be granted all service roles. Role names here are capabilities, not credentials;
login/TLS/secret provisioning is deployment work, outside the migrations.

For each API/connection transaction: verify JWT externally, BEGIN READ COMMITTED,
SET LOCAL ROLE to the identity's one capability, bind
`set_config('app.user_id', subject, true)` and
`set_config('app.workspace_id', authorized_workspace, true)`, then recheck current
membership/action/resource and expected versions. Workers bind only the trusted
workspace context; audit them as SYSTEM. COMMIT or ROLLBACK on every path, including
cancellation and errors. Never use session-wide user/workspace context or reuse a
transaction for another principal. Supabase browser roles are denied. GUCs do not
provide authentication against arbitrary SQL executed in a compromised backend.

Worker discovery must use trusted workspace routing, not unscoped product reads.
Global scheduler/relay tenant discovery, public unsubscribe digest resolution,
workspace bootstrap, invitation acceptance, ownership transfer and operator commands
need narrowly scoped audited command interfaces in their backend milestones. Add
such interfaces through `supabase/migrations/` when implemented; do not solve missing
lock/discovery access by giving the runtime table ownership or broad global grants.
In particular final send/suppression commands must provide restricted row-lock
access to the documented platform → rate-control → workspace → recipient → campaign
→ enrollment → mailbox → message → rate-scope gates. Ordinary SELECT grants alone
do not authorize PostgreSQL locking clauses. These command interfaces are still
required before sending is enabled.

Schema-specific refinements:

- Normalization v1 is defined by SUPPRESSION.md. SQL checks the canonical ASCII
  storage shape; full mailbox parsing, IDNA, labels and length policy are backend
  validation. Recipient identity is immutable. Original input remains separate.
- `campaign_audiences.selection_manifest` is a bounded object with numeric
  `version: 1`, `lists: []` and `leads: []`; entries are selected UUIDs, sorted and
  validated by the backend. Persist before processing. Capture freezes list
  membership using database counters; contact fields are sampled in bounded batches.
  Captured source revisions and the completed manifest digest support recovery.
- Current campaign settings reference an immutable owning version. Activation
  freezes audience/content; paused-only schedule/limit edits append a version then
  update the pointer and schedule generation. No activated mailbox reassignment in
  this MVP. RUNNING pauses before archive; completed outreach is duplicated.
- `connected_generation` references an actual connection only while CONNECTED.
  Refresh creates a new encrypted generation and conditionally advances the mailbox
  pointer under its lock. Disconnect increments the fencing generation and clears
  the pointer before destroying secrets. Retained generations are never reused.
- OAuth CLAIMED state carries owner/expiry/generation. API and callback must use
  the protected connection identity and current actor checks; terminal callbacks
  clear leases/verifier material. Exact provider verification stays in the adapter.
- PLANNED messages can omit render content. Before scheduling/dispatch, persist
  destination, sender address/name, subject/body, digest and rendered time together;
  signature is already included in frozen body HTML. Later mailbox/template/lead
  changes cannot alter a rendered send. Stable RFC identity is generated before I/O.
- Independent suppression reason rows may be reactivated. Every observation has
  an immutable source. MANUAL release records a current Admin/Owner audit action
  `suppression.release_manual`, target type `suppression`, matching target/actor,
  nonempty reason and request/effect identity. Clearing a release pointer during
  reactivation does not delete its old audit event.
- Audit idempotency uses `(workspace_id, request_id, effect_key)`, allowing multiple
  effects in a request. Domain-event version uniqueness also includes event type.
- Rate identities include unit/window. Tenant calendar-day policies have explicit
  timezone and are separate from rolling windows. `window_seconds = 86400` identifies
  the calendar-day policy, not elapsed DST day length. Debit scope bucket boundaries
  are UTC instants; immutable policy snapshots include all applicable limit, pacing,
  cooldown, timezone and identity values. Debit/scopes share unit/quantity/time.
- Absent rate control, RECOVERING or absent reconstruction evidence deny capacity.
  The backend additionally compares Redis generation/expiry and locks the global
  gate. Only the dedicated rate-controller transitions reconstruction readiness.
- External notification acceptance uncertainty is UNKNOWN, never implicit SENT.
  Reclaiming an expired SENDING lease requires reconciliation before retry. Auth
  email resolution is an internal Auth-service operation, not an ordinary profile
  column or browser query.

The table catalogue describes storage ownership. The review matrix and regression
scenarios map implementation, remaining backend safety work and explicit deferrals.
Billing, CRM, AI, open/click tracking and optional inbox unread state are deferred.

## Common conventions applying to every catalogue entry

- Opaque UUID primary key `id` unless a composite/natural PK is stated. Use UTC timestamp-with-time-zone instants for authoritative time; no server-local timestamp defaults. Mutable aggregates carry `version`, `created_at`, `updated_at`; immutable history carries `recorded_at` and source occurrence time if different.
- **T** means tenant-owned: non-null `workspace_id`, FK to workspaces, unique `(workspace_id,id)` for composite references. Every tenant-to-tenant FK includes workspace_id. When child ownership is narrower, include campaign/mailbox/sequence/enrollment in a candidate key/FK as specified; matching workspace alone is not sufficient.
- **U** means user identity/self-owned; **S** means internal system/global. These exceptions are named explicitly, never represented as nullable tenant IDs that accidentally broaden RLS.
- References default to RESTRICT deletion. Immutable history is retained; archival removes active use. Cascade is allowed only for disposable children explicitly listed below and only from an approved deletion workflow. No generic workspace ON DELETE CASCADE across sending history. The approved deletion workflow is [ADR-0015](../adr/0015-archive-purge-and-erasure.md) (migration 0033): ordered, owner-restricted purge/erase commands, never a cascade; sent history is redacted in place rather than deleted, and suppressions are never deleted.
- Every table has RLS enabled where accessible to runtime roles, explicit least-privilege grants, and default-deny writes. **T-read** means backend-authorized workspace read with current membership; **T-service** means scoped backend mutation/worker capability; **T-secret** excludes normal reads and admits only the protected credential/evidence service. **S-service** is dedicated internal access, not the normal API credential. These classes specify RLS intent, not a missing role-to-action matrix.
- Catalogue lists additional important columns, FKs, uniqueness/checks, indexes/access and lifecycle. PK/unique constraints create their necessary indexes; do not add duplicate indexes. All nullable values have explicit meaning. Bounded JSON is allowed for validated configuration/evidence, not for substituting critical FKs or states. Common checks require nonempty bounded names/keys where present, nonnegative counts, positive versions and coherent lifecycle timestamps. Tables with no additional uniqueness requirement rely on their stated PK; tables with only historical references have no invented cascading domain FK.

## Identity, tenancy and authorization

### profiles (U)

Application display identity, not authentication secrets. PK `id = auth.users.id`; FK restricts uncontrolled identity deletion pending account-erasure workflow. Columns: display name, preferences, timestamps. No password/token columns. Self-read/update for allowed profile fields; another member sees only a safe profile projection after membership authorization. PK lookup is primary access; no global email-search index. Profile archive/PII erasure cannot cascade-delete audit or campaign history. Auth identity deletion is a separately coordinated operation.

### workspaces (T root)

Tenant root; PK id also defines workspace identity. Name, status ACTIVE/RESTRICTED/DELETION_PENDING, sending restriction reason/version, pending-safety count, defaults and owner-policy metadata. Checks: nonnegative pending count; allowed status. Index by status/id supports scoped operational discovery; user list is joined from membership. Workspace row is the restriction/sending gate. Current-member read; mutation requires specific capability. Restriction is reversible with audit; deletion requires approved retention and ownership policy, never ordinary cascade.

### workspace_memberships (T)

PK id; user_id FK profiles; role_code constrained to the five expected roles, active/revoked status, version and joined/revoked timestamps. Unique `(workspace_id,user_id)`; indexes `(user_id,status,workspace_id)` for workspace selection and `(workspace_id,status,id)` for team/RLS membership lookup. Membership revocation retains relationship/audit. No user may edit their own role through profile access. Role-grant checks, protection of last owner and transfer transaction remain blocked on product approval. No speculative custom-role tables.

### workspace_invitations (T)

Inviter FK profiles, intended normalized identity, requested role, token digest (unique), expiry, status PENDING/ACCEPTED/REVOKED/EXPIRED, accepted membership FK. One active invitation per workspace/intended identity; accepted status requires accepted membership. Index `(workspace_id,status,expires_at)` and digest lookup. T-service with invitation-read permission; token resolver returns only acceptance context. Acceptance consumes token and creates membership atomically. Revoke/expiry invalidates digest use; purge token metadata after approved retention, retain minimal audit.

### command_receipts (T)

Durable idempotency for user commands: actor FK, operation, request key, payload hash, resource type/ID as audit reference, response summary/version, status and expiry. Unique `(workspace_id,actor_id,operation,request_key)`; check bounded key/hash and no secrets in response. Index expiry for approved cleanup. Resource audit reference is not authority: target command validates actual FK-owned resource. T-service only. Retain across maximum replay horizon; delete only after commands can no longer repeat effects. Conflicting hash rejects replay.

## Leads, lists and safety identity

### leads (T)

Email original/canonical/version, name/company/title, optional profile fields (migration `0021`), bounded custom fields, ACTIVE/ARCHIVED, validation facts and contact revision. Profile fields are nullable columns on the lead: `phone`, `department`, `experience_years` (0-80), `linkedin_url`, `website` (the person's own site), `city`, `state`, `country`, and the company attributes `company_website`, `company_industry`, `company_founded_year` (1600-2100), `company_linkedin_url`. Company data is deliberately flat on the lead beside the existing `company` text column: there is no company entity, so no company-level dedupe or shared company record exists; a `companies` table would need its own tenant scoping, dedupe rules and snapshot semantics and is deferred until a consumer needs it. Text is bounded to 200 characters, URLs to 500 with an `http(s)://` scheme and no whitespace; the backend (`app/modules/leads/fields.py`) is the format authority and these checks are the database backstop. Every profile field is a template variable of the same name and feeds `contact_revision`, so editing one invalidates captured audience snapshots; `title` remains the job-title column (`{{job_title}}` is its alias). New columns need explicit column-level INSERT/UPDATE grants for `app_api` and `app_worker_general`. Unique `(workspace_id,canonical_address,normalization_version)` including archived contacts prevents reimport bypass. Index `(workspace_id,id)` for paging and canonical key for lookup; optional name-search index only after approved search semantics/query evidence. T-read/T-service. Referenced leads archive; identity edits increment revision and never rewrite captured audience/enrollment addresses. Invalid address is a separate validation fact from suppression.

### lead_lists (T)

Name, archive timestamp, membership revision and capture_count. Check capture_count >= 0. Index `(workspace_id,archived_at,id)` for list browser. T-read/T-service. Membership mutations lock this row and defer while capture_count > 0. Archive preserves campaign selection history; hard deletion of unused list may later cascade only its membership rows after approval.

### lead_list_memberships (T)

Composite PK `(workspace_id,list_id,lead_id)` with composite FKs to both parents. Added_at and actor FK. Reverse index `(workspace_id,lead_id,list_id)` supports lead detail. One association per pair; remove relationship without removing lead. T-read/T-service; exact row removal is permitted by list-management command when no capture is active. No suppression semantics in this table.

### recipient_addresses (T)

Durable absent-row safety gate, with canonical address/normalization version and optional minimal original display representation. Unique `(workspace_id,canonical_address,normalization_version)`; PK id used by suppression/enrollments. No lead FK is required. T-service; no unrestricted address enumeration. Lookup/gate acquisition is the dominant access. Retain while suppression/history requires identity; never cascade with lead. Normalization version transition requires collision review.

### suppressions (T)

Address FK, reason MANUAL/UNSUBSCRIBE/HARD_BOUNCE/COMPLAINT, ACTIVE/RELEASED, first/last observation, released_at, version and release audit ID. Unique `(workspace_id,address_id,reason)`; checks status/release timestamp coherence. Partial index `(workspace_id,address_id)` where ACTIVE is final send lookup. T-read requires suppression visibility; mutations only suppression service. Independent reasons preserve precedence. Release retains row; hard deletion is restricted pending policy.

### suppression_sources (T)

Suppression FK, source kind, source semantic key, optional provider_receipt/domain_event/actor FKs, observed_at and bounded safe evidence. Unique `(workspace_id,suppression_id,source_kind,source_key)`; chronological parent index for audit. Evidence source can be one of explicit nullable typed FKs; check supported source requirements, never arbitrary unvalidated target ID. T-service/T-read redacted projection. Append-only; payload retention may expire while dedupe key survives.

### unsubscribe_tokens (T-secret)

Address FK, optional originating message FK, unique digest, purpose, creation/expiry/revocation time. No raw token storage. Message/address composite relationship verified. Index `(workspace_id,address_id)` for revocation/inspection and expiry for policy cleanup. Restricted token resolver; ordinary API cannot enumerate. Repeated valid token remains idempotent for suppression; token lifetime and post-expiry recovery require approval. Removing message does not erase existing suppression.

### platform_controls / platform_suppressions (S)

`platform_controls` has a singleton key, sending restriction/version and serves the shared/exclusive global authorization gate. `platform_suppressions` has UUID PK, unique canonical-address/version/reason, active/released state, operator actor reference and evidence/audit link. Active-address index serves a restricted boolean eligibility function. No customer read/list/write. S-service modifies with audit under global gate; no workspace-null union with tenant suppressions. Retention is safety-policy governed; no customer deletion cascade.

## Templates and provider accounts

### templates (T)

Name, current version FK, mode STANDARD for MVP, archive timestamp. Index `(workspace_id,archived_at,id)` for browser. Current version FK must include template ID/workspace so it cannot point into another template. T-read/T-service; publishing a version and moving pointer is atomic. Archive referenced template; no cascade of published campaign content.

### template_versions (T)

Template FK, positive revision, subject/body, optional pre-header (`preheader`, 1–255 chars, no control characters; migration 0024), variable schema, content digest and renderer/content schema version. Unique `(workspace_id,template_id,revision)` plus `(workspace_id,template_id,id)` candidate key. Bounded content and required subject/body checks. Parent/revision index supports version selection. Immutable after publication; template copy/activation freezes independent campaign content. T-read, publish-only T-service; retention of referenced versions.

### mailboxes (T)

Provider code, provider account identity, original address, sender display/signature, connection/health/policy/circuit/sync axes, cooldown, pending-safety count, current connection generation and config version. Unique `(workspace_id,provider,provider_account_id)` for known identities; partial indexes `(workspace_id,policy_state,id)` and `(provider,blocked_until,id)` for eligibility/operations. Check nonnegative pending count and defined axis values. T-read safe metadata/T-service. Disconnect advances generation and disables use; history retains mailbox.

Recommended separate global active-account uniqueness (provider + stable account ID) prevents one real account from being connected to multiple workspaces with independent quotas. It must be approved before migration; SQL null/partial uniqueness during CONNECTING must not falsely claim an established identity. If shared accounts are approved, a dedicated consent/quota model is required first.

### mailbox_connections (T-secret)

Mailbox FK, unique `(workspace_id,mailbox_id,generation)`, encrypted credential envelope/key ID/nonce, auth mechanism, granted scopes/capabilities, protected SMTP/IMAP configuration, expiry, refresh lease/generation, revoked_at. Current pointer/generation belongs to mailbox. Check usable encrypted credential fields for active mechanism, positive generation and bounded config; index refresh expiry/lease only for authorized service discovery. Never normal T-read. Conditional refresh cannot overwrite revoked generation. Secret destruction on disconnect/retention leaves nonsensitive generation evidence for attempts; no attempt FK requires retaining ciphertext forever.

### oauth_flows (T-secret)

Actor/provider/workspace, unique state digest, encrypted verifier/exchange material where needed, approved return path, expiry, status PENDING/CLAIMED/COMPLETED/FAILED, claim generation and resulting mailbox FK. Index expiry/claim lease for cleanup/recovery. Check single-use state/status and provider allowlist. Connection service only; recheck membership at callback. Token material expires promptly under approved policy; retain minimal safe audit. No role is granted by callback state.

### mailbox_sync_states (T)

Mailbox FK, connection generation, folder/sync scope, opaque protected checkpoint/continuation, lease owner/generation/expiry, next_due_at, last_complete_at, status and subscription metadata/expiry. Unique `(workspace_id,mailbox_id,sync_scope)`; indexes `(next_due_at,id)` for due discovery, lease expiry for recovery. T-service writes, safe mailbox-status projection reads. Cursor/content context never returned as ordinary metadata. Disconnect suspends sync; reconnect version invalidates stale pages. Retain checkpoint while account is linked; deletion only after safe disconnect/retention.

## Campaign snapshots, planning and progress

### campaigns (T)

Name/description, creator FK, lifecycle status (seven-state closed set), version, start_at, current draft/activated sequence and audience references, activation identity, planning status, schedule generation, archive time and safe error. Index `(workspace_id,status,id)` for browser and `(status,start_at,id)` for system due activation. State/required activation checks; no arbitrary status mutation grant. T-read/T-service commands only. Historical archive, not cascade. [Campaign state machine](../architecture/CAMPAIGN_STATE_MACHINE.md) owns transitions. Pause bookkeeping (migration 0040): `paused_at` (set only while PAUSED), `paused_total_seconds`, `last_resumed_at`. `schedule_generation` is the resume fence: resume bumps it in the same statement as the status flip, and the scheduler claims only messages stamped with the campaign's current value ([ADR-0020](../adr/0020-pause-resume-paused-edits-and-late-enrollment.md)).

### campaign_audiences — kind (ADR-0020, migration 0043)

`kind` is `ACTIVATION` (the audience captured before activation, immutable once READY) or `SUPPLEMENT` (leads added while the campaign is PAUSED, captured with the same machinery, never modifying the activated audience). Immutable after insert. A supplement may only be created by `campaigns.execute` on a PAUSED activated campaign; enrollments from it are accepted by the enrollment guard only while the campaign is PAUSED and the supplement READY. Supplement ENROLL/RENDER planning jobs carry the supplement's `audience_id` and have their own unique key, so they never collide with the activation's jobs.

### Reference edits for a paused personalized campaign (ADR-0020, migration 0044)

`app_guard_frozen_sequence_steps` (SECURITY DEFINER, owned by `app_integrity_guard`) accepts an UPDATE of a step of a FROZEN sequence of a PAUSED, activated `HYPER_PERSONALIZED` campaign only if `kind`, `position` and `wait_duration_minutes` are unchanged and no message of that step has `rendered_at` set. INSERT, DELETE, reorder and every attachment change stay refused. STANDARD campaigns are unchanged from 0042. The guard functions use COALESCE-hardened comparisons so a NULL campaign type fails closed.

### campaign_planning_jobs — scheduler discovery (ADR-0020, migration 0045)

`app_scheduler` gains a table-level `SELECT` and a `USING (true)` policy on `campaign_planning_jobs`, exactly like its other discovery policies, so the periodic sweep can name ENROLL/RENDER jobs whose task message was lost. It stays **read-only**: the migration asserts the role has no INSERT/UPDATE/DELETE. The recovery itself is a worker task (`app_worker_general`) running workspace-scoped.

### campaign_sequences (T)

Campaign FK, revision, DRAFT/FROZEN marker and frozen_at, content digest. Unique `(workspace_id,campaign_id,revision)` and candidate key including campaign/id. Parent revision access uses unique index. Draft edits under campaign lock; activation freezes. T-read/T-service; no update/delete of frozen content through normal service. Referenced sequence restricts deletion.

### sequence_steps (T)

Sequence and campaign composite FK, unique `(workspace_id,sequence_id,position)`, kind EMAIL/WAIT, email subject/body/variable schema (and an optional EMAIL-only `email_preheader`, 1–255 chars; migration 0024) or positive wait duration, optional source template-version FK. Check exactly one payload shape per kind and bounded nonnegative position. Unique candidate key includes sequence/id for message relationship. Ordered parent index interprets sequence. Alternation/start/end validation occurs under sequence lock in activation service because a row CHECK cannot express multi-row structure. Frozen steps immutable; disposable draft steps may be removed by edit command.

### campaign_step_attachments (T)

Files attached to an EMAIL step (migration 0025; [ADR-0010](../adr/0010-step-attachments-and-inline-images.md)). Composite FK `(workspace_id,sequence_id,step_id)` to `sequence_steps` (draft step deletion cascades), campaign FK, disposition ATTACHMENT/INLINE, `content_id` (stable `cid:` token), private-bucket `storage_key`, filename, allow-listed content type, size 1..2.5 MiB, SHA-256. Unique `(workspace_id,step_id,content_id)` and `(workspace_id,step_id,sha256,disposition)`; indexes by step and by storage key (reference counting). RLS enabled and forced: `app_api` may SELECT (`product.read`) and INSERT/DELETE (`campaigns.draft`) — no UPDATE, rows are immutable; `app_worker_send` SELECT only. A trigger rejects any change once the parent sequence is FROZEN. Copied rows share the storage object and content id. The frozen sequence digest covers each row's content id, SHA-256 and disposition.

### Hyper-personalization additions (migrations 0026–0029 prepared; not applied to any shared environment)

Per [ADR-0011](../adr/0011-hyper-personalized-campaign-type.md), [ADR-0012](../adr/0012-llm-port-data-handling-and-validation.md) and [ADR-0013](../adr/0013-research-sources-and-outbound-fetch.md); each migration is prepared separately and applied only after its own review (each file's header is the migration review). Apply 0026 before deploying the code; 0027, 0028 and 0029 before enabling `PERSONALIZATION_ENABLED`, `personalization` dispatch and sample previews respectively. Existing rows are unaffected.

- **0026** — `campaigns.campaign_type` (`STANDARD`/`HYPER_PERSONALIZED`, NOT NULL default `STANDARD`, INSERT-only grant to `app_api`, never updated) and `campaign_sequences.personalization_config` (jsonb object, size-capped, frozen with the sequence by the existing guard). Reference templates reuse `sequence_steps` subject/body/preheader. Since ADR-0016/0017 the same jsonb may also carry optional `email_format` (`TEXT`/`HTML`), `company` and `brand` keys (no schema change; the 16384-byte check still applies, and the application caps the canonical size below it). Unset keys are omitted, so configurations saved earlier keep their approval digest byte for byte.
- **0027** — `personalization_research_cache` (T, per workspace, unique `(workspace_id,source,url_hash)`, bounded extracted text, TTL columns, worker-role access only) and `personalization_usage_daily` (T, PK `(workspace_id,usage_day,kind)`, atomic capped increments).
- **0028** — `message_generations` (T, one row per message, unique `(workspace_id,message_id)`, state/lease/attempt counters, provenance model/prompt version/context digest, `fallback_used`, bounded facts/angle) and append-only `message_generation_attempts` (T, outcome, failure codes, token/cost/latency, digests; never message content or lead data). `app_worker_general` writes; `app_api` reads; the scheduler role gets read-only discovery.
- **0029** — `personalization_previews` (T, bounded sanitized samples, expiry) and append-only `campaign_personalization_approvals` (T, bound to a config digest; approval is valid only while the digest matches).

All are tenant-owned with `workspace_id`, composite FKs, and enabled/forced RLS, like the surrounding tables.

### campaign_settings_versions (T)

Campaign FK, positive revision/generation, timezone, weekday set, same-day start/end, lower-bound start, configured limits, enabled approved settings and computation version. Unique campaign/revision; check valid window/ranges/nonempty days and positive configured limits. Parent-version lookup is main index. Timezone validity is validated against configured tzdata in service. Append new version for permitted paused edit; never mutate historical scheduling assumptions. T-read/T-service.

### campaign_mailboxes (T)

Composite PK `(workspace_id,campaign_id,mailbox_id)`; FKs campaign and mailbox, active assignment/config revision. Reverse mailbox/campaign index supports affected-campaign lookup. Selection not authorization; final mailbox state checked at send. Draft associations mutable; activated removals require approved paused policy and preserve attempt references. T-read/T-service; no cross-tenant sender references.

### campaign_audiences (T)

Campaign FK, revision, CAPTURING/READY/FAILED/ABANDONED status, source manifest digest, start/completion time and capture generation. Unique campaign/revision; active capture partial index for recovery. Capture_count dependencies explained in engine. T-read summary/T-service capture. READY revision is immutable; activation references one READY audience from the same campaign. Unactivated abandoned revisions may expire only after gate release and retention approval.

### audience_capture_sources (T)

Audience/campaign and list composite FKs, captured list revision, gate-held flag, capture generation. Composite PK `(workspace_id,audience_id,list_id)`; reverse list/gate-held index supports gate reconciliation. Check list source type and nonnegative revision. T-service; no arbitrary client counter edits. Held gate insertion/counter increment and release/counter decrement are atomic; recovery reconciles count from active manifests. Keep source manifest for activation provenance; archive with audience.

### campaign_audience_members (T)

Audience/campaign FK, lead FK, address FK, capture ordinal/contact revision, frozen variables and eligibility/exclusion reason. Unique `(workspace_id,audience_id,lead_id)` and `(workspace_id,audience_id,address_id)`; stable ordinal index for chunk traversal. Check accepted/excluded status and bounded captured fields. T-read audience permission/T-service during capture; READY members immutable. Old list/lead changes do not rewrite membership. Retain used audience; disposable failed capture may be purged after approved policy.

### campaign_planning_jobs (T)

Campaign/audience/sequence links validated as same activation, phase CAPTURE/ENROLL/RENDER as appropriate, state, cursor, total/processed counts, lease/generation, next_due_at and error. CAPTURE uses audience_id as its capture identity with composite audience/campaign FK; ENROLL/RENDER use activation_id with composite campaign/activation FK. Separate partial unique keys on `(workspace_id,audience_id,phase)` for CAPTURE and `(workspace_id,campaign_id,activation_id,phase)` for activated phases prevent duplication. Campaign exposes a candidate key including activation_id for this reference. Check progress bounds and phase-specific required/null parent fields. Due/lease index for recovery, campaign index for UI. T-service and safe progress projection. Cursor commits with chunk work. Terminal summaries retain; no activation duplicated on replay.

### campaign_enrollments (T)

Campaign/audience-member/sequence composite FKs, lead/address FKs, frozen destination/variables, assigned mailbox FK, state ACTIVE/COMPLETED/STOPPED/FAILED, next step/sequence pointer, hold/stop reasons, first/last acceptance time and version. Unique campaign/lead and campaign/address; candidate key `(workspace_id,campaign_id,sequence_id,id)` for message consistency. Index `(workspace_id,campaign_id,state,id)` for progress/completion and `(workspace_id,address_id,state,id)` for suppression fanout. Checks terminal metadata and no arbitrary foreign step. T-read/T-service; retain immutable identity/history after activation.

### controlled_send_authorizations (T)

Supports the mandatory pre-campaign Gmail milestone without a synthetic campaign. UUID PK, requester/membership FK, mailbox/address composite FKs, command receipt FK, purpose CONTROLLED_TEST, expiry/bounded send window, recipient approval evidence, revoked_at and version. Unique command receipt; check expiry after creation and fixed supported purpose. Index requester/workspace for review and expiry for due eligibility. T-service creates only after explicit permission and test-recipient confirmation; current membership is rechecked at send. Retain minimal authorization evidence with message history. It cannot authorize a campaign-scale recipient batch.

### messages (T)

Purpose CAMPAIGN/CONTROLLED_TEST is checked as an exclusive payload shape: CAMPAIGN requires campaign/enrollment/sequence/step and no test authorization; CONTROLLED_TEST requires controlled_send_authorization FK and no campaign links. Campaign/enrollment/sequence composite FK and step FK ensure the message step belongs to that enrollment's sequence. Mailbox/address/optional conversation FKs; stable RFC Message-ID; content snapshot/digest/renderer version, due_at, anchor_at, schedule generation, execution state, dispatch origin/generation/expiry, retry budget/time, accepted_at, hold reason and version. Unique `(workspace_id,enrollment_id,step_id)` for campaign messages and unique test authorization for controlled messages; optional provider message ID is scoped by mailbox, not globally unique. One recipient per MVP message. Test authorization mailbox/address must match the message through composite ownership validation.

State checks require complete content/due time before dispatch, accepted_at for SENT, dispatch fields for QUEUED and defined terminal reasons. Partial due index `(due_at,id)` for SCHEDULED/RETRY_SCHEDULED plus workspace/campaign lookup; expired QUEUED index `(claim_expires_at,id)`; history index `(workspace_id,campaign_id,id)`; RFC/provider lookup indexes scoped to mailbox. [Message state machine](../architecture/MESSAGE_STATE_MACHINE.md) governs all transitions; simple CHECK constraints alone cannot enforce transition history. T-read safe content by permission/T-service. Archive/redact under retention; never delete to permit resend.

### message_attempts (T)

Message/mailbox composite ownership FKs, ordinal, invocation owner, credential generation evidence, authorization deadline, dispatch generation, PREPARED/ACCEPTED/REJECTED/UNKNOWN/NOT_INVOKED evidence, timestamps, provider IDs/codes and bounded safe reconciliation metadata. Unique `(workspace_id,message_id,ordinal)`; partial unique message key while unresolved PREPARED/UNKNOWN prevents concurrent attempts. Rejected/accepted result and message state settle atomically. Index authorization deadline for recovery and mailbox/provider identifiers for reconciliation. T-service, redacted activity projection. Never store secrets or duplicate full MIME content. Append evidence and preserve attempt identity; unresolved attempts cannot be purged.

### attempt_evidence (T)

Attempt FK, source kind/identity, observed/occurred times, evidence classification and minimal protected payload reference/digest. Unique `(workspace_id,attempt_id,source_kind,source_key)`; parent/time index supports investigation. T-secret/service, append-only. Late conflicting evidence is recorded even if current outcome is terminal; it triggers incident workflow rather than destructive overwrite. Retention may expire payload while retaining outcome/provenance.

## Conversations, inbound evidence and outcomes

### conversations (T)

Mailbox FK, optional provider thread key, created/latest_activity_at, archive timestamp; nullable campaign summary is a derived display projection, not authoritative association. Partial unique `(workspace_id,mailbox_id,provider_thread_id)` when stable, otherwise unique local anchor identity. Index `(workspace_id,latest_activity_at,id)` for inbox; mailbox lookup for sync. T-read inbox permission/T-service. Archive retains messages; merging unresolved conversations relinks transactionally with alias evidence rather than dropping inbound records.

### inbound_messages (T)

Mailbox/conversation composite FKs, provider stable identity including IMAP scope where needed, RFC headers, bounded normalized participants/content, inbound direction, received/observed times, classification, MATCHED/UNRESOLVED and connection generation. Unique mailbox/provider-message key; RFC ID is indexed for evidence, not assumed globally unique. Index mailbox/received time and unresolved/observed time. Check bounded payload and provider identity shape. T-read sanitized projection/T-secret raw evidence. Replay upserts observations without duplicating message. Retention/redaction preserves minimal matching/stop evidence.

### inbound_outreach_links (T)

Inbound-message and outbound-message composite FKs including mailbox, plus enrollment/campaign relationship validated through outbound message. Evidence type, confidence/status, matched_at. Unique inbound/outbound pair; resolved matching policy admits only supported association cardinality under locks. Index `(workspace_id,enrollment_id)` for reply facts and inbound ID for UI. T-service mutation, T-read safe attribution. No link inserted from subject alone. Retain links after campaign archive; no separate duplicate replies table.

### recipient_outcomes (T)

Enrollment FK, outcome kind REPLIED/UNSUBSCRIBED/HARD_BOUNCE/COMPLAINT and semantic source identity, occurred/observed time and optional inbound/event FKs. Unique `(workspace_id,enrollment_id,kind,source_key)`; index campaign/enrollment/kind through validated composite ownership. Append-only T-service, T-read projection. Allows late reply after COMPLETED/STOPPED without changing terminal enrollment state. Counts use distinct enrollment/lead semantics; outcomes never replace shared suppression.

### conversation_reads (T, optional MVP UX)

Composite PK `(workspace_id,conversation_id,user_id)`; FKs conversation/profile and membership relationship. Last-read inbound/time marker; check valid same-conversation reference when ID used. User-specific read/write via authorized inbox service, not workspace-wide “read” flag. Index user/workspace for unread queries. Disposable preference; remove on membership erasure without deleting conversation. Do not create unless unread UX is selected.

## Durable events, work and safety holds

### provider_receipts (T-secret)

Provider/mailbox/account/subscription scope, unique provider event key or documented fingerprint, verified_at, received_at, source schema, receipt status, lease/generation, retry count/next_due_at, safe error and restricted payload reference/digest. Composite mailbox FK. Unique `(workspace_id,mailbox_id,provider,event_identity)` where mailbox resolves; workspace-scoped verified events use a separate declared scope identity with check, not nullable uniqueness ambiguity. Due/status and lease indexes support ingestion/retry. Restricted ingestion/worker access; safe activity projection only. Retain dedupe identity beyond payload deletion.

### safety_holds (T)

Explicit durable outstanding safety work: unique source receipt/work identity, target_kind WORKSPACE/MAILBOX, exactly one appropriate target FK, ACTIVE/RESOLVED, reason, created/resolved times. Index active target for pre-send/reconciliation. T-service only, safe hold reason exposed. Receipt insertion + hold + target pending-safety count are atomic; resolution decrements once with effects. Counters are accelerators backed by hold rows, checked/reconciled under target gate. Failed receipt keeps hold until a reviewed resolution. No unauthenticated global hold creation.

### domain_events (T)

Event ID, type/schema version, aggregate type/ID/version, semantic effect key, occurred/recorded times, correlation ID and bounded minimal payload. Aggregate reference is historical metadata, not permission; source command checks actual typed resources. Unique semantic key scoped by event kind/workspace; aggregate/version/type uniqueness where one fact per transition is defined. Index `(workspace_id,aggregate_type,aggregate_id,recorded_at)` and recorded time for bounded consumers. T-service append; permission-filtered activity/analytics reads. Immutable; payload minimization/retention without losing required safety facts.

### outbox_work (T)

Work ID, kind/schema, resource ID plus resource type, optional event FK, semantic key, correlation, durable creation/availability and supersession status. Unique `(workspace_id,kind,semantic_key)`; index available/status. Resource reference is interpreted only by registered domain task and revalidated on load. IDs/minimal metadata only, no secrets/body. T-service enqueue/read. Business mutation and row commit together; retain until deliveries and replay horizon settle. Not universal event sourcing.

### outbox_deliveries (T)

Work FK, destination/consumer, state PENDING/LEASED/PUBLISHED/RETRY/COMPLETED/FAILED/SUPERSEDED, lease owner/generation/expiry, next_attempt_at, attempts and safe error/publication observation. Unique `(workspace_id,work_id,consumer)`; due and expired lease/receipt-deadline indexes; check nonnegative attempts and state timestamps. Relay/consumer T-service only. Completion means consumer evidence, not publish success. Retain beyond broker/durable replay horizon.

### consumer_receipts (T)

Composite PK `(workspace_id,consumer,work_id,effect_version)`; work FK, completed_at and safe result reference. Marker/effect commit atomically. Index completed_at for approved retention; no global user reads. T-service, immutable after completion. Delete only when repeated delivery can no longer replay the effect; unique business keys remain additional protection.

### audit_events (T) / platform_audit_events (S)

Separate tables prevent nullable tenancy widening access. Each has UUID PK, actor kind/ID (profile FK nullable only for erased/system actor), action, target type/ID, safe before/after fields, reason, request/command ID and recorded_at. Unique action-effect key when command replay applies; indexes workspace/resource/time or platform actor/time for investigation. Append-only service grants; permission-controlled redacted reads. Target references are historical evidence, not resource authorization. Retention/erasure preserves allowed evidence while minimizing PII; no secret/body values.

## Rate-control persistence

### rate_control (S)

Singleton PK, generation, READY/RECOVERING, recovery watermark/time and policy version. Internal role only. Shared authorization/recovery-exclusive gate; no tenant reads. Durable generation survives Redis loss. Updates audited; never reset to READY by missing Redis key. No ordinary deletion.

### rate_scopes (S) and tenant_rate_policies (T)

Rate_scopes supplies canonical lock identity and policy for platform/provider/account scopes: UUID PK, unique kind/external-scope key, unit/window/limit, version, cooldown and minimum spacing anchor. Tenant_rate_policies has UUID PK, workspace and explicit optional campaign/mailbox FKs, check exactly the fields required by WORKSPACE/CAMPAIGN/MAILBOX kind, unit/window/limit/timezone/version/cooldown. Unique scope/unit/window; checks positive limits and valid intervals. Indexed scope lookup and cooldown recovery. No role grants to override broader limits. Internal policy is S-service only; tenant values exposed by appropriate T-read/T-service capability. Versioned edits/audit, retention of policies referenced by attempt evidence.

### capacity_debits (T)

Attempt FK, reservation ID, unit/quantity, authorized_at, settlement status and provider-counting evidence. Unique attempt/reservation/unit; nonnegative quantity, known settlement state. T-service, redacted capacity projection. Indexed `(workspace_id,authorized_at)` and attempt for recovery. Accepted/unknown remains consumed; no timeout-based refund. Retain through longest quota/reconciliation/restore horizon.

### capacity_debit_scopes (T, internal cross-scope references)

UUID PK and debit composite FK. Exactly one of internal rate_scope FK or tenant_rate_policy composite FK must be populated; check scope kind and ownership. Separate partial unique keys on `(workspace_id,debit_id,rate_scope_id,unit)` and `(workspace_id,debit_id,tenant_rate_policy_id,unit)` cover the selected reference. Quantity/policy version evidence. Index internal scope/authorized_at and tenant policy/authorized_at for rolling sums (timestamp denormalized with write-service consistency validation). Internal function may aggregate across workspaces without exposing rows; normal tenant queries never see another workspace's debit. Immutable accounting association; retained with debit. This models one attempt consuming several scopes without duplicating message intent.

## Imports and notifications

### import_jobs (T)

Initiator FK, immutable storage object key/version/digest, import kind LEADS/SUPPRESSION, validated mapping/dedupe policy, status, row cursor/counts, next_due_at, lease/generation and safe failure summary. Optional list composite FK. Check count consistency, bounded mapping and immutable source after confirmation. Index `(workspace_id,created_at,id)` for history and due/status for workers. T-read import permission/T-service. File bytes remain object storage, not DB blob. Terminal summary survives source-file expiry.

### import_row_results (T)

Composite PK `(workspace_id,import_id,row_number)`; import FK, ACCEPTED/DUPLICATE/REJECTED status, lead/suppression FK as appropriate, safe validation reason and minimal redacted error artifact reference. Check positive row and result shape. Import/row PK handles resume and error paging. T-service, T-read authorized result. Row result and entity/list mutation commit together; replay never overwrites unrelated existing lead fields. Purge raw row/error details under retention while keeping summary/idempotency until replay impossible.

### notifications (T)

Recipient user/membership relationship, source event FK, type, safe resource link/summary, unread/read timestamps. Unique `(workspace_id,recipient_user_id,source_event_id,type)`; index `(workspace_id,recipient_user_id,created_at,id)` for notification center. Check safe known link type. T-service create, recipient-only T-read/update-read, current membership required. Retention can remove stale notices independently of underlying event. No duplicated campaign truth.

### notification_deliveries (T)

Notification FK, channel, attempt ordinal, status/due time, provider identifier and safe error. Unique notification/channel/attempt; due/status index. T-service only. In-app delivery is transactional; optional external channel is separate provider side effect with unknown delivery status when needed. No campaign mailbox credentials or content. Bounded retries and retention; exact channels are open. Platform-wide auth/account transactional emails owned by Supabase Auth/provider need not create fake tenant notifications.

## RLS, pooling and privileged work

Normal API database role is non-owner, non-superuser and has no BYPASSRLS. It receives only required table/function privileges; browser/anon/authenticated Supabase roles cannot directly mutate core business tables. Enable RLS and apply FORCE RLS where owner-path exposure would otherwise bypass it. PostgreSQL explicitly distinguishes owner/BYPASSRLS behavior: [row security documentation](https://www.postgresql.org/docs/current/ddl-rowsecurity.html), checked 2026-09-09.

Request flow: validate auth identity → begin transaction → establish transaction-local trusted user/workspace context → check active membership and action → execute explicitly workspace-filtered query/mutation → commit/rollback. Policies enforce both row visibility and new-row workspace (`USING` and `WITH CHECK` concepts). Missing context denies. `SET LOCAL` context ends with transaction; see [PostgreSQL SET](https://www.postgresql.org/docs/current/sql-set.html). No session-wide tenant settings or pooled connection inheritance. Policy helper for membership avoids recursive membership RLS through a narrowly scoped fixed-search-path function returning only authorization facts; no generic SQL or unrestricted row access.

The application role sets trusted context after JWT verification; RLS is defense in depth against omitted scope, not a claim that an attacker with arbitrary SQL execution cannot forge application context. Parameterized queries, restricted credentials and application authorization remain mandatory. User capability cannot be selected by setting a string: worker/admin privilege derives from actual login role/service identity and narrow grants.

Worker mutation uses workspace-scoped context and capability. Global scheduler/outbox/sync discovery uses narrow functions returning eligible workspace/resource IDs only; privileged functions have fixed search_path, explicit schemas, revoked public execution, validated inputs and dedicated owner. Cross-tenant global rate aggregation and suppression eligibility expose only necessary counts/boolean results. None is the API's unrestricted service-role connection. Audit privileged discovery/intervention where meaningful.

## Transaction, consistency and recovery rules

Lock order: platform gate, rate-control readiness gate, workspace, address, campaign, enrollment, mailbox, message, then canonical rate-scope rows. Commands that do not need some gates omit them but never acquire an earlier class after a later one. Recovery that changes rate generation takes rate-control exclusive gate before scope rows. List capture uses sorted list gates and its job records independently; it must not hold them while acquiring send gates. Claim discovery is lock-free until parent-first bounded mutation; [PostgreSQL SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html) is for contested work acquisition, not a consistent analytical view.

Atomic units are those in the subsystem documents: activation+outbox; chunk+checkpoint; send attempt+debits; acceptance+progression+next intent; reply+stop+checkpoint; suppression+sources+outbox; receipt+safety hold; consumer effect+marker; invitation+membership. Provider I/O and broker publish occur outside database transactions. Unknown provider acceptance retains evidence and prohibits resend. Deferred foreign keys may be used only for reviewed cyclic pointers during initial insert, never to waive final same-tenant consistency.

Recovery scans due jobs, expired QUEUED claims, unresolved attempts, pending receipts/holds and published-but-uncompleted outbox deliveries. Indexes above directly support those queries. Redis loss never deletes these records. Database outage prevents new sends; backup restore requires outbound restriction and reconciliation of the uncertain interval before re-enabling. Backups/restore drills and recovery of encryption keys/object versions are production prerequisites; RTO/RPO remain owner decisions.

## Retention, scaling and migration sequence

Archive referenced campaigns, leads, templates, mailboxes and conversations. Immutable snapshots/attempts/outcomes/audit cannot cascade with account deletion. Approved erasure may redact content/PII and retain minimal enforcement/evidence; it must not remove suppression then recreate a sendable lead on reimport. Delete temporary upload/error artifacts and expired credential state through explicit policy, preserving necessary digests/dedupe tombstones. Never purge unresolved work. Exact durations by data class require approval before jobs/migrations implement them.

Begin with ordinary indexed PostgreSQL tables and bounded keyset queries. No speculative partitioning/sharding, warehouse, custom roles framework or generalized workflow store. Validate due-query and suppression lookup plans with production-shaped synthetic data before adding indexes. Index every FK used for fanout where its existing unique/PK index does not cover the needed prefix. Enforce aggregate limits through state/locks, not analytics caches.

Approved draft migration sequence: 0001 identity/security → 0002 contacts/content → 0003 mailboxes/campaign snapshots → 0004 messages/events/inbox → 0005 operations/capacity. Apply the full schema chain before runtime traffic. Backend implementation still starts with authentication/bootstrap/authorization and the controlled Gmail milestone before campaign execution; a controlled message has no campaign dependency in its row shape. No forward FK to an absent table is created in an earlier incremental migration; optional later evidence links follow their owning feature. Do not turn the first migration into this entire catalogue. Explain each migration's objects, constraints, RLS, existing-data impact and rollback/recovery; apply shared-environment migrations only on explicit instruction.

## Open Decisions and source conflicts

1. Missing RBAC/ownership policy and misplaced user flows: restore authoritative product files before permission grants or owner-transition constraints.
2. Normalization and cross-workspace account uniqueness: approve conservative proposals before unique constraints become migration history.
3. Activated edits/reopening, window/DST behavior, OOO stopping and SMTP reply requirements: resolve the domain Open Decisions before related API behavior.
4. Retention/token lifetimes/raw evidence access and regional obligations: approve before destruction or production retention configuration.
5. Object storage: PROJECT_CONTEXT recommends Supabase Storage; SYSTEM_ARCHITECTURE recommends S3 or equivalent. Keep object key/version abstraction and preserve the higher-authority Supabase direction pending explicit infrastructure approval; do not provision either here.
6. Runtime: older EC2/Compose versus later ECS/Fargate/managed Redis recommendations are not treated as an approved deployment choice by this task.
7. Alembic references in PROJECT_CONTEXT conflict with explicit newer MVP/SYSTEM_ARCHITECTURE and current task instructions. Supabase migrations only governs; preserve historical source file rather than rewriting it.

## Validation requirements and definition of done

Before migration implementation, review every catalogue entry against its owner subsystem and every Open Decision against product/security. Later tests must prove cross-tenant FK/RLS rejection including inserts/updates, pooled connection context reset after rollback, membership revocation, absent context, worker role isolation, last-owner concurrency after policy approval, unique message/attempt/event keys, outbox crash recovery, safety-hold count repair, absent-address suppression race, sync checkpoint atomicity, Redis-loss accounting and archive/erasure preserving safety. Run query-plan/load tests for due discovery, suppression, recipient fanout, inbox paging and imports.

This phase performed documentation design only. No SQL, migration, schema deployment or application tests are part of this file. Related: [security](../security/SECURITY_ARCHITECTURE.md) and all [architecture documents](../architecture/DOMAIN_MODEL.md).
