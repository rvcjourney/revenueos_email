# Follow-up emails in the same thread

## Status

Accepted by the product owner on 2026-10-27 (decisions below were chosen explicitly). Migration 0048 is prepared and must be reviewed and applied by the project owner; this ADR does not apply it. Rollout slices 1-4 (below) are implemented.

This ADR **merges** two of the "Email follow-up settings" listed in [PROJECT_CONTEXT §17](../product/PROJECT_CONTEXT.md) — "send in same thread" and "use previous subject" — into one setting (D1). It also settles the item [ADR-0018](0018-email-craft-rules-and-sequence-playbook.md) left open ("threading follow-ups under the first email's subject") for steps that opt in.

**Amends ADR-0018:** its distinct-subject rule applies only to steps where `email_same_thread` is false (D6).

## Context

Owners want a follow-up EMAIL step to arrive as a reply in the same thread as the lead's previous email, for STANDARD and HYPER_PERSONALIZED campaigns, on Gmail API, Microsoft Graph and SMTP mailboxes. Today every step is a new conversation with its own subject.

A read-only investigation (2026-10-27) found:

1. The previous email is already at hand where follow-ups are built. `fetch_progressable_enrollments` (`campaigns/worker_repository.py`) joins the enrollment's SENT message for the step it points at, and the personalization writer's `load_previous_email` (`personalization/repository.py`) reads the most recent earlier SENT email with its subject.
2. The message content digest (`compute_content_digest`) covers the subject, so a subject frozen into the snapshot at render time is integrity-checked by the send gate with no gate change.
3. `messages` has `rfc_message_id`, `provider_message_id` and `content_subject` but no provider thread column. Gmail's `threadId` (and Graph's `conversationId`) is recorded only on `message_attempts.provider_thread_ref`.
4. Gmail and SMTP build raw MIME and write `OutboundMessageEnvelope.extra_headers`. The Graph adapter sends a JSON draft and ignores `extra_headers` (Graph accepts only `x-` custom headers) and our generated Message-ID.
5. `sequence_steps_payload_shape_check` requires `email_subject` for every EMAIL step, and the API, `describe_sequence_problem` and the editor all require a subject.
6. `compute_sequence_content_digest` and `compute_approval_digest` hash an explicit list of step fields, not every column.

## Decision

### D1 One switch per step

EMAIL steps gain one boolean, `email_same_thread`. On, the step is sent as a reply to the lead's previous email and takes that email's subject; there is no separate "use previous subject" setting.

- Not allowed on the first EMAIL step. A save, reorder or delete that would leave a threaded step as the first EMAIL step is refused with 422 `same_thread_requires_previous_email`.
- API default `false`. The editor pre-checks it for **new** follow-up EMAIL steps. Existing steps are unchanged.

### D2 The parent message

The parent is the enrollment's message for the **immediately previous EMAIL step** — the SENT message progression already joins. It is stored when the follow-up is created as `messages.reply_to_message_id`. A composite foreign key to `(workspace_id, mailbox_id, id)` enforces the same workspace and the same mailbox. The message snapshot guard makes it immutable once the message is rendered.

### D3 Subject

A threaded step has no subject of its own. The frozen snapshot subject is `"Re: " + parent.content_subject`, without doubling an existing `Re:` prefix (case-insensitive), truncated to the 500-character column limit. The content digest covers it automatically. The previous email is not quoted in the body (out of scope for v1).

### D4 Sending

`In-Reply-To` is the parent's `rfc_message_id`. `References` is the ancestors' `rfc_message_id`s oldest first, found by walking `reply_to_message_id` (bounded by the number of steps).

- **SMTP:** the two headers only.
- **Gmail:** the headers plus `threadId` from the parent's latest ACCEPTED attempt's `provider_thread_ref`. If Gmail rejects the send specifically because the thread is invalid or not found — a response that proves nothing was sent — it is retried once without `threadId`.
- **Graph:** find the parent's sent copy by `internetMessageId` (the existing `lookup_message`), `POST /me/messages/{id}/createReply`, `PATCH` the reply draft's recipients, subject, body and attachments to our snapshot, then `POST …/send` as today. If the parent's sent copy cannot be found, it is a normal unthreaded send. `createReply` is Graph's documented way to thread a message; setting In-Reply-To/References through MAPI extended properties on a plain draft was rejected as unverified.

