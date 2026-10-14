"""One generation attempt: research -> context -> model -> validation (ADR-0011).

The same service backs the just-in-time worker and the sample previews, so the
business rules exist once (AGENTS.md: reuse authoritative logic). It performs no
database work of its own and holds no transaction; the caller owns durability,
leases and eligibility.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from app.modules.campaigns.message_rendering import (
    compute_content_digest,
    render_step_content,
)
from app.modules.personalization.brand_layout import render_for_config
from app.modules.personalization.config_schema import PersonalizationConfig
from app.modules.personalization.context_builder import build_context
from app.modules.personalization.ports import (
    Fact,
    GenerationRequest,
    PersonalizationModel,
    PreviousEmail,
    ResearchOutcome,
)
from app.modules.personalization.text_utils import (
    extract_hrefs,
    extract_urls,
    html_to_text,
    split_paragraphs,
)
from app.modules.personalization.validator import ValidationContext, validate_generation
from app.modules.personalization.version import (
    GENERATED_RENDERER_VERSION,
    PROMPT_VERSION,
    STANDARD_RENDERER_VERSION,
)
from app.modules.templates.rendering import (
    inject_preheader,
    render_preheader,
    render_template_content,
)

AttemptKind = Literal["GENERATED", "FALLBACK", "REJECTED"]


class ResearchLookup(Protocol):
    def research(
        self, *, workspace_id: UUID, company_website: str | None
    ) -> ResearchOutcome: ...


@dataclass(frozen=True)
class GenerationInputs:
    workspace_id: UUID
    frozen_variables: Mapping[str, Any]
    objective: PersonalizationConfig
    reference_subject: str
    reference_body_html: str
    reference_preheader: str | None
    step_position: int
    previous: PreviousEmail | None = None
    retry_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttemptOutcome:
    kind: AttemptKind
    subject: str
    body_html: str
    content_digest: str
    renderer_version: int
    failure_codes: tuple[str, ...]
    facts_used: tuple[Fact, ...]
    angle: str | None
    context_digest: str
    research_status: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    output_digest: str | None = None
    research_excerpt: str = ""
    research_url: str | None = None


class PersonalizationService:
    def __init__(
        self,
        *,
        model: PersonalizationModel,
        research: ResearchLookup,
        min_facts: int,
    ) -> None:
        self._model = model
        self._research = research
        self._min_facts = min_facts

    @property
    def model_name(self) -> str:
        return self._model.model

    @property
    def provider(self) -> str:
        return self._model.provider

    def render_reference(self, inputs: GenerationInputs) -> tuple[str, str, str]:
        """(subject, body, digest) of the reference template with ordinary
        variable substitution -- the thin-context fallback."""
        rendered = render_step_content(
            subject=inputs.reference_subject,
            body_html=inputs.reference_body_html,
            preheader=inputs.reference_preheader,
            frozen_variables=inputs.frozen_variables,
            renderer_version=STANDARD_RENDERER_VERSION,
        )
        return rendered.subject, rendered.body_html, rendered.content_digest

    def attempt(self, inputs: GenerationInputs) -> AttemptOutcome:
        """Raises a `ModelError` subclass for provider failures; the caller maps
        those to retry policy. Validation problems are a normal REJECTED result."""
        rendered_subject, rendered_body = render_template_content(
            subject=inputs.reference_subject,
            body_html=inputs.reference_body_html,
            context_data=inputs.frozen_variables,
        )
        reference_text = html_to_text(rendered_body)

        research = self._research.research(
            workspace_id=inputs.workspace_id,
            company_website=_str_or_none(
                inputs.frozen_variables.get("company_website")
            ),
        )
        context = build_context(
            inputs.frozen_variables,
            website_snippets=research.snippets,
            research_status=research.status,
            min_facts=self._min_facts,
        )

        if context.thin:
            subject, body, digest = self.render_reference(inputs)
            return AttemptOutcome(
                kind="FALLBACK",
                subject=subject,
                body_html=body,
                content_digest=digest,
                renderer_version=STANDARD_RENDERER_VERSION,
                failure_codes=(),
                facts_used=(),
                angle=None,
                context_digest=context.context_digest,
                research_status=research.status,
                research_excerpt=" ".join(research.snippets)[:500],
                research_url=research.source_url,
            )

        request = GenerationRequest(
            workspace_ref=str(inputs.workspace_id),
            objective=inputs.objective.generation_objective(),
            reference_subject=rendered_subject,
            reference_paragraphs=split_paragraphs(reference_text),
            recipient=context.recipient,
            facts=context.facts,
            step_position=inputs.step_position,
            previous=inputs.previous,
            retry_codes=inputs.retry_codes,
        )
        result = self._model.generate(request)

        validation = validate_generation(
            result.output,
            ValidationContext(
                objective=inputs.objective,
                reference_subject=rendered_subject,
                reference_text=reference_text,
                reference_urls=frozenset(
                    extract_hrefs(rendered_body) | extract_urls(reference_text)
                ),
                facts=context.fact_by_id(),
                previous=inputs.previous,
                require_fact_use=True,
            ),
        )
        output_digest = hashlib.sha256(
            f"{validation.subject}\x00{validation.body_text}".encode()
        ).hexdigest()
        common: dict[str, Any] = {
            "context_digest": context.context_digest,
            "research_status": research.status,
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "latency_ms": result.latency_ms,
            "output_digest": output_digest,
            "research_excerpt": " ".join(research.snippets)[:500],
            "research_url": research.source_url,
        }
        if validation.codes:
            return AttemptOutcome(
                kind="REJECTED",
                subject="",
                body_html="",
                content_digest="",
                renderer_version=GENERATED_RENDERER_VERSION,
                failure_codes=validation.codes,
                facts_used=(),
                angle=None,
                **common,
            )

        # The pre-header is authored on the step and rendered deterministically,
        # exactly as for standard content, before the digest is computed.
        # An HTML campaign wraps the validated, sanitized paragraphs in the brand
        # layout here -- the same seam previews use, so a preview is exactly what
        # will be sent -- before the preheader and the digest (ADR-0017).
        body = inject_preheader(
            render_for_config(inputs.objective, validation.body_html),
            render_preheader(inputs.reference_preheader, inputs.frozen_variables),
        )
        return AttemptOutcome(
            kind="GENERATED",
            subject=validation.subject,
            body_html=body,
            content_digest=compute_content_digest(
                validation.subject, body, GENERATED_RENDERER_VERSION
            ),
            renderer_version=GENERATED_RENDERER_VERSION,
            failure_codes=(),
            facts_used=validation.facts_used,
            angle=result.output.angle.strip(),
            **common,
        )


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def facts_payload(facts: tuple[Fact, ...], *, limit: int = 8) -> list[dict[str, str]]:
    """The facts recorded for follow-up context: bounded well below the 4 KiB
    column cap."""
    return [
        {"id": f.id, "source": f.source, "text": f.text[:240]} for f in facts[:limit]
    ]


__all__ = [
    "AttemptOutcome",
    "GenerationInputs",
    "PROMPT_VERSION",
    "PersonalizationService",
    "ResearchLookup",
    "facts_payload",
]
