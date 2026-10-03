# RevenueOS campaign intake

## Status

Accepted — 2026-10-03 (owner approved the plan). Does not amend an earlier ADR. The general public API, API keys and customer-facing webhooks stay deferred ([USER_ROLES.md](../product/USER_ROLES.md)); this is one purpose-built route for one first-party caller.

## Context

The platform is one tool inside the owner's larger RevenueOS product. RevenueOS ("Brain") holds the knowledge about a client company and decides what an email campaign should say and to whom. It needs to hand a finished campaign to this platform without a person re-entering it, and to get proof that it was stored.

Today a campaign is built through five separate route groups (campaign, sequence steps, settings, mailbox assignment, audience). A caller that drives them one by one can leave a half-built campaign behind on any failure, and `POST /campaigns` requires an `Idempotency-Key` it does not use, so a retried create produces a second campaign.

The owner's constraints: STANDARD campaigns first; the leads already exist in this platform; mailboxes are connected by a person; a person still starts the campaign; the route is for RevenueOS only.

## Decision

1. **One route.** `POST /api/v1/workspaces/{workspace_id}/integrations/revenueos/campaigns` accepts a campaign name and description, the emails in order with the days to wait before each, the schedule, the audience (`list_ids` / `lead_ids` of existing leads) and one `mailbox_id`, plus RevenueOS's own `reference`.
2. **Composition, not a second implementation.** `RevenueOSIntakeService` (`app/modules/integrations/revenueos_intake.py`) calls `CampaignService`, `SequenceService`, `CampaignSettingsService`, `CampaignMailboxService` and `AudienceService`. Sanitizing, variable validation, workspace scoping of ids and DRAFT-only rules stay in those services.
3. **Always a STANDARD draft.** The route cannot choose a campaign type and never activates. Hyper-personalized intake is out of scope here.
4. **All or nothing.** The whole package is written in the request's single transaction. The audience capture task is enqueued last, so a rejected part rolls everything back and leaves no task pointing at nothing.
5. **Idempotent on `reference`, acknowledged in the database.** A `command_receipts` row (`operation = 'revenueos.campaign_intake'`, `request_key = reference`, `resource = campaign`) is written in the same transaction. A repeat of the same reference with the same content creates nothing and returns the stored campaign (`200`, `duplicate: true`); the same reference with different content is `409`. A receipt whose campaign was later removed is also `409`: an acknowledged reference is never silently recreated.
6. **Key-only authentication (owner decision, 2026-10-03).** This deployment serves RevenueOS only, so the route does not take a Supabase bearer token. The caller sends `X-RevenueOS-Key`, compared in constant time with `REVENUEOS_INTAKE_KEY` (32 characters minimum, enforced at boot). The key proves the caller is RevenueOS and carries no identity.
7. **A configured acting user; authorization unchanged.** RLS, `creator_id` and receipts need a real user, so the route acts as `REVENUEOS_ACTOR_USER_ID`, read from server configuration and never from the request. The route binds that user and the URL's workspace to the transaction and then applies the same checks as every other route: the role comes from the user's current ACTIVE membership under RLS, a workspace the user does not belong to is `404`, and `campaigns.draft` is required. The acting user should hold the Member role, which cannot activate campaigns or manage mailboxes. With either setting missing the route answers `503`. This is the only route that authenticates this way.
8. **No schema change.** `command_receipts` and its `app_api` grants already allow this. No migration.

## What stays manual

Audience capture is asynchronous, so the audience is not committed by this route. A person opens the campaign, confirms the audience, and starts it. Preflight keeps reporting `audience_not_selected` until then.

## Alternatives Considered

**An orchestrator (n8n) calling the five existing route groups**: no code here, but no atomicity and no usable idempotency on create. **A Supabase bearer token plus the key** (the first version of this ADR): two factors, but RevenueOS must sign in and refresh a token about hourly; the owner chose the simpler key-only form. **Machine API keys with scopes**: the right long-term public API, but needs new tables, an RLS design for a non-user principal and a migration; deferred. **A shared secret with no user at all**: RLS and `creator_id` need a real profile; an invented service identity would bypass the role model. **Committing the audience from this route**: would require waiting on a worker inside a request.

## Consequences

- The key is the route's only credential and is one value for every workspace. Whoever holds it can create draft campaigns in each workspace the acting user belongs to; it cannot activate, send, read data back or reach other routes. It must stay server-side, travel only over TLS, and be rotated (redeploy on both sides) if exposed. There is no per-key rate limit.
- The acting user must exist as a Supabase user with a `profiles` row (it has signed in once) and be added to each client workspace. Removing its membership cuts RevenueOS off from that workspace immediately.
- Receipt uniqueness is per workspace and per acting user. If `REVENUEOS_ACTOR_USER_ID` changes, earlier references are no longer recognised as duplicates.
- Receipts carry the existing 7-day `expires_at`. Nothing removes them by expiry today (only workspace deletion does); if such a purge is added, idempotency for a reference lasts that long.
- One request is capped at 10 emails; the UI has no such cap.
- Follow-up emails are stored but only sent when `SEQUENCE_PROGRESSION_ENABLED` is true ([ADR-0009](0009-sequence-progression-sweeper.md)). Preflight reports this only for hyper-personalized campaigns, so for a STANDARD intake the operator must make sure the setting is on.
- The unused `Idempotency-Key` on `POST /campaigns` and `audience/select` is a separate, pre-existing issue and is not changed here.
