# CONTEXT.md — Project Briefing for Claude

Orientation for an AI coding agent working in this repository. Read this first, then
follow the pointers. It is a map, not a spec: the documents under `docs/` are the
source of truth, and this file deliberately links to them instead of copying them.

Snapshot date: **2026-10-07**. Anything under "Current state" can go stale; verify it
against `git log`, `supabase/migrations/`, and the user before relying on it.

Reading order for any task: `AGENTS.md` (authoritative engineering rules) →
`CLAUDE.md` (how Claude operates here) → this file → the docs for the area you are
touching → the existing code and tests.

---

## 1. What this product is

**Outly** — a multi-tenant SaaS for cold / outbound email outreach (references:
Instantly, Saleshandy, Lemlist; not a clone). Customers connect their own sending
mailboxes (Gmail, Microsoft 365, or custom SMTP/IMAP), import leads, build email
sequences, run campaigns, receive and manage replies, and watch deliverability and
analytics. Production runs at `https://outly.b2botix.ai`.

Primary journey:

```text
sign up → create workspace → connect mailbox → import leads → create campaign
→ audience → sequence (Email → Wait → Email …) → senders → schedule → review
→ activate → scheduler plans + dispatches → workers send → events tracked
→ replies sync into the unified inbox → analytics / deliverability
```

Two campaign types (immutable once created, [ADR-0011](docs/adr/0011-hyper-personalized-campaign-type.md)):

- **STANDARD** — fixed templates with `{{variables}}`.
- **HYPER_PERSONALIZED** — an LLM writes each lead's email just-in-time (~60 min before
  its due time) from a per-step objective + reference email, lead facts, and optional
  company-website research. Sample previews and a manager approval gate activation.
  Same engine, state machines and send path as STANDARD; only the way a message's
  content snapshot is produced differs. Flag-gated (`PERSONALIZATION_ENABLED`).

Product docs: `docs/product/` — `PROJECT_CONTEXT.md` (long master context, 4k lines,
read selectively), `MVP.md`, `PAGE_MAP.md`, `USER_FLOWS.md` (UF-01…UF-37),
`USER_ROLES.md` (**the approved permission matrix**).

---

## 2. Tech stack and runtime topology

| Layer | Technology |
|---|---|
| Backend API | Python 3.12, FastAPI, SQLAlchemy 2 (Core/text SQL, **no ORM schema creation**), psycopg 3, pydantic-settings, PyJWT |
| Workers | Celery + Redis broker, plus standalone polling entrypoints (`workers/`) |
| Database / Auth / Storage | **Supabase Cloud** PostgreSQL (session pooler), Supabase Auth (JWT), Supabase Storage (private buckets: `imports`, `email-attachments`) |
| Frontend | Next.js 15 (App Router) + React 19, TypeScript, Tailwind 3, TanStack Query, react-hook-form + zod, TipTap email editor, vitest + Testing Library |
| Packaging | Docker Compose — a **single** `docker-compose.yml` (production stack; also works locally; never pass `-f`) |
| Prod proxy | Host Caddy on a shared VPS: `/api/v1/*` → backend, everything else → frontend; `/api/v1/webhooks/*` deliberately returns 403 |
| CI | `.github/workflows/ci.yml`: backend (SQLite unit tests), backend real-Postgres (`pgserver`), workers, frontend |

Compose services (9): `redis`, `backend` (API), `frontend`, `scheduler`,
`rate-controller`, `worker-general` (queues `maintenance,imports,campaigns`),
`worker-events` (`webhooks`), `worker-send` (`email.send`), `worker-sync`
(`mailbox.sync`), and `worker-personalization` (flag-gated, `personalization` queue,
holds the LLM key). PostgreSQL is **external** (`DATABASE_URL`). Redis is transient by
design.

Prod ports (host-bound to 127.0.0.1): API 4130, web 4131. Local default 8000 / 3000.

---

## 3. Repository map