### D5 Fallback

If the parent or its `rfc_message_id` is missing, the email is sent unthreaded with its frozen `Re:` subject. A lead is never failed for this. The reason (e.g. `same_thread_unavailable`) is recorded with the existing attempt-evidence/audit pattern.

### D6 Personalized campaigns

`PROMPT_VERSION` is unchanged. The subject the model writes (or the FALLBACK reference subject) is replaced by the D3 subject, and subject validation is skipped only for threaded steps. `compute_approval_digest` and `compute_sequence_content_digest` include the flag only when it is true, so every existing digest stays byte-identical; only campaigns that use the flag need their samples re-approved.

The AI sequence-drafting validator (the [ADR-0016](0016-api-process-ai-drafting-and-company-analysis.md)/[ADR-0018](0018-email-craft-rules-and-sequence-playbook.md) draft path) is unchanged, except that the distinct-subject rule applies only to steps where `email_same_thread` is false; a subject on a threaded step is ignored.

### D7 Edits while paused

One rule for both campaign types, consistent with [ADR-0020](0020-pause-resume-paused-edits-and-late-enrollment.md) Amendment 2 (migration 0046): `email_same_thread` may change while PAUSED **only on steps after the frontier**. It is enforced in the sequence service **and** in `app_guard_frozen_sequence_steps`, which 0048 updates (taking `app_integrity_guard` for its own transaction with `GRANT app_integrity_guard TO CURRENT_USER WITH INHERIT TRUE, SET TRUE` and revoking it afterwards, as 0042/0044 do). Messages already created keep their parent. The post-resume reconcile pass gains a body-only re-render for threaded steps that keeps the existing `content_subject`.

### D8 Test send

A step test send of a threaded step uses `"Re: "` + the previous EMAIL step's rendered subject and carries no threading headers.

### D9 Duplication

Campaign duplicate and step duplicate copy the flag.

## Consequences

- Migration 0048: `sequence_steps.email_same_thread` (NOT NULL default false, API INSERT/UPDATE grants, payload-shape CHECK rewritten so an EMAIL step needs a subject **or** the flag); `messages.reply_to_message_id` with its composite FK, an index, the worker INSERT grant and the snapshot-guard update; `app_guard_frozen_sequence_steps` updated for D7. Apply after 0046 and 0047 and **before** deploying the code: until it is applied, a step without a subject fails the old CHECK.
- A threaded Graph send costs about two extra API calls (lookup, createReply, PATCH).
- Threading depends on the parent's `rfc_message_id` being stored: the RFC Message-ID is persisted only after the provider call, contrary to [PROVIDER_ARCHITECTURE.md:75](../architecture/PROVIDER_ARCHITECTURE.md); parents without a stored id fall back per D5.
- If per-lead mailbox reassignment (the open decision in [CAMPAIGN_ENGINE.md:47](../architecture/CAMPAIGN_ENGINE.md)) is ever approved, threaded follow-ups after a mailbox change fall back to unthreaded sends by design: a reply must come from the same mailbox.
- Erasure and purge must keep working with the new self-referencing FK; this is to be proven by real-Postgres tests before the migration is applied.
- Reply matching (`replies/matcher.py`) is unchanged. Replies to threaded follow-ups quote the whole References chain, so matching is expected to improve.

## Rollout

Implementation ships in slices:

1. Migration 0048, STANDARD campaigns, SMTP and Gmail. The API refuses the flag on HYPER_PERSONALIZED campaigns (422 `same_thread_not_supported_yet`), and Graph sends threaded steps unthreaded via D5.
2. HYPER_PERSONALIZED campaigns (D6). The slice 1 refusal is removed. The writer takes the subject from the parent stored at planning (`messages.reply_to_message_id`); without one (the lead changed mailbox) from the previous sent email; with neither it keeps the model's or reference subject and audits `message.same_thread_unavailable` (`previous_subject_missing`). A threaded step's sample preview takes "Re: " + the same lead's previous sample subject.
3. Graph `createReply` (D4). The slice 1 `provider_not_supported` fallback is removed; a Graph mailbox with no sent parent to reply to sends unthreaded and audits `provider_thread_not_found`.
4. Frontend.

## Out of scope

Quoting the previous email; prompt changes telling the model it is writing a reply; threading the first email.
