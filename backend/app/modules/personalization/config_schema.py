"""The campaign objective a user defines once for a hyper-personalized campaign,
and the digest that binds a sample approval to it (ADR-0011)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.modules.personalization.brand_kit import (
    MAX_URL_CHARS,
    BrandKit,
    clean_display_text,
    clean_url,
)
from app.modules.personalization.version import PROMPT_VERSION

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

Phrase = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]

EmailFormat = Literal["TEXT", "HTML"]
CompanySource = Literal["WEBSITE", "MANUAL"]

# The database CHECK is 16384 bytes of the jsonb text form. Stay well under it so
# a valid-looking save is never rejected by the database with an opaque error.
MAX_CONFIG_BYTES = 15_000

# The fields the per-lead prompt has always received. The company profile, brand
# kit and email format must never reach that prompt, so its bytes (and
# PROMPT_VERSION, and every existing approval) stay unchanged (ADR-0016).
_OBJECTIVE_CORE_FIELDS = (
    "objective",
    "offer",
    "cta",
    "target",
    "problem_solved",
    "tone",
    "must_mention",
    "never_say",
)


def _clean(value: str) -> str:
    if _CONTROL_RE.search(value):
        raise ValueError("must not contain control characters")
    return value.strip()


class CompanyProfile(BaseModel):
    """What the company does, from its website or from text the user typed.

    Reviewed and editable by the user before it is saved; it grounds reference
    drafting (ADR-0016)."""

    model_config = ConfigDict(extra="forbid")

    source: CompanySource
    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    company_name: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=1000)
    services: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=8
    )
    industries: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=6
    )
    audience: str = Field(default="", max_length=400)
    tone_of_voice: str = Field(default="", max_length=200)
    key_messages: list[Annotated[str, Field(min_length=1, max_length=150)]] = Field(
        default_factory=list, max_length=5
    )

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return clean_url(value)

    @field_validator("company_name", "summary", "audience", "tone_of_voice")
    @classmethod
    def _text(cls, value: str) -> str:
        return clean_display_text(value)

    @field_validator("services", "industries", "key_messages")
    @classmethod
    def _lists(cls, value: list[str]) -> list[str]:
        return [clean_display_text(item) for item in value]


class PersonalizationConfig(BaseModel):
    """What the user is trying to achieve (ADR-0011), plus the optional company
    profile, email format and brand kit that AI drafting uses (ADR-0016/0017)."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=1000)
    offer: str = Field(min_length=1, max_length=1000)
    cta: str = Field(min_length=1, max_length=500)
    target: str = Field(default="", max_length=500)
    problem_solved: str = Field(default="", max_length=1000)
    tone: str = Field(default="", max_length=200)
    must_mention: list[Phrase] = Field(default_factory=list, max_length=10)
    never_say: list[Phrase] = Field(default_factory=list, max_length=10)
    email_format: EmailFormat | None = None
    company: CompanyProfile | None = None
    brand: BrandKit | None = None
    # A reviewer's notes on earlier samples (ADR-0025). Part of the stored
    # config, so the approval digest covers it and every lead's email uses it.
    feedback: str | None = Field(default=None, max_length=4000)

    @field_validator("objective", "offer", "cta", "target", "problem_solved", "tone")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        return _clean(value)

    @field_validator("feedback")
    @classmethod
    def _feedback(cls, value: str | None) -> str | None:
        return (_clean(value) or None) if value is not None else None

    @field_validator("must_mention", "never_say")
    @classmethod
    def _phrases(cls, value: list[str]) -> list[str]:
        cleaned = [_clean(item) for item in value]
        if len({item.lower() for item in cleaned}) != len(cleaned):
            raise ValueError("must not contain duplicates")
        return cleaned

    @model_validator(mode="after")
    def _within_size_budget(self) -> PersonalizationConfig:
        if config_size_bytes(self.canonical()) > MAX_CONFIG_BYTES:
            raise ValueError(
                "the objective, company details and brand settings are too large"
            )
        return self

    def canonical(self) -> dict[str, Any]:
        """The stored form. Unset optional keys are omitted, so a configuration
        saved before those keys existed, and one saved again unchanged, is
        byte-identical and therefore keeps its approval digest."""
        return self.model_dump(mode="json", exclude_none=True)

    def objective_core(self) -> dict[str, Any]:
        """Only the fields the per-lead prompt is built from."""
        full = self.canonical()
        return {name: full[name] for name in _OBJECTIVE_CORE_FIELDS}

    def generation_objective(self) -> dict[str, Any]:
        """The per-lead prompt's objective: the core, plus the reviewer's feedback
        only when there is some, so the prompt of every campaign without feedback
        is byte-identical to before (ADR-0025)."""
        core = self.objective_core()
        if self.feedback:
            core["feedback"] = self.feedback
        return core


def config_size_bytes(canonical: Mapping[str, Any]) -> int:
    """Size in the database's jsonb text form (", " and ": " separators, raw
    UTF-8), which is what the 16 KiB CHECK measures."""
    return len(
        json.dumps(canonical, ensure_ascii=False, separators=(", ", ": ")).encode(
            "utf-8"
        )
    )


def parse_config(raw: Mapping[str, Any] | None) -> PersonalizationConfig | None:
    """None when there is no objective yet; raises ValidationError when the
    stored value is not a valid objective."""
    if not raw:
        return None
    return PersonalizationConfig.model_validate(dict(raw))


def compute_approval_digest(
    *,
    config: Mapping[str, Any] | None,
    steps: Sequence[Mapping[Any, Any]],
    model: str,
) -> str:
    """Server-computed identity of everything a sample approval vouches for:
    the objective, every email step's reference content, the prompt version and
    the model. Any edit changes it, so an approval is stale without any write."""
    email_steps = [
        {
            "position": step["position"],
            "subject": step.get("email_subject"),
            "body": step.get("email_body_html"),
            "preheader": step.get("email_preheader") or None,
        }
        for step in sorted(steps, key=lambda s: s["position"])
        if step["kind"] == "EMAIL"
    ]
    payload = json.dumps(
        {
            "config": dict(config) if config else None,
            "steps": email_steps,
            "prompt_version": PROMPT_VERSION,
            "model": model,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
