# Stateless unsubscribe, default mailbox limits and automatic mailbox protection

## Status

Accepted. Go-live work for the first real campaigns. Migration 0038 is prepared and must be reviewed and applied by the project owner; this ADR does not apply it.

## Context

A review of the send path before the first real campaign found five gaps:

1. **Nothing put an unsubscribe link or `List-Unsubscribe` header in outgoing mail.** The public endpoint existed, but nothing generated a token for it. It also could not have worked on the real database: it ran as `app_api` with no user, whose `suppressions` insert policy needs `app_has_permission(...)`, and the event repository set a GUC (`app.current_workspace_id`) that the row-level-security functions never read (`app.workspace_id`). Unit tests passed only because SQLite has no row-level security.
2. **No sending caps or pacing.** No rate-policy rows existed, a campaign's `daily_limit` was stored but never read, every first email got the same `due_at`, and a rate-limit denial left the message `QUEUED` until its claim lease expired, then claimed it again at once.
3. **No automatic protection.** The dashboard warned at 2% and 5% bounce rate, but nothing stopped a mailbox, and there was no way to release a safety hold.
4. **Production state and CI were unverified.** The CI workflow had a YAML error and never ran a real-database test.
5. The plain-text alternative of every email was a placeholder.

## Decision

### Unsubscribe

1. **A stateless signed token.** `u1.` + workspace id + message id + a truncated HMAC-SHA256 under `UNSUBSCRIBE_SIGNING_KEY` (own context string, same construction as the open-tracking token). It identifies a message, not an address; the address is read from the message at unsubscribe time, so a link can only suppress its own recipient in its own workspace. No expiry: a link must work as long as the email exists. Nothing is stored at send time, which matters because each extra commit costs a database connection (NullPool). Tokens stored by the earlier design keep working. The key must never be rotated casually.
2. **One choke point.** `SendingService._compose_outbound` adds the footer (unsubscribe link and the workspace's postal address), the `List-Unsubscribe` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers (RFC 8058), a real text alternative and, if enabled, the open pixel, to every CAMPAIGN message at send time. Render-time injection was rejected: there are five `render_step_content` call sites (standard, progression, just-in-time personalization, ...), and one missed path would send an email with no opt-out. The frozen, digest-verified body is never changed. Controlled test sends have no footer.
3. **Fail closed.** A campaign message is deferred (`unsubscribe_not_configured`) before any capacity is reserved when the unsubscribe settings are missing. A campaign cannot be activated without them (`unsubscribe_not_configured`). The workspace postal address and footer message are optional (the product owner dropped the original `postal_address_required` activation check on 2026-10-08); the footer is added with whichever of them are set. In production with `SENDING_WORKER_ENABLED=true` the backend refuses to start without them, and without a real `PLATFORM_OPERATOR_EMAILS`.
4. **The endpoint.** The signature is verified before a database connection is taken, so forged requests cost nothing. GET shows a confirmation page and never unsubscribes (link scanners fetch every URL). POST suppresses and also accepts the one-click form post. The side effects run as `app_worker_general` scoped to the token's workspace through `enter_worker_scope`, since the recipient is anonymous. `EventRepository` now sets `app.workspace_id`.
5. **Microsoft Graph** mailboxes carry the footer link only for now: Graph's JSON send path rejects non-`x-` headers. Sending those through raw MIME is a later change. Gmail API and SMTP carry the headers.

### Default mailbox limits

6. **Every mailbox has a limit.** One MAILBOX-kind `tenant_rate_policies` row: 50 messages per rolling 24 hours, at least 60 seconds apart (`MAILBOX_DEFAULT_*`, editable 1-500 per day and 0-3600 s by `mailboxes.manage`). Created in the connect transaction by `app_connection`, backfilled for existing mailboxes by migration 0038. The default is intentionally conservative: a new mailbox with no history is judged harshly. The numbers are duplicated in the migration and the settings; the migration backfill is one-time and does not follow later setting changes.
7. **Absence is not unlimited.** A CAMPAIGN message whose mailbox has no MAILBOX policy with a window of at least an hour is deferred (`mailbox_limits_missing`). The capacity-debit foreign key needs real policy rows, so synthetic policies were not an option.
8. **A denial reschedules.** `SendingRepository.defer_for_capacity` returns the claimed message to its origin status with a later `due_at` (30 s to 24 h from the limiter's retry hint, plus jitter; 15 minutes for a held or limit-less mailbox), only if no attempt exists, and bumps the dispatch generation so that a late duplicate of the deferring task is treated as stale (at the old generation it would find the message `SCHEDULED`, fail the state gate and be skipped permanently). It uses the columns `app_worker_send` can already update; no outbox change is needed because each claim opens a new work item (`{message}:{dispatch_generation}`).
9. **Planning spreads the load.** `campaigns/pacing.py` gives each first email its own time from its place in its mailbox's queue (`(capture_ordinal - 1) // mailboxes`, matching the mailbox assignment), the mailbox's daily cap and spacing, and the campaign's `daily_limit` shared across its mailboxes. The limiter stays the authority. Gaps in `capture_ordinal` (members that were not accepted) leave a few slots empty; a dense per-mailbox rank would need a schema change and was not worth it.
10. **Backpressure.** The scheduler claims at most `SCHEDULER_MAX_OUTSTANDING_CLAIMS` minus the messages already `QUEUED`.

### Automatic protection

11. **Hold the mailbox, not the workspace.** After each reply-sync visit, a mailbox whose bounce rate over the last 7 days is above 5% with at least 20 emails sent gets a safety hold (`auto-bounce:{mailbox}:{utc-date}`, once per day, and not while an earlier automatic hold is still active). The existing send gate already refuses to send while a hold is active. Thresholds (2% / 5% / 20 sends; complaints 0.1% / 50) live in `safety/thresholds.py` and are shared with the dashboard.
12. **A person releases it.** `POST /mailboxes/{id}/safety-holds/{hold}/release` (`mailboxes.manage`, audited), through a new `app_api` policy that only lets a hold go from `ACTIVE` to `RESOLVED`. A release lasts at least until the next UTC day; if the rate is still critical then, the mailbox is held again. Nothing releases an automatic hold by itself.
13. **Not done.** Spam complaints have no data source until provider feedback loops or webhooks exist, so complaints are shown but not acted on. Mailboxes without reply detection (an SMTP mailbox with no IMAP settings) never see bounces, so they are never auto-held. There is no in-app notification: the existing notifications module only lists notifications and no worker role may create them without a further migration; the dashboard and the mailbox page show active holds.

### Verification

14. CI now parses (the `DATABASE_URL` values are quoted) and has a job that runs the real-PostgreSQL suites on a throwaway database (`pgserver`) with every migration applied. `python -m app.core.preflight` is a read-only go-live check.

## Consequences

- The deploy order is: review and apply migration 0038, then deploy the code. Code deployed first fails safe (nothing sends without a limit).
- `UNSUBSCRIBE_BASE_URL` and `UNSUBSCRIBE_SIGNING_KEY` are new required settings; losing or rotating the key breaks links in emails already sent.
- A first campaign is slow on purpose: 50 a day per mailbox. Several mailboxes multiply it; raise a limit only as a mailbox builds a reputation.
- Provider behaviour (Gmail and Graph accepting the headers and footer, bounces being detected, replies being matched) can only be proven against live mailboxes; the rehearsal in `docs/operations/DEPLOYMENT.md` covers it.