```text
AGENTS.md  CLAUDE.md  README.md  CONTEXT.md   rules + onboarding
docker-compose.yml  .env.example               single stack; env template (never commit .env)
backend/
  app/
    main.py                  FastAPI app
    api/deps.py              get_db, get_verified_principal, get_workspace_context, get_platform_operator
    api/v1/*.py              thin routers (one per module) → api/v1/router.py
    core/                    config, auth (JWT), permissions, crypto, errors, logging, audit, preflight
    db/                      session, context (RLS/role scoping), health
    modules/<domain>/        business logic: service.py + repository.py (+ schemas.py)
    schemas/  services/      shared pydantic models; task_dispatch, redis_health
  tests/                     ~90 files; tests/support/ = real-Postgres harness; tests/integration/ = LIVE Supabase (never run)
workers/                     Celery app + tasks, scheduler, rate_controller, send/sync/personalization entrypoints, workers/tests/
frontend/
  app/auth, app/onboarding, app/app/**   routes (dashboard, campaigns/[id]/{overview,audience,sequence,senders,schedule,review,analytics,personalization}, leads, inbox, mailboxes, deliverability, analytics, team, notifications, settings)
  components/{ui,campaigns,email-editor,leads,mailboxes,workspace,brand}
  lib/*-api.ts               typed API clients; api-client.ts, permissions.ts (mirror of backend), workspace-context.tsx, supabase/*
  middleware.ts              Supabase session refresh
supabase/migrations/         0001…0038 — the ONLY schema source
scripts/check_migrations.py  static migration checker
docs/{product,architecture,database,security,operations,adr}
infrastructure/vps/          Caddyfile snippet
```

Backend domain modules under `backend/app/modules/`: `campaigns` (activation, audience,
sequence, scheduling, pacing, progression, rendering, preflight, review), `sending`
(send service, gates, retry policy, reconciliation, recovery), `scheduler`
(due discovery, claims, outbox), `mailboxes` (+ `providers/` Gmail, Microsoft Graph,
SMTP/IMAP), `rate_limit` (Redis Lua token buckets), `events` (provider receipts,
outbox, consumers, adapters), `replies`, `inbox`, `tracking`, `unsubscribe`,
`suppression`, `safety` (auto-holds), `leads`, `imports`, `templates`,
`personalization` (LLM port, prompts, validators, research/egress, brand kit),
`analytics`, `notifications`, `team`, `usage`, `admin`, `bulk`, `erasure`.

---

## 4. Architecture in one page

