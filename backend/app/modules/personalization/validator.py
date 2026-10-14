"""Deterministic validation of a generated email (ADR-0012).

Deterministic checks are authoritative: there is no LLM judge. The validator
builds the outgoing HTML itself from plain-text paragraphs (the model never
emits markup), runs it through the existing allow-list sanitizer, and returns
stable failure codes. Pure and I/O free.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.modules.personalization.config_schema import PersonalizationConfig
from app.modules.personalization.email_style import (
    SHAPE_GUIDANCE,
    max_words_for_reference,
    shape_codes,
)
from app.modules.personalization.ports import Fact, GenerationOutput, PreviousEmail
from app.modules.personalization.text_utils import (
    containment,
    extract_emails,
    extract_numbers,
    extract_phones,
    extract_urls,
    find_urls_with_spans,
    html_to_text,
    significant_tokens,
    split_paragraphs,
    word_shingles,
)
from app.modules.templates.sanitizer import sanitize_email_html

MAX_SUBJECT_CHARS = 200
MAX_PARAGRAPHS = 14
MAX_PARAGRAPH_CHARS = 2000
MAX_BODY_CHARS = 6000
MIN_BODY_WORDS = 15
REFERENCE_OVERLAP_MIN = 0.3
CTA_OVERLAP_MIN = 0.5
FOLLOWUP_SIMILARITY_MAX = 0.5
MAX_ANGLE_CHARS = 240

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SUBJECT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FILLER_RE = re.compile(
    r"\b(just\s+(following|checking)\s+up|circling\s+back|circle\s+back|bumping\s+this|"
    r"bump(ing)?\s+this\s+(up|to\s+the\s+top)|touching\s+base|"
    r"following\s+up\s+on\s+my\s+(last|previous)\s+email)\b",
    re.IGNORECASE,
)
_UNSAFE_RE = re.compile(
    r"\b(password|passcode|social\s+security|credit\s+card|wire\s+transfer|gift\s+card|"
    r"bitcoin|crypto\s+wallet|legal\s+action|final\s+notice|act\s+now|guaranteed\s+returns?|"
    r"100%\s+guarantee|risk[- ]free)\b",
    re.IGNORECASE,
)
# A reference whose first paragraph is a greeting line ("Hi Rahul,") fixes the
# first paragraph of every email written from it (ADR-0025).
_GREETING_RE = re.compile(r"^(hi|hello|hey|dear)\b[^\n]{0,80}$", re.IGNORECASE)
_PLACEHOLDER_MARK_RE = re.compile(r"\{\{|\}\}|\[\[|\]\]|<[a-z/][^>]*>", re.IGNORECASE)

# What each code tells the model on the next attempt. Codes only; the rejected
# output itself is never sent back (ADR-0012).
CODE_GUIDANCE: dict[str, str] = {
    "empty_subject": "Provide a non-empty subject line.",
    "subject_too_long": "Keep the subject under 200 characters, ideally under 80.",
    "subject_control_chars": "The subject must be a single line of plain text.",
    "subject_placeholder": "Do not use template placeholders or markup in the subject.",
    "empty_body": "Provide a non-empty email body.",
    "body_too_short": "Write a complete email of at least a few sentences.",
    "body_too_long": "Shorten the email; it is too long.",
    "too_many_paragraphs": "Use fewer, shorter paragraphs.",
    "paragraph_too_long": "Break long paragraphs into shorter ones.",
    "body_control_chars": "Use plain text only, without control characters.",
    "body_placeholder": "Do not use placeholders, brackets or markup in the body.",
    "unsupported_number": (
        "Do not state numbers, prices or statistics that are not in the "
        "reference, objective or facts."
    ),
    "unsupported_url": "Only use links that appear in the reference or objective.",
    "unsupported_email": (
        "Do not include email addresses that are not in the reference or objective."
    ),
    "unsupported_phone": (
        "Do not include phone numbers that are not in the reference or objective."
    ),
    "unknown_fact_id": "facts_used may only contain ids from the provided facts.",
    "no_facts_used": (
        "Base the opening on at least one provided fact and list its id in facts_used."
    ),
    "missing_must_mention": "Include every must_mention phrase from the objective.",
    "never_say_violation": "Do not use any never_say phrase from the objective.",
    "cta_missing": "Keep the call to action from the objective.",
    "reference_intent_lost": (
        "Stay close to the message, offer and structure of the reference email."
    ),
    "unsafe_content": "Remove pushy, threatening or sensitive-data language.",
    "angle_invalid": (
        "Provide a short angle (one sentence) describing the personalization used."
    ),
    "repeats_previous": "Do not repeat the previous email; add something new.",
    "subject_repeated": "Use a different subject from the previous email.",
    "filler_followup": (
        "Avoid filler such as 'just following up'; add new value instead."
    ),
    "greeting_changed": "Start with exactly the greeting line of the reference.",
    **SHAPE_GUIDANCE,
}


@dataclass(frozen=True)
class ValidationContext:
    objective: PersonalizationConfig
    reference_subject: str
    reference_text: str
    reference_urls: frozenset[str]
    facts: Mapping[str, Fact]
    previous: PreviousEmail | None = None
    require_fact_use: bool = True


@dataclass(frozen=True)
class ValidationResult:
    codes: tuple[str, ...]
    subject: str
    body_html: str
    body_text: str
    facts_used: tuple[Fact, ...]

    @property
    def ok(self) -> bool:
        return not self.codes


def assemble_body_html(
    paragraphs: tuple[str, ...], allowed_urls: frozenset[str]
) -> str:
    """Plain-text paragraphs -> HTML. Text is escaped; only allow-listed URLs
    become links; a single newline becomes <br>."""
    rendered: list[str] = []
    for paragraph in paragraphs:
        pieces: list[str] = []
        cursor = 0
        for start, end, url in find_urls_with_spans(paragraph):
            pieces.append(html.escape(paragraph[cursor:start], quote=True))
            if url in allowed_urls:
                escaped = html.escape(url, quote=True)
                pieces.append(f'<a href="{escaped}">{escaped}</a>')
            else:
                pieces.append(html.escape(url, quote=True))
            cursor = end
        pieces.append(html.escape(paragraph[cursor:], quote=True))
        rendered.append("<p>" + "".join(pieces).replace("\n", "<br>") + "</p>")
    return "".join(rendered)


def _objective_text(objective: PersonalizationConfig) -> str:
    return "\n".join(
        [
            objective.objective,
            objective.offer,
            objective.cta,
            objective.target,
            objective.problem_solved,
            *objective.must_mention,
            # The sender's own words, like the objective: a number or link the
            # reviewer asked for is supported (ADR-0025).
            objective.feedback or "",
        ]
    )


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


def _normalize_subject(subject: str) -> str:
    return re.sub(r"^(re|fwd?):\s*", "", subject.strip().lower())


def validate_generation(
    output: GenerationOutput, ctx: ValidationContext
) -> ValidationResult:
    codes: list[str] = []

    def flag(code: str) -> None:
        if code not in codes:
            codes.append(code)

    subject = (output.subject or "").strip()
    if not subject:
        flag("empty_subject")
    if len(subject) > MAX_SUBJECT_CHARS:
        flag("subject_too_long")
    if _SUBJECT_CONTROL_RE.search(output.subject or ""):
        flag("subject_control_chars")
    if _PLACEHOLDER_MARK_RE.search(subject):
        flag("subject_placeholder")

    paragraphs = tuple(p.strip() for p in output.paragraphs if p and p.strip())
    if not paragraphs:
        flag("empty_body")
    if len(paragraphs) > MAX_PARAGRAPHS:
        flag("too_many_paragraphs")
    if any(len(p) > MAX_PARAGRAPH_CHARS for p in paragraphs):
        flag("paragraph_too_long")
    if any(_CONTROL_RE.search(p) for p in paragraphs):
        flag("body_control_chars")
    body_text = "\n\n".join(paragraphs)
    if len(body_text) > MAX_BODY_CHARS:
        flag("body_too_long")
    if paragraphs and len(body_text.split()) < MIN_BODY_WORDS:
        flag("body_too_short")
    if _PLACEHOLDER_MARK_RE.search(body_text):
        flag("body_placeholder")
    # Craft rules shared with reference drafting (email_style): structure, a
    # question as the ask, no pleasantry openers, and a length tied to the
    # reference being personalized.
    for code in shape_codes(paragraphs):
        flag(code)
    reference_first = split_paragraphs(ctx.reference_text)[:1]
    if (
        paragraphs
        and reference_first
        and _GREETING_RE.match(reference_first[0])
        and _squash(paragraphs[0]) != _squash(reference_first[0])
    ):
        flag("greeting_changed")
    if paragraphs and len(body_text.split()) > max_words_for_reference(
        len(ctx.reference_text.split())
    ):
        flag("body_too_long")

    combined = f"{subject}\n{body_text}"
    allowed_corpus = "\n".join(
        [
            ctx.reference_subject,
            ctx.reference_text,
            _objective_text(ctx.objective),
            *(fact.text for fact in ctx.facts.values()),
        ]
    )
    objective_and_reference = "\n".join(
        [ctx.reference_subject, ctx.reference_text, _objective_text(ctx.objective)]
    )

    if extract_numbers(combined) - extract_numbers(allowed_corpus):
        flag("unsupported_number")
    allowed_urls = frozenset(
        ctx.reference_urls | extract_urls(_objective_text(ctx.objective))
    )
    if extract_urls(combined) - allowed_urls:
        flag("unsupported_url")
    if extract_emails(combined) - extract_emails(objective_and_reference):
        flag("unsupported_email")
    if extract_phones(combined) - extract_phones(objective_and_reference):
        flag("unsupported_phone")

    used: list[Fact] = []
    for fact_id in output.facts_used:
        fact = ctx.facts.get(fact_id)
        if fact is None:
            flag("unknown_fact_id")
        elif fact not in used:
            used.append(fact)
    if ctx.require_fact_use and ctx.facts and not used:
        flag("no_facts_used")

    lowered = combined.lower()
    if any(phrase.lower() not in lowered for phrase in ctx.objective.must_mention):
        flag("missing_must_mention")
    if any(phrase.lower() in lowered for phrase in ctx.objective.never_say):
        flag("never_say_violation")

    cta_urls = extract_urls(ctx.objective.cta)
    cta_tokens = significant_tokens(ctx.objective.cta)
    if cta_urls and not (cta_urls & extract_urls(combined)):
        flag("cta_missing")
    elif len(cta_tokens) >= 2:
        covered = len(cta_tokens & significant_tokens(combined)) / len(cta_tokens)
        if covered < CTA_OVERLAP_MIN:
            flag("cta_missing")

    reference_tokens = significant_tokens(
        f"{ctx.reference_subject}\n{ctx.reference_text}"
    )
    if len(reference_tokens) >= 6:
        covered = len(reference_tokens & significant_tokens(combined)) / len(
            reference_tokens
        )
        if covered < REFERENCE_OVERLAP_MIN:
            flag("reference_intent_lost")

    for match in _UNSAFE_RE.finditer(combined):
        if match.group(0).lower() not in objective_and_reference.lower():
            flag("unsafe_content")
            break

    angle = (output.angle or "").strip()
    if not angle or len(angle) > MAX_ANGLE_CHARS or _SUBJECT_CONTROL_RE.search(angle):
        flag("angle_invalid")

    if ctx.previous is not None:
        if (
            containment(word_shingles(body_text), word_shingles(ctx.previous.body_text))
            >= FOLLOWUP_SIMILARITY_MAX
        ):
            flag("repeats_previous")
        if _normalize_subject(subject) == _normalize_subject(ctx.previous.subject):
            flag("subject_repeated")
        if _FILLER_RE.search(combined):
            flag("filler_followup")

    body_html = ""
    if paragraphs:
        body_html = sanitize_email_html(assemble_body_html(paragraphs, allowed_urls))
        if not html_to_text(body_html):
            flag("empty_body")

    return ValidationResult(
        codes=tuple(codes),
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        facts_used=tuple(used),
    )
