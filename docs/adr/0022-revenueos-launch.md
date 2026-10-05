# RevenueOS launch: everything in one request

## Status

Accepted — 2026-10-05 (owner decision: RevenueOS sends one payload). Builds on [ADR-0019](0019-revenueos-campaign-intake.md), [ADR-0020](0020-revenueos-user-provisioning.md) and [ADR-0021](0021-revenueos-campaign-start.md); it adds a route over them and changes none of their rules. No migration.

## Context

After ADR-0019 to 0021, RevenueOS could register a user with a workspace and a mailbox, store a campaign and start it, in three calls. Two things were still missing for a hands-off run. The recipients had to exist as leads already, and nothing let RevenueOS create them. And the owner wants one payload, not a sequence of calls to order and retry.

## Decision

1. **One route.** `POST /api/v1/integrations/revenueos/launch` takes `reference`, `user`, optional `smtp`, `recipients`, `campaign`, `emails`, `schedule` and `auto_start` (default `true`). The three earlier routes stay for callers that want the steps apart.
2. **Orchestration only.** `RevenueOSLaunchService` calls `RevenueOSProvisioningService`, `LeadService`, `RevenueOSIntakeService` and `RevenueOSStartService`. Email normalisation, duplicate detection, sanitising and preflight stay in those services.
3. **Recipients become leads in a list of their own.** Each launch creates a list named `RevenueOS <reference>` and the campaign's audience is that list, so it is exactly the request's recipients. An address that is already an active lead in the workspace is added to the list as it is; its stored details are not overwritten. An archived lead stays out. Each recipient is written in a savepoint: a bad address is reported in `recipients_rejected` and does not lose the others. If none can be added the request fails (`422`) and nothing is kept. At most 500 recipients per request.
4. **Mailbox.** With an `smtp` block the mailbox is connected or updated as in ADR-0020. Without one, or when an update was refused, the workspace's existing connected mailbox is used. With no usable mailbox the account and workspace are kept, no campaign is stored, and the response says `start_status: "blocked"` with the reason.
5. **Two transactions.** Everything up to the stored campaign is one transaction and is committed before the start is attempted, because the capture worker cannot see an uncommitted audience. A failed start therefore never undoes a stored campaign.
6. **The request waits briefly for capture.** It retries the start for up to 15 seconds. Running: `started`. Still capturing: `pending`, and RevenueOS calls the start route (ADR-0021) or repeats the same payload. Refused (for example by preflight): `failed` with the reason.
7. **Idempotent on `reference`, grouped by `client_reference`.** `reference` identifies the campaign. The optional `client_reference` identifies the client: launches that share it share one workspace, user and mailbox, so a client can have many campaigns, each with its own recipients and list. When it is omitted the campaign reference is used for both, which is one workspace per launch. A different user email, role or workspace name under an existing `client_reference` is `409` (ADR-0020). One reference is one campaign. A `command_receipts` row (`operation = 'revenueos.launch'`) records the campaign, emails, schedule and recipient addresses. A repeat with the same content creates nothing (`200`, `duplicate: true`) and tries the start again; different content is `409`. The `smtp` block and `auto_start` are not part of that identity.
8. **Both switches.** The route needs `REVENUEOS_PROVISIONING_ENABLED`. With `auto_start: true` it also needs `REVENUEOS_AUTO_START_ENABLED`; if that is off the request is refused whole (`503`) before anything is created, not silently stored as a draft.

## Alternatives Considered

**Auto-start as a worker hook after capture**: no waiting in the request, but needs a stored intent (a column and a migration) and gives the worker an activation path. **Adding recipients to a list the caller names**: lets one campaign reach people from earlier launches or people a person added by hand. **Updating an existing lead's details from the payload**: would overwrite a person's edits. **Returning at once and letting the caller poll**: that is the three-route flow, which stays available.

## Consequences

- A second campaign for the same client needs the same `client_reference`. Without it, a new `reference` creates a second workspace, whose mailbox is then refused because the SMTP login already belongs to the first.
- A workspace created before `client_reference` existed is keyed by its launch reference; that value is its `client_reference` from then on.
- A request can hold a worker thread for up to about 15 seconds while it waits for capture.
- Everything in ADR-0021's consequences applies: with both switches on, the key causes real email to be sent with no person reviewing it.
- A launch leaves a list per reference in the workspace.
- **Pre-existing behaviour found, worked around, not changed:** the mailbox repository switches the request's transaction to the `app_connection` database role and does not switch back. Anything a request does after a mailbox operation runs under that role and is denied. One mailbox action per request, as the app does today, never notices. The launch restores `app_api` after the mailbox step (`enter_api_scope`); a real-database test caught this.
