# RevenueOS launch of hyper-personalized campaigns

## Status

Accepted — 2026-10-06 (owner decision: RevenueOS sends hyper-personalized campaigns through the API; a person in the app approves the samples). Extends [ADR-0022](0022-revenueos-launch.md). Amends [ADR-0019](0019-revenueos-campaign-intake.md) decision 3 ("always a STANDARD draft") for the launch route only. Changes nothing in [ADR-0011](0011-hyper-personalized-campaign-type.md) to [ADR-0013](0013-research-sources-and-outbound-fetch.md). No migration.

## Context

The four RevenueOS routes created STANDARD campaigns only. The owner wants RevenueOS to send a HYPER_PERSONALIZED campaign in the same one-request form. ADR-0011 requires sample previews to be generated and approved by a user with `campaigns.execute` before such a campaign can be activated, and the owner chose to keep that approval with a person.

## Decision

1. **The launch route takes two more fields.** `campaign_type` (`STANDARD` by default, or `HYPER_PERSONALIZED`) and `objective` (the existing `PersonalizationConfig`: objective, offer, cta, and optionally target, problem_solved, tone, must_mention, never_say). `objective` is required for a hyper-personalized launch and refused for a standard one. `emails` are then the reference templates.
2. **Recipients carry profile fields.** A recipient accepts the lead profile fields (`company_industry`, `company_website`, `city`, `state`, `country`, `department`, `website`, `linkedin_url`, `phone`, `experience_years`), validated by the existing lead rules. They are what per-lead writing draws on; which of them reach the model is still decided by ADR-0012.
3. **Composition only.** The intake service creates the campaign with the type, and saves the objective through `PersonalizationApiService.put_config` before the steps. The objective is passed as an argument, not as part of the intake payload, so the hash of every campaign stored before this change is unaffected. The launch's own hash includes the objective only when present, for the same reason.
4. **The API prepares; a person approves and starts.** After the campaign is stored and committed, the request waits briefly for audience capture, commits the READY audience and requests sample previews for the first accepted recipient (`create_previews`, with a batch id derived from the campaign so a repeat spends nothing). The response says `start_status: "awaiting_approval"`. It never approves and never activates: a Manager, Admin or Owner reads the samples on the campaign page, approves them and starts the campaign.
5. **`auto_start` does not apply.** It is ignored for a hyper-personalized launch, and `REVENUEOS_AUTO_START_ENABLED` is not required. `PERSONALIZATION_ENABLED` is: without it the request is refused whole (`503`) before anything is created.
6. **Repeatable.** While capture is still running the response is `pending`; sending the same request again continues from there. Once a person has started the campaign a repeat answers `already_started`.

## Alternatives Considered

**Approve automatically after the rule checks pass**: fully hands-off, declined by the owner; it would make ADR-0011's human control a formality. **Return the sample text for RevenueOS to approve in a second call**: reviewable by software, declined. **Let the platform draft the reference emails from the client's website (ADR-0016)**: RevenueOS already holds that knowledge and sends the templates. **Extend the campaign intake route too**: kept to the launch route to limit the change.

## Consequences

- A hyper-personalized launch always needs a person, so it is not hands-off the way a standard launch is.
- The sample is generated for whichever recipient was captured first; a person can generate another for a recipient of their choice in the app.
- Requesting a sample spends from the workspace's daily preview budget and sends that recipient's allowed fields to the model provider, without a person having asked for it.
- The reference templates are checked by the existing reference-template rules when the steps are saved; a template they reject fails the whole request.
- Only the stored state, the audience commit and the sample request are covered by a real-database test. Sample generation, approval and per-lead writing run in the personalization worker with the model provider and have not been exercised on this deployment.