Authoritative: [SYSTEM_ARCHITECTURE](docs/architecture/SYSTEM_ARCHITECTURE.md) (3.9k
lines — use its headings, don't read it whole) and the ADRs in `docs/adr/` (0001–0019).

1. **Modular monolith** ([ADR-0001](docs/adr/0001-modular-monolith.md)). One backend
   package; workers are separate entrypoints into the *same* code. Workers never
   reimplement campaign, suppression, permission or provider rules — they call the
   domain services.
2. **PostgreSQL is the source of truth for all durable state and work intent;
   Redis/Celery is transport only** ([ADR-0004](docs/adr/0004-postgresql-durable-redis-transient.md)).
   If Redis is flushed, work is rediscovered from the database. Mutation + domain
   event + outbox row commit in one transaction; a relay publishes the outbox to Celery.
3. **Durable message scheduling** ([ADR-0007](docs/adr/0007-durable-message-scheduling.md)).
   Future emails are `messages` rows with `due_at`, never day-long Celery ETAs. The
   scheduler claims due rows (`FOR UPDATE SKIP LOCKED`), sets `QUEUED` with a
   `dispatch_generation`, and writes an outbox item. See `SCHEDULER.md`, `QUEUES.md`, `WORKERS.md`.
4. **Message state machine** (`MESSAGE_STATE_MACHINE.md`):
   `PLANNED → SCHEDULED → QUEUED → SENDING → SENT | RETRY_SCHEDULED | UNKNOWN_OUTCOME | FAILED | SKIPPED | CANCELLED`.
   Only a newly authorized `SENDING` attempt may call a provider. The authorization
   transaction re-checks every gate (campaign RUNNING, enrollment ACTIVE, suppression,
   reply facts, mailbox health + credential generation, window, capacity, no safety hold).
   **`UNKNOWN_OUTCOME` is never auto-resent** — it is reconciled. `SENT` means provider
   accepted, not delivered.
5. **Campaign state machine** (`CAMPAIGN_STATE_MACHINE.md`):
   `DRAFT, SCHEDULED, RUNNING, PAUSED, ERROR, COMPLETED, ARCHIVED`. Users issue
   commands (with expected version + idempotency key); they never set status.
   Activation freezes audience + sequence (immutable afterwards; duplicate instead of
   editing). No reopening COMPLETED/ARCHIVED.
6. **Follow-ups** are created by a level-triggered sweeper
   ([ADR-0009](docs/adr/0009-sequence-progression-sweeper.md), `SEQUENCE_PROGRESSION_ENABLED`),
   not inside the send transaction.
7. **Provider abstraction** ([ADR-0006](docs/adr/0006-provider-abstraction.md),
   `PROVIDER_ARCHITECTURE.md`): Gmail API, Microsoft Graph, SMTP/IMAP behind one port;
   OAuth tokens/passwords stored as authenticated-encryption envelopes
   (`MAILBOX_ENCRYPTION_KEY[_ID]`); SMTP/IMAP egress is SSRF-hardened.
8. **Rate limiting** (`RATE_LIMITING.md`): Redis Lua token buckets reserved before
   send, debited durably; rebuilt from PostgreSQL after Redis loss. Per-mailbox default
   cap is fail-closed (no cap ⇒ no send), [ADR-0019](docs/adr/0019-stateless-unsubscribe-and-default-mailbox-limits.md).
9. **Events/replies/suppression** (`EVENT_SYSTEM.md`, `REPLY_SYNC.md`, `SUPPRESSION.md`):
   provider receipts deduplicated → normalized effects; replies and bounces stop
   sequences; suppression is permanent per (workspace, address, reason) and never
   removed by imports, list edits or deletions. Reply sync is polling (Gmail/Graph/IMAP);
   push notifications are not implemented. Message-level open tracking
   ([ADR-0014](docs/adr/0014-message-level-tracking-events.md)); signed stateless unsubscribe
   ([ADR-0019](docs/adr/0019-stateless-unsubscribe-and-default-mailbox-limits.md)).
10. **Archive / purge / erasure / hard delete**
    ([ADR-0015](docs/adr/0015-archive-purge-and-erasure.md)): archive is a state, not a
    deletion; destructive commands are `SECURITY DEFINER` SQL functions, ADMIN/OWNER only,
    typed-confirmation, suppressions never deleted.

Personalization/AI specifics: ADRs 0011–0013 (campaign type, LLM port + data handling,
website research + outbound fetch), 0016 (API-process AI drafting + company analysis),
0017 (branded HTML email + sanitizer profile), 0018 (email craft rules + sequence playbook).
LLM provider is OpenAI via raw `httpx` behind a port; model name comes from env;
field allow-list means email/phone/LinkedIn are never sent to the model.

---

## 5. Multi-tenancy, auth and database security (the part that is easy to break)

- **Tenant root = `workspaces`.** Every tenant table carries `workspace_id`; composite
  `(workspace_id, id)` foreign keys prevent cross-tenant references. An ID alone never
  grants access; cross-tenant access must fail and tests should prove it
  (`docs/database/DATABASE.md`, `docs/security/SECURITY_ARCHITECTURE.md`).
- **Auth:** Supabase Auth issues the JWT; the backend verifies it
  (`core/auth.py`) and then resolves **current membership from the database** — role
  claims in the token are never trusted. Flow: identity → active membership → capability
  → composite workspace/resource lookup → lifecycle guard.
- **Roles:** `VIEWER < MEMBER < MANAGER < ADMIN < OWNER`. The matrix lives in
  `docs/product/USER_ROLES.md`, in `backend/app/core/permissions.py`, in the SQL function
  `app_has_permission()` (migration 0001) and in `frontend/lib/permissions.ts` — all
  must stay in sync. Unknown capability ⇒ deny. Frontend visibility is UX, not authorization.
- **RLS is enforced and forced.** The API connects as a non-owner, non-BYPASSRLS role;
  `get_db` does `BEGIN → SET LOCAL ROLE app_api → set_config('app.user_id'/'app.workspace_id', …, true)`
  (`db/context.py`). Context is **transaction-local**, so after any `commit()` the role
  and context must be re-established (`enter_api_scope`, `enter_worker_scope`).
  The API never uses the service-role key for ordinary requests.
- **Least-privilege DB roles** (`DATABASE.md`): `app_api`, `app_connection`,
  `app_worker_general`, `app_worker_send`, `app_worker_sync`, `app_scheduler`,
  `app_rate_controller`, plus `app_erasure`. They have **column-level** INSERT/UPDATE
  grants: a new column needs explicit grants, or the write fails with
  `InsufficientPrivilege` — this is the most common cause of "works on SQLite, fails on
  Postgres". Cross-tenant discovery (scheduler, platform suppression, rate aggregation)
  goes through narrow `SECURITY DEFINER` functions with fixed `search_path` and revoked
  `PUBLIC` execute.
- **Secrets:** never log, return, commit or embed. API DTOs, task payloads, logs and
  errors carry no tokens/passwords/bodies. Browser holds only the Supabase anon key.
- **Platform operators** (`PLATFORM_OPERATOR_EMAILS`, `/admin`) are a separate authority
  from workspace roles.
- **Unit tests run on SQLite** (no RLS/roles/grants). Anything touching SQL, grants,
  RLS or worker SQL must also be verified on real Postgres — see §8.

---

## 6. Database and migrations

- Migrations live **only** in `supabase/migrations/` (`0001_initial.sql` … `0038_golive_limits_and_hold_release.sql`).
  No Alembic, no SQLAlchemy auto-create. `python scripts/check_migrations.py` is the static checker.
- **Creating SQL is not permission to apply it.** Prepare a migration, give the review
  from `CLAUDE.md` §7 (Migration / Purpose / Creates / Modifies / Deletes / Constraints /
  Indexes / RLS / Functions / Existing-data impact / Application impact / Risk / Rollback),
  and wait for explicit approval. The user applies migrations themselves (usually via
  the Supabase dashboard). Never edit a migration that may be applied; add a new one.
- **Deploy order matters:** apply the migration *before* deploying code that
  selects/writes its new columns, otherwise sends/requests fail with permission or
  missing-column errors.
- Schema reference: `docs/database/DATABASE.md` (per-table contracts) and
  `docs/database/MIGRATION_REVIEW.md`. Domain concepts: `docs/architecture/DOMAIN_MODEL.md`.

Migration groups (for orientation, not a substitute for reading them):
0001–0005 foundation (identity, leads/templates, mailboxes/campaigns, messages/events/inbox,
platform) · 0006–0020 bootstrap, imports, scheduler, send authorization, recovery,
reply sync, inbox, analytics, team/admin · 0021–0023 lead profile fields, frozen-variable
cap · 0024–0025 step pre-header + attachments · 0026–0029 hyper-personalization ·
0030–0032 tracking/reply-sync grants · 0033–0036 archive/purge/erasure, workspace
delete, campaign+mailbox hard delete · 0037 deliverability read grants · 0038 go-live
mailbox limits + hold release.

---

## 7. Current state (verify before relying on it)

Facts gathered from project notes up to 2026-10-07. The git tree was clean at the start
of this session and recent commits cover the work below, so older "uncommitted" notes
are likely now committed — confirm with `git log`.

- **Production:** runs on a shared Hostinger VPS with Supabase Cloud (project ref in
  the user's notes, not here). **Real sending has been OFF** (`SENDING_WORKER_ENABLED=false`)
  pending the owner's own-mailbox verification. No live Gmail/Graph/SMTP end-to-end
  rehearsal has been done; the runbook is in `docs/operations/DEPLOYMENT.md`.
- **Migrations applied to prod are not tracked in the repo.** Known: 0001–0019 applied
  by 2026-09-25; the user later said 0033 was applied; 0034 unconfirmed; **0038 was
  prepared and not applied**. Ask the user (or inspect via the Supabase tools only if
  they ask you to) before assuming any migration ≥0020 is live.
- **Go-live hardening (0038, [ADR-0019](docs/adr/0019-stateless-unsubscribe-and-default-mailbox-limits.md)):**
  signed unsubscribe + footer/List-Unsubscribe headers, per-mailbox daily caps and
  spacing (fail-closed), capacity deferral, planning-time pacing, scheduler backpressure,
  auto-hold at >5% bounce over 7 days (≥20 sends), hold list/release API + UI, read-only
  `python -m app.core.preflight`. Production with sending enabled refuses to start
  without `UNSUBSCRIBE_BASE_URL`, `UNSUBSCRIBE_SIGNING_KEY` (never rotate — links in sent
  mail break) and a real `PLATFORM_OPERATOR_EMAILS`.
- **Feature flags default OFF:** `SENDING_WORKER_ENABLED`, `SEQUENCE_PROGRESSION_ENABLED`
  (turning it on makes running campaigns send step 2), `PERSONALIZATION_ENABLED`,
  `OPEN_TRACKING_ENABLED`.
- **Known defects found in a 2026-10-07 read-only scale audit, not fixed:** RESTRICTED
  workspace not checked by send gates/scheduler; messages of a disconnected mailbox
  starve other tenants of scheduler claim slots; reconciliation re-polls the same 50
  `UNKNOWN_OUTCOME` rows every tick with no backoff; non-timeout `httpx.TransportError`
  after a Gmail/Graph send is treated as definitively rejected (possible duplicate
  email); no send-time window gate for resume/retries; `circuit_state` is never set;
  metrics are in-process only; scheduler `recover_expired_claim` doesn't bump
  `dispatch_generation`. Re-verify against current code before fixing.
- **Other limitations:** SMTP mailboxes without IMAP never see replies/bounces; Graph
  mailboxes get the unsubscribe footer link but no header; complaints are not measured;
  no Gmail Pub/Sub / Graph subscriptions (polling only); provider webhook endpoints are
  intentionally blocked at the proxy; OpenAI data-retention terms and robots.txt policy
  for website research are open owner decisions.
- **Baselines that already fail (not yours to "fix" casually):** repo-wide `ruff`
  (~555 errors) and `mypy app` (~52) — CI marks them `continue-on-error`;
  `tests/static test_reviewed_chain` fails (migration-checker errors);
  `tests/integration/*` need the live Supabase project.

---

## 8. Build, run, test

Windows host; PowerShell primary, Git Bash available. Use `npm.cmd` in PowerShell.

```powershell
# Backend
cd backend; python -m pip install -e ".[dev]"
python -m uvicorn app.main:app --reload
python -m pytest --ignore=tests/integration      # NEVER run tests/integration (hits the live project)
python -m ruff check . ; python -m mypy app

# Workers (from repo root)
$env:PYTHONPATH=".;backend"
celery -A workers.celery_app:celery_app worker --loglevel=INFO --queues=maintenance
python -m workers.scheduler

# Frontend
cd frontend; npm.cmd install; npm.cmd run dev
npm.cmd run lint; npm.cmd run typecheck; npm.cmd run test; npm.cmd run build

# Stack / migrations check
docker compose config ; python scripts/check_migrations.py
```

Test running gotchas (hard-won):

- With the local `.env`, run backend tests as
  `PERSONALIZATION_ENABLED=false PERSONALIZATION_OPENAI_API_KEY= PERSONALIZATION_MODEL= python -m pytest --ignore=tests/integration`
  (full suite ≈ 3.5 min). Worker tests: from repo root with `PYTHONPATH=backend:.`.
- **Real-Postgres harness** (no Docker): `backend/tests/support/real_pg.py`
  (`throwaway_database()` applies every migration to a throwaway `pgserver` Postgres) and
  `real_seed.py` (`seed_world`). Tests are `backend/tests/test_*real_db.py`
  (e.g. `cd backend && python -m pytest tests/test_personalization_real_db.py`, ~20 s). Use it
  to verify RLS, grants, triggers and worker SQL **before** asking the user to apply anything.
  Act as a role via `SET LOCAL ROLE app_api|app_worker_general|…` plus the `app.*` settings.
  If the C: drive is full pgserver dies with `DiskFull`; point `TMP`/`TEMP` at another drive.
  Stop leftover `postgres.exe` processes when done.
- Under full-suite CPU load a few frontend tests (leads page, `email-step-dialog`) time out
  but pass alone. `tsc` rewrites tracked `frontend/tsconfig.tsbuildinfo` (revert it); a stale
  `frontend/.next` breaks `tsc` (delete it).
- Long quoted heredocs and `/tmp` paths misbehave in this shell on Windows; write scripts
  with the Write tool into the scratchpad and run them.
- Never send real email, call real OpenAI, or touch the production-linked Supabase project
  during tests; use fakes (`fake_model.py`, fake providers, fakeredis).

---

## 9. Working agreements specific to this user

- **Never connect to the production VPS** — no SSH, scp, docker, or file reads there, even
  read-only. Diagnose from the repo and public HTTP GETs; for server actions, write exact
  commands for the user to run and let them paste back output. (The server is shared with
  ~45 other projects; mistakes can hurt unrelated services.)
- **Never apply migrations** or execute any irreversible/external action unless explicitly told.
  State plainly the difference between *prepared*, *tested*, *applied*, *deployed* — e.g.
  "Migration created — not applied."
- Keep changes narrow; report unrelated issues separately rather than fixing them.
- Don't hide failures, silence errors, delete failing tests, or fabricate "tests passed".
- Update the one authoritative doc when behavior changes; record architecture decisions as
  new ADRs in `docs/adr/` (after the decision is approved, not after the code is written).
- The user is the owner of product decisions. When a decision is genuinely absent and
  affects architecture, surface it as a question instead of embedding a default.
- Finish meaningful tasks with the completion report from `AGENTS.md` §32 (Implemented /
  Files changed / Database changes / Config changes / Tests run / Decisions / Limitations).

---

## 10. Where to look for…

| Question | Start here |
|---|---|
| What should this feature do? | `docs/product/MVP.md`, `PAGE_MAP.md`, `USER_FLOWS.md` |
| Who may do X? | `docs/product/USER_ROLES.md` → `core/permissions.py` |
| Table/column/grant/RLS contract | `docs/database/DATABASE.md`, then the migration |
| Why was it designed this way? | `docs/adr/` (read the ADR for that area) |
| How does a campaign activate/plan/send? | `CAMPAIGN_ENGINE.md`, `modules/campaigns/activation_service.py`, `modules/sending/service.py`, `modules/scheduler/` |
| Why didn't a message send? | `MESSAGE_STATE_MACHINE.md`, `modules/sending/gates.py`, `sending/retry_policy.py` |
| Replies / bounces / inbox | `REPLY_SYNC.md`, `modules/replies`, `modules/events`, `modules/inbox` |
| Queues and worker groups | `QUEUES.md`, `WORKERS.md`, `docker-compose.yml`, `workers/` |
| Deploying / env vars / runbooks | `docs/operations/DEPLOYMENT.md`, `.env.example` |
| Security model | `docs/security/SECURITY_ARCHITECTURE.md` |
| A frontend page or its API client | `frontend/app/app/<area>/`, `frontend/lib/<area>-api.ts` |
| AI drafting / personalization | `modules/personalization/`, ADRs 0011–0013, 0016–0018 |

If code and documentation disagree, say so and ask — do not silently pick one
(`AGENTS.md` §2, §27).
