# RevenueOS sample feedback and versioned approval

## Status

Accepted — 2026-10-14 (owner decision: a RevenueOS reviewer sees samples for several different recipients, can send feedback that rewrites them, and approves exactly the version read). Amends [ADR-0024](0024-revenueos-sample-approval.md) and [ADR-0023](0023-revenueos-hyper-personalized-launch.md) decision 4 (sample for the first recipient). No migration.

## Context

ADR-0024 lets RevenueOS read one recipient's samples and approve the latest batch. Reviewers want to see the range across 3–4 different recipients, ask for changes in plain words, and be sure that what they approve is what every recipient gets. Approving "the latest batch" also lets a batch written after the reviewer read the samples be approved unseen.

## Decision

1. **Chosen sample recipients.** The launch request accepts `sample_recipients`: 1–4 emails, each one of its recipients, HYPER_PERSONALIZED only. Samples are requested for those that were accepted into the audience, in the order given; with none accepted, for the first accepted recipient as before. Part of the launch hash only when present. The in-app preview limit of one lead is unchanged; RevenueOS uses `RevenueOSPreviewCreateIn` (up to 4).
2. **Feedback lives in the objective.** `PersonalizationConfig.feedback` (optional, up to 4000 characters, rounds separated by a blank line). Because the approval digest covers the whole stored config, approval locks the feedback, and every recipient's email is written with it, not only the samples. Absent, the stored config, the digest and the per-lead prompt are byte-identical to before.
3. **How the model uses it.** Feedback is sent with the objective, in the data, and a rule is added to the system prompt only when there is feedback: apply it to wording, length, tone and emphasis; it never overrides the existing rules, the greeting, the CTA, must_mention or never_say. It is not sent to reference-email drafting (`objective_core` is unchanged). Numbers and links in the feedback count as the sender's own, like the objective.
4. **Feedback route.** `POST .../integrations/revenueos/campaigns/{campaign_id}/feedback {batch_id, feedback}` (1–1000 characters). Refused with `409 batch_not_latest` unless `batch_id` is the latest batch. It appends the feedback through `put_config` (DRAFT only, optimistic version) and requests new samples for the same recipients through `create_previews`, in one transaction. The new batch id is derived from `batch_id`, so a repeat answers `duplicate: true` and spends nothing. Needs `campaigns.draft`; it starts nothing, so it does not need `REVENUEOS_AUTO_START_ENABLED`.
5. **Versioned approval.** The approve route accepts an optional `{batch_id, config_digest}`, as read from the samples route. Another batch is refused with `409 batch_not_latest`, changed settings with `409 approval_stale`. Without a body the ADR-0024 behaviour is unchanged.
6. **Samples shape.** The samples route adds `batch_id`, `config_digest` and `recipients` (audience member id, email, name, company, title, and that recipient's emails by step). The flat `samples` list stays, ordered recipient by recipient, with `email_number` now the step's place in the sequence.
7. **Greeting kept.** When the reference's first paragraph is a greeting line ("Hi {{first_name}}," rendered for the recipient), a generated email whose first paragraph differs is rejected with `greeting_changed` (and retried like any other code). This applies to every hyper-personalized email, not only RevenueOS ones. PROMPT_VERSION is not bumped: the prompt already required the reference's greeting, and bumping would stale every existing approval.

## Alternatives Considered

**Rewrite only the samples from feedback**: cheaper, but the approved samples would not match what recipients get. **A separate feedback table**: keeps history, but needs a migration, and the digest would not cover it without changing its definition; the audit log already records each objective change. **Let the reviewer pick sample recipients after launch**: one more call; RevenueOS already knows whom it wants to see.

## Consequences

- Each feedback round spends `recipients × email steps` from the daily preview budget (`PERSONALIZATION_DAILY_PREVIEW_CAP`, default 100). Four recipients × three emails × four versions is 48.
- Feedback accumulates; there is no route to remove it. Up to 4000 characters, then `422 feedback_too_long`.
- A repeat of a feedback call with different text returns the batch written from the first text.
- If the process stops after the new batch is committed and before the generation task is dispatched, the batch stays `generating`; this is the same window `create_previews` already has.
- Running campaigns get the greeting check on their next generations. An email that drifts from the greeting is retried with the code's guidance; if every attempt drifts, that message fails like any other validation failure instead of being sent.
