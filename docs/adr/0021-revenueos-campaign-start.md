# RevenueOS campaign start

## Status

Accepted — 2026-10-05 (owner decision: campaigns sent by RevenueOS should start without a person). Amends [ADR-0019](0019-revenueos-campaign-intake.md), which kept starting manual and recommended a Member-role acting user. Builds on its key and acting user. No migration.

## Context

ADR-0019 stores a draft and stops: a person opens the campaign, confirms the audience and starts it. With accounts, workspaces and SMTP mailboxes now provisioned by RevenueOS ([ADR-0020](0020-revenueos-user-provisioning.md)), that click is the last manual step, and the owner wants it removed for this internal deployment.

Starting cannot happen inside the intake request. The audience is captured by a worker task after that request commits, and only a READY audience can be committed and activated.

## Decision

1. **A second route, not a flag on intake.** `POST /api/v1/workspaces/{workspace_id}/integrations/revenueos/campaigns/{campaign_id}/start`. RevenueOS calls it after intake, with the `campaign_id` intake returned.
2. **Composition only.** `RevenueOSStartService` calls `AudienceService.commit_audience` and `CampaignActivationService.activate`, the two calls the Review page makes. Preflight, the frozen sequence, planning and idempotency stay in those services.
3. **Asynchronous capture is the caller's retry.** While the audience is CAPTURING the route answers `409 audience_not_ready`; RevenueOS retries. A FAILED or ABANDONED capture is `422`.
4. **Safe to repeat.** A campaign that is already RUNNING or SCHEDULED answers `200` with `already_started` and changes nothing.
5. **Never overrides a person.** A PAUSED, ERROR, COMPLETED or ARCHIVED campaign is `409`. The route does not resume, and there is no pause or stop route.
6. **Same permission as the app.** The acting user needs `campaigns.execute` (Manager, Admin or Owner) in that workspace; a Member-role acting user gets `403`. The role is read from the current membership, as on every route.
7. **Its own switch.** `REVENUEOS_AUTO_START_ENABLED`, off by default; otherwise `503` before any database work.
8. **Sends on the campaign's own terms.** No `start_at` is passed, so the campaign runs from now within the schedule window, weekdays and daily limit that came with the intake payload.

## Alternatives Considered

**`auto_start: true` on the intake payload, acted on by the capture worker**: one call for RevenueOS, but the intent would need a new column and the worker would gain an activation path; a larger change with a schema migration. **Waiting for capture inside the intake request**: holds a request on a worker. **Leaving the acting user a Member and bypassing the permission**: would put a second, weaker activation rule in the code.

## Consequences

- With the switch on and a Manager-or-higher acting user, the RevenueOS key can cause real email to be sent to real people with no person reviewing the audience or the text. A wrong list or a bad email is not caught before it goes out.
- The limits that remain are the campaign's schedule and `daily_limit`, mailbox health and rate control, suppression lists, preflight, and `SENDING_WORKER_ENABLED` on the server.
- Nothing lets RevenueOS stop a campaign it started. A person pauses it in the app.
- Follow-up emails still need `SEQUENCE_PROGRESSION_ENABLED` (ADR-0019).
- RevenueOS has to wait for capture between the two calls; in practice a retry after a second or two.
