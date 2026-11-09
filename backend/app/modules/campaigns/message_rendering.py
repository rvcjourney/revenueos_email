from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from app.modules.templates.rendering import (
    inject_preheader,
    render_preheader,
    render_template_content,
)


class RenderedMessageContent(NamedTuple):
    subject: str
    body_html: str
    content_digest: str


def render_step_content(
    *,
    subject: str,
    body_html: str,
    frozen_variables: Mapping[str, Any],
    renderer_version: int,
    preheader: str | None = None,
    thread_subject: str | None = None,
) -> RenderedMessageContent:
    """Render a frozen sequence step's content against an enrollment's frozen
    variables.

    Reuses the same deterministic renderer templates already use
    (app.modules.templates.rendering.render_template_content) -- variable
    substitution/escaping logic is not reimplemented here.

    The pre-header is folded into the rendered body as a hidden leading element
    *before* the digest is computed. The send gate recomputes the digest from
    the stored body, so it covers the pre-header with no gate/column change, and
    a step without a pre-header renders byte-identically to before.

    `thread_subject` (a step sent in the same thread, ADR-0021) is the finished
    "Re: ..." subject: it is used as-is, never rendered as a template.
    """
    rendered_subject, rendered_body = render_template_content(
        subject=subject if thread_subject is None else "",
        body_html=body_html,
        context_data=frozen_variables,
    )
    if thread_subject is not None:
        rendered_subject = thread_subject
    rendered_body = inject_preheader(
        rendered_body, render_preheader(preheader, frozen_variables)
    )
    content_digest = compute_content_digest(
        rendered_subject, rendered_body, renderer_version
    )
    return RenderedMessageContent(rendered_subject, rendered_body, content_digest)


def reply_subject(subject: str) -> str:
    """The subject of an email sent as a reply in the same thread (ADR-0021 D3):
    "Re: " + the previous email's subject, never "Re: Re:", within the column's
    500 characters."""
    clean = subject.strip()
    if not clean.lower().startswith("re:"):
        clean = f"Re: {clean}"
    return clean[:500]


def compute_content_digest(subject: str, body_html: str, renderer_version: int) -> str:
    """The message content digest the send gate recomputes from the stored
    subject, body and renderer version. Shared by template rendering and by
    model-generated content so both hash identically."""
    digest_input = f"{subject}\x00{body_html}\x00{renderer_version}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def compute_sequence_content_digest(
    steps: Sequence[Mapping[str, Any]],
    *,
    personalization_config: Mapping[str, Any] | None = None,
) -> str:
    """A stable sha256 over a sequence's step content, satisfying
    campaign_sequences_frozen_check's ^[0-9a-f]{64}$ shape.

    Computed once at activation freeze time. Independent of the input list's
    ordering (sorted by position here) but sensitive to any content change.
    """
    canonical = []
    for step in sorted(steps, key=lambda s: s["position"]):
        entry: dict[str, Any] = {
            "position": step["position"],
            "kind": step["kind"],
            "email_subject": step.get("email_subject"),
            "email_body_html": step.get("email_body_html"),
            "wait_duration_minutes": step.get("wait_duration_minutes"),
        }
        # Only present when set, so sequences frozen before pre-headers existed
        # (and steps without one) hash exactly as they always did.
        if step.get("email_preheader"):
            entry["email_preheader"] = step["email_preheader"]
        # Same rule for the same-thread switch (ADR-0021): only when on.
        if step.get("email_same_thread"):
            entry["email_same_thread"] = True
        # Attachment identity (not bytes): content id + sha256 pin the exact files
        # the frozen sequence will send. Absent when there are none.
        if step.get("attachments"):
            entry["attachments"] = sorted(
                (
                    {
                        "content_id": a["content_id"],
                        "sha256": a["sha256"],
                        "disposition": a["disposition"],
                    }
                    for a in step["attachments"]
                ),
                key=lambda a: (a["disposition"], a["content_id"]),
            )
        canonical.append(entry)
    if personalization_config is not None:
        # Only present for hyper-personalized sequences, so every existing
        # (standard) sequence digest is byte-identical to before. The objective
        # and prompt version are frozen with the reference templates (ADR-0011).
        from app.modules.personalization.version import PROMPT_VERSION

        payload = json.dumps(
            {
                "steps": canonical,
                "personalization": dict(personalization_config),
                "prompt_version": PROMPT_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
