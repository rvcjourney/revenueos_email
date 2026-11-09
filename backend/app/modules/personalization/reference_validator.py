"""Deterministic validation of drafted reference emails (ADR-0016).

Pure and I/O free; no LLM judge. It is deliberately at least as strict as the
per-lead validator (validator.py) on the things that validator will later hold
each generated email to -- every must-mention phrase, the call to action, the
never-say list, unsupported facts, unsafe wording -- so a reference that passes
here cannot make per-lead generation fail on those rules. It differs where a
reference email is different: merge variables such as ``{{first_name|there}}``
are required and checked, because a lead with too little data receives the
rendered reference itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.modules.personalization.config_schema import (
    CompanyProfile,
    PersonalizationConfig,
)
from app.modules.personalization.email_style import shape_codes, step_brief
from app.modules.personalization.ports import DraftedStep, SequenceDraftOutput
from app.modules.personalization.text_utils import (
    containment,
    extract_emails,
    extract_numbers,
    extract_phones,
    extract_urls,
    html_to_text,
    significant_tokens,
    word_shingles,
)
from app.modules.personalization.validator import _FILLER_RE as FILLER_RE
from app.modules.personalization.validator import _UNSAFE_RE as UNSAFE_RE
from app.modules.personalization.validator import (
    CTA_OVERLAP_MIN,
    FOLLOWUP_SIMILARITY_MAX,
    assemble_body_html,
)
from app.modules.personalization.validator import (
    _normalize_subject as normalize_subject,
)
from app.modules.personalization.validator import _objective_text as objective_text
from app.modules.templates.sanitizer import sanitize_email_html

MAX_SUBJECT_CHARS = 100
MAX_PREHEADER_CHARS = 100
MAX_PARAGRAPHS = 10
MIN_WAIT_DAYS = 1
MAX_WAIT_DAYS = 14
DEFAULT_WAIT_DAYS = (3, 4, 5, 6, 7)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LINE_BREAK_RE = re.compile(r"[\x00-\x1f\x7f]")
_MARKUP_RE = re.compile(
    r"\[\[|\]\]|<[a-z/!][^>]*>|^\s{0,3}#{1,6}\s|\*\*|__", re.I | re.M
)
_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_VARIABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@dataclass(frozen=True)
class ContextEmail:
    """A neighbouring email that is *not* being rewritten (STEP mode)."""

    position: int
    subject: str
    body_text: str
    # Sent in the same thread (ADR-0021): its subject is not used, so not compared.
    threaded: bool = False


@dataclass(frozen=True)
class DraftContext:
    objective: PersonalizationConfig
    company: CompanyProfile | None
    expected_positions: tuple[int, ...]
    allowed_variables: frozenset[str]
    context_emails: tuple[ContextEmail, ...] = ()
    # Positions being drafted that are sent in the same thread: the subject the
    # model writes for them is ignored (ADR-0021 D6).
    threaded_positions: frozenset[int] = frozenset()


@dataclass(frozen=True)
class ValidatedStep:
    position: int
    subject: str
    preheader: str | None
    paragraphs: tuple[str, ...]
    fragment_html: str  # sanitized paragraphs, before any brand layout
    wait_days: int  # days to wait AFTER this email (clamped); 0 for the last


@dataclass(frozen=True)
class DraftValidation:
    codes: tuple[str, ...]
    steps: tuple[ValidatedStep, ...] = field(default_factory=tuple)
    # Each email's own problems (position -> codes); empty tuple = that email is
    # fine on its own. Lets a retry rewrite only the emails that failed.
    by_position: Mapping[int, tuple[str, ...]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.codes


def _company_text(company: CompanyProfile | None) -> str:
    if company is None:
        return ""
    return "\n".join(
        [
            company.company_name,
            company.summary,
            company.audience,
            *company.services,
            *company.industries,
            *company.key_messages,
        ]
    )


def _render_placeholders(text: str) -> str:
    """Text as a reader sees it when nobody's data is available: each variable is
    replaced by its fallback."""

    def fallback(match: re.Match[str]) -> str:
        _, _, tail = match.group(1).partition("|")
        return tail.strip()

    return _PLACEHOLDER_RE.sub(fallback, text)


def _check_placeholders(text: str, allowed: frozenset[str], flag) -> None:
    for match in _PLACEHOLDER_RE.finditer(text):
        name, sep, fallback = match.group(1).partition("|")
        name = name.strip()
        if not _VARIABLE_NAME_RE.match(name):
            flag("malformed_placeholder")
        elif name not in allowed:
            flag("unknown_variable")
        elif not sep or not fallback.strip():
            flag("variable_without_fallback")
    # Any brace left once valid placeholders are removed is malformed.
    if "{" in _PLACEHOLDER_RE.sub("", text) or "}" in _PLACEHOLDER_RE.sub("", text):
        flag("malformed_placeholder")


def clamp_wait_days(value: int) -> int:
    return min(MAX_WAIT_DAYS, max(MIN_WAIT_DAYS, int(value)))


def validate_draft(output: SequenceDraftOutput, ctx: DraftContext) -> DraftValidation:
    codes: list[str] = []
    step_codes: list[str] = []
    by_position: dict[int, tuple[str, ...]] = {}

    def flag(code: str) -> None:
        if code not in codes:
            codes.append(code)
        if code not in step_codes:
            step_codes.append(code)

    drafted = sorted(output.steps, key=lambda s: s.position)
    if tuple(s.position for s in drafted) != tuple(sorted(ctx.expected_positions)):
        flag("wrong_step_count")
        return DraftValidation(codes=tuple(codes))

    objective = ctx.objective
    # The sequence length decides each email's job (and so its length limits);
    # neighbours that are not being rewritten still count.
    total_emails = len(ctx.expected_positions) + len(ctx.context_emails)
    grounding = "\n".join([objective_text(objective), _company_text(ctx.company)])
    allowed_numbers = extract_numbers(grounding)
    allowed_urls = frozenset(extract_urls(grounding))
    allowed_emails = extract_emails(grounding)
    allowed_phones = extract_phones(grounding)
    cta_urls = extract_urls(objective.cta)
    cta_tokens = significant_tokens(objective.cta)

    validated: list[ValidatedStep] = []
    # (position, normalized subject, body word shingles) of every email seen so
    # far in sequence order, including untouched neighbours.
    # Rendered the same way as the draft under test: stored emails still carry their
    # {{variable|fallback}} placeholders, and comparing raw against rendered text
    # would never match.
    # A same-thread email's subject is None here: it never matches another.
    others: list[tuple[int, str | None, set[tuple[str, ...]]]] = [
        (
            e.position,
            None if e.threaded else normalize_subject(_render_placeholders(e.subject)),
            word_shingles(_render_placeholders(e.body_text)),
        )
        for e in ctx.context_emails
    ]

    for index, step in enumerate(drafted):
        step_codes.clear()
        threaded = step.position in ctx.threaded_positions
        subject = "" if threaded else (step.subject or "").strip()
        preheader = (step.preheader or "").strip()
        paragraphs = tuple(p.strip() for p in step.paragraphs if p and p.strip())

        if not threaded and (
            not subject
            or len(subject) > MAX_SUBJECT_CHARS
            or _LINE_BREAK_RE.search(step.subject or "")
        ):
            flag("subject_invalid")
        if len(preheader) > MAX_PREHEADER_CHARS or _LINE_BREAK_RE.search(preheader):
            flag("subject_invalid")
        if len(paragraphs) > MAX_PARAGRAPHS or any(
            _CONTROL_RE.search(p) for p in paragraphs
        ):
            flag("markup_in_text")

        body_text = "\n\n".join(paragraphs)
        combined = f"{subject}\n{preheader}\n{body_text}"
        for part in (subject, preheader, body_text):
            _check_placeholders(part, ctx.allowed_variables, flag)
            if _MARKUP_RE.search(_PLACEHOLDER_RE.sub("", part)):
                flag("markup_in_text")

        readable = _render_placeholders(body_text)
        words = len(readable.split())
        brief = step_brief(step.position, total_emails)
        if words < brief.min_words:
            flag("body_too_short")
        if words > brief.max_words:
            flag("body_too_long")
        for code in shape_codes([_render_placeholders(p) for p in paragraphs]):
            flag(code)

        readable_all = _render_placeholders(combined)
        if extract_numbers(readable_all) - allowed_numbers:
            flag("unsupported_number")
        if extract_urls(readable_all) - allowed_urls:
            flag("unsupported_url")
        if extract_emails(readable_all) - allowed_emails:
            flag("unsupported_email")
        if extract_phones(readable_all) - allowed_phones:
            flag("unsupported_phone")

        lowered = readable_all.lower()
        if any(phrase.lower() not in lowered for phrase in objective.must_mention):
            flag("missing_must_mention")
        if any(phrase.lower() in lowered for phrase in objective.never_say):
            flag("never_say_violation")

        if cta_urls and not (cta_urls & extract_urls(readable_all)):
            flag("missing_cta")
        elif len(cta_tokens) >= 2:
            covered = len(cta_tokens & significant_tokens(readable_all)) / len(
                cta_tokens
            )
            if covered < CTA_OVERLAP_MIN:
                flag("missing_cta")

        for match in UNSAFE_RE.finditer(readable_all):
            if match.group(0).lower() not in grounding.lower():
                flag("unsafe_content")
                break

        normalized_subject = (
            None if threaded else normalize_subject(_render_placeholders(subject))
        )
        shingles = word_shingles(readable)
        if index > 0 or any(p < step.position for p, _, _ in others):
            if FILLER_RE.search(readable_all):
                flag("filler_followup")
        if normalized_subject is not None and any(
            normalized_subject == s for _, s, _ in others
        ):
            flag("subject_repeated")
        earlier = [sh for pos, _, sh in others if pos < step.position]
        if earlier and containment(shingles, earlier[-1]) >= FOLLOWUP_SIMILARITY_MAX:
            flag("repeats_previous")
        others.append((step.position, normalized_subject, shingles))
        others.sort(key=lambda item: item[0])

        fragment = (
            sanitize_email_html(assemble_body_html(paragraphs, allowed_urls))
            if paragraphs
            else ""
        )
        if not html_to_text(fragment):
            flag("body_too_short")

        by_position[step.position] = tuple(step_codes)
        is_last = index == len(drafted) - 1
        validated.append(
            ValidatedStep(
                position=step.position,
                subject=subject,
                preheader=preheader or None,
                paragraphs=paragraphs,
                fragment_html=fragment,
                wait_days=0 if is_last else clamp_wait_days(step.wait_days_after),
            )
        )

    return DraftValidation(
        codes=tuple(codes), steps=tuple(validated), by_position=by_position
    )


def default_wait_days(gap_index: int) -> int:
    """Deterministic fallback when the model's wait is unusable: 3, 4, 5... days."""
    return DEFAULT_WAIT_DAYS[min(gap_index, len(DEFAULT_WAIT_DAYS) - 1)]


__all__ = [
    "ContextEmail",
    "DraftContext",
    "DraftValidation",
    "DraftedStep",
    "ValidatedStep",
    "clamp_wait_days",
    "default_wait_days",
    "validate_draft",
]
