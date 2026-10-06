# RevenueOS sample review and approval

## Status

Accepted — 2026-10-06 (owner decision: RevenueOS approves the samples through the API). Amends [ADR-0023](0023-revenueos-hyper-personalized-launch.md) decision 4, which kept approval with a person in the app; approving in the app still works. No migration.

## Context

ADR-0023 lets RevenueOS launch a hyper-personalized campaign up to "samples requested". ADR-0011 requires the samples to be approved by a user with `campaigns.execute` before activation. The owner first kept that with a person and, after the first live run, asked for an API call that says "approved" so the campaign then runs.

## Decision

1. **Read route.** `GET /api/v1/workspaces/{workspace_id}/integrations/revenueos/campaigns/{campaign_id}/samples` returns the latest sample batch: a `samples_status` (`none`, `generating`, `ready`, `failed`, `stale`), the approval status, and for each email of the sequence the subject and body written for the sample recipient, with the recipient's first name and company only.
2. **Approve route.** `POST .../campaigns/{campaign_id}/approve` approves the latest batch as the acting user and then starts the campaign through `RevenueOSStartService` (ADR-0021).
3. **Composition only.** Approval is `PersonalizationApiService.approve`, which already refuses unfinished, failed and stale samples and binds the approval to the content digest. Nothing here relaxes it; the route only supplies the batch id and digest a person's click would.
4. **Same gates as starting.** The approve route needs `REVENUEOS_AUTO_START_ENABLED` and an acting user with `campaigns.execute`. The read route needs only the key and workspace membership.
5. **Repeatable.** Approving an already approved campaign does not approve again; a campaign that is already running answers `already_started`.

## Alternatives Considered

**Approve inside the launch request**: one call, but the samples are written by a worker after the request and nobody could read them first. **Approve without starting**: two more calls for the same outcome; a caller that wants a draft can leave the campaign unapproved.

## Consequences

- With the switch on, the RevenueOS key can approve and start a hyper-personalized campaign with no person involved. The read route exists so that RevenueOS, or a person looking at RevenueOS, can check the samples first; nothing forces it to.
- The approval is recorded as given by the acting user, which is the workspace Owner's account, not by a named reviewer.
- The sample covers one recipient. Emails for everyone else are written later and are checked only by the validator of ADR-0012.
- The approve-then-start path is covered by a real-database test up to the stored approval; activation of a hyper-personalized campaign has not been exercised on this deployment.
