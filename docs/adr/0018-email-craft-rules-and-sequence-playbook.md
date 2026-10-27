# Email craft rules and the sequence playbook

## Status

Accepted — 2026-10-01 (owner approved the plan). Amends the prompt and validation policy of [ADR-0012](0012-llm-port-data-handling-and-validation.md) and [ADR-0016](0016-api-process-ai-drafting-and-company-analysis.md); the schema, data handling and trust boundary are unchanged.

## Context

AI-written emails read as one dense paragraph and every follow-up restated the first email. The prompts only asked for "short plain paragraphs" and "a new angle"; nothing defined structure, and follow-ups were not given distinct jobs. The per-lead prompt told the model to keep the reference's structure, and the validator required 30% word overlap with it, so a weak reference was copied into every lead's email.

Published cold-email research (Gong, Instantly, Saleshandy and others) is consistent on the points that matter: a first email under about 100 words; short paragraphs of one to two sentences because most B2B email is first read on a phone; one idea and one low-friction question as the ask (interest questions out-reply meeting requests); and follow-ups that each do a different job instead of repeating the first email.

## Decision

1. **One module owns the rules.** `app/modules/personalization/email_style.py` holds the paragraph and length limits, the shape checks, and the sequence playbook. Reference drafting (`draft_prompt`, `reference_validator`) and per-lead generation (`prompt_builder`, `validator`) both use it, so the two cannot drift.
2. **Structure.** An email is a greeting, a hook (one sentence about the reader's situation, not the sender), a value statement (one or two sentences, one outcome) and an ask (one short question about interest), each its own paragraph of at most 40 words. The output schema is unchanged (`paragraphs`); the validators enforce the shape: `paragraph_too_dense`, `too_few_paragraphs`, `ask_not_question` (a question mark in one of the last three paragraphs) and `generic_opener` (pleasantries and self-introductions such as "I hope this finds you well" and "I'm reaching out").
3. **Sequence playbook, assigned by code.** Each email has a job and a word range: `intro` (40–100 words), `new_angle` (25–80), `proof` (25–80, using only facts in the objective or company details), `insight` (25–80) and `close` (15–50, only when the sequence has three or more emails). The reference drafting prompt receives the job, an instruction and the range for each email; the reference validator enforces the range. The model no longer decides what each follow-up is for.
4. **Per-lead length and role.** A per-lead email may be at most 1.35 times the words of its reference (never less than 60). The reference defines the follow-up's job and angle; the per-lead prompt personalizes the hook, keeps the value and ask, and forbids re-introducing the sender in a follow-up. The prompt tells the model never to copy a fact's wording or a marketing slogan.
5. **Versions.** `PROMPT_VERSION` becomes `hp-2` and `DRAFT_PROMPT_VERSION` becomes `rd-2`. The approval digest includes the prompt version, so existing sample approvals become stale and must be regenerated and approved again.
6. **Reliability (amendment, 2026-10-01).** Production drafts were rejected for being too short, having too few paragraphs and missing the call to action. Causes: the prompt told the model to paraphrase the call to action while the validator requires its words; follow-up briefs asked for questions unrelated to it; the model was given a length range but no target or paragraph layout; and a retry redid every email while naming problems without saying which email had them. Now each brief carries a target word window and a paragraph layout, the prompt lists the exact call-to-action words or links the validator checks, a retry names the email each problem belongs to, and emails that already passed are kept so only the failed ones are rewritten. `DRAFT_PROMPT_VERSION` is `rd-3`. The validator also compares a neighbouring email's subject and text with their `{{variable|fallback}}` placeholders rendered, as it does for the draft; before, a repeated subject was never detected when regenerating one email.

## Not decided here

Threading follow-ups under the first email's subject (the validators still require a distinct subject per email), an explicit "proof points" field on the objective, and the layout defaults (plain text versus branded HTML) are unchanged and left for separate decisions.

## Consequences

- Stricter validation can reject more attempts, which raises retries and token use per email; the prompts state the limits up front to keep that low.
- Reference emails already stored keep their wording; use "Regenerate" to rewrite them with the playbook. A user-written reference that violates the shape rules still works, but per-lead output is held to the rules regardless of its reference.
- Output quality still depends on the model; the checks guarantee shape and length, not tone. Validator thresholds should be tuned on real outputs.
- Amended by [ADR-0021](0021-follow-up-same-thread.md): the distinct-subject rule applies only to steps not sent in the same thread.
