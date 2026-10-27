# LLM port, provider data handling, budgets and validation policy

## Status

Accepted for implementation — 2026-09-26, together with [ADR-0011](0011-hyper-personalized-campaign-type.md).

**Amended by [ADR-0016](0016-api-process-ai-drafting-and-company-analysis.md):** the API process may also hold `PERSONALIZATION_OPENAI_API_KEY` and call the model, for interactive authoring only (company analysis and reference drafting), never with lead data. The per-lead pipeline below is unchanged and still runs only in the personalization worker.

## Context

Hyper-personalized campaigns ([ADR-0011](0011-hyper-personalized-campaign-type.md)) need a language model to write per-lead email. Nothing in the repository calls an LLM: no SDK, configuration, secrets, budget control or data-handling rule exists, and [WORKERS](../architecture/WORKERS.md) says personalization workers have "no MVP queues or secrets". Lead data is customer PII; model output is untrusted text that will be sent to third parties under a workspace's sender identity.

## Decision

**Port and provider.** Generation is behind an injectable `PersonalizationModel` port (`backend/app/modules/personalization/`), created through a `model_factory` like the storage factory. OpenAI is the first adapter. A deterministic fake with scripted responses and failure injection is the default in tests, and constructing the OpenAI adapter without an injected transport fails in tests so no test can reach the network. The port maps provider errors into a fixed taxonomy (timeout, rate limited, transient, permanent/auth/config, refusal, malformed output) that drives retry policy.

**Adapter implementation.** Raw `httpx` (already a dependency) rather than the vendor SDK: one endpoint is needed, and exact control over timeouts, no hidden retries, `trust_env=False` (no proxy) and error classification is required by [WORKERS](../architecture/WORKERS.md); it is also testable with `httpx.MockTransport`. The model name comes from configuration with no baked-in default. Output uses a strict JSON schema (`subject`, `paragraphs[]`, `facts_used[]`, `angle`). The request `user` field carries a hash of the workspace id, not an address.

**Secrets and runtime.** The API key exists only in the `personalization` worker group's environment (`PERSONALIZATION_OPENAI_API_KEY`), never in task payloads, database rows, logs or DTOs. The worker refuses to start when `PERSONALIZATION_ENABLED=true` and the key or model is missing. The feature flag defaults to off.

**Data handling (subprocessor).** Enabling `PERSONALIZATION_ENABLED` is the operator's assertion that lead data may be sent to the model provider as a subprocessor; there is no per-workspace consent record (owner decision). Only an allow-list of fields is sent: first name, last name, company, job title, industry, location and researched website text. **Email address, phone and LinkedIn are never sent.** One lead per request. Provider-side retention should be disabled where the provider offers it; confirming provider terms, retention and legal wording is an open item to close before production enablement. Logs and metrics carry only ids, codes, counts and timings, never lead fields, website text, prompts or outputs ([SECURITY_ARCHITECTURE](../security/SECURITY_ARCHITECTURE.md)).

**Untrusted input.** Lead fields, website text and prior emails are placed in a JSON data object in the user message, never concatenated into instructions. The model has no tools. The model never emits HTML: the body is assembled by our code from `paragraphs[]`, then passed through the existing `sanitize_email_html` and preheader injection before the content digest is computed. Links are only allowed from URLs present in the reference template or objective.

**Validation is deterministic and authoritative.** No LLM judge (owner decision). A pure validator returns stable failure codes: non-empty; subject length and no CR/LF/control characters or `{{`; body length; numbers, currency, URLs, emails and phones must appear in reference ∪ objective ∪ supplied facts; `facts_used` ids valid; must-mention present, never-say absent; CTA preserved; key-term overlap with the reference above a threshold; prohibited/unsafe patterns; for follow-ups, overlap with the previous body below a threshold, a different subject, and no filler ("just following up", "circling back", "bumping this"). Retry prompts carry failure codes only, not the rejected output.

**Retries and cost.** Quality rejections consume `attempt_count` (max `PERSONALIZATION_MAX_ATTEMPTS`, default 3); transient provider errors use a separate `transient_error_count` with exponential backoff and do not consume quality attempts; auth/config errors keep rows pending with long backoff and raise an operator alert. Every attempt reserves budget first: per-workspace daily caps for generation, preview and website fetch (environment-configured), plus a global requests-per-minute limit. A crash after the model call leaves an attempt that is marked abandoned at lease expiry and still counted, bounding billed calls to `max_attempts` per message.

## Alternatives Considered

**Vendor SDK** — rejected for control and dependency discipline (AGENTS §20). **Anthropic/other first** — the owner chose OpenAI; the port makes other adapters additive. **LLM judge** — rejected by the owner (cost); the validator can be extended later, and any judge may only add rejections. **Per-workspace opt-in row and acknowledgment** — declined by the owner; noted as a residual consent/audit gap. **Persisting the raw candidate before validation** to avoid paying twice after a crash — deferred.

## Consequences

- New configuration keys and a new secret in one worker group only.
- Validator thresholds need tuning against real outputs; too strict yields `FAILED`, too loose risks weak personalization.
- Cost controls are separate from send rate limiting ([RATE_LIMITING](../architecture/RATE_LIMITING.md)) and do not consume send capacity.
- Amended by [ADR-0021](0021-follow-up-same-thread.md): a follow-up sent in the same thread takes "Re: " + the previous email's subject, so the subject checks (including "a different subject") do not apply to it and the model's subject is discarded.
