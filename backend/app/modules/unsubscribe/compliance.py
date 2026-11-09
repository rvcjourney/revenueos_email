from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from app.core.config import Settings
from app.core.errors import AppError
from app.modules.unsubscribe.tokens import make_unsubscribe_token

UNSUBSCRIBE_PATH_PREFIX = "/api/v1/unsubscribe/"
POSTAL_ADDRESS_MAX_CHARS = 500
FOOTER_TEXT_MAX_CHARS = 300
DEFAULT_FOOTER_TEXT = (
    "If you would rather not hear from us, you can unsubscribe at any time."
)

_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def unsubscribe_ready(settings: Settings) -> bool:
    """Campaign mail is only sent when a valid signed link can be built, so an
    unconfigured environment can never send an email without an opt-out."""
    if not (settings.unsubscribe_signing_key and settings.unsubscribe_base_url):
        return False
    parsed = urlparse(settings.unsubscribe_base_url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def unsubscribe_url(settings: Settings, workspace_id: UUID, message_id: UUID) -> str:
    token = make_unsubscribe_token(
        workspace_id, message_id, settings.unsubscribe_signing_key
    )
    base = settings.unsubscribe_base_url.rstrip("/")
    return f"{base}{UNSUBSCRIBE_PATH_PREFIX}{token}"


def unsubscribe_headers(url: str) -> tuple[tuple[str, str], ...]:
    """RFC 2369 / RFC 8058 headers: mailbox providers show their own unsubscribe
    button, and POST to the URL to opt a recipient out without a page visit."""
    return (
        ("List-Unsubscribe", f"<{url}>"),
        ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
    )


@dataclass(frozen=True)
class ComplianceInfo:
    postal_address: str = ""
    footer_text: str = ""


def read_compliance(defaults: Any) -> ComplianceInfo:
    """The workspace's footer settings from `workspaces.defaults.compliance`.
    Anything malformed reads as "not set" rather than raising."""
    section = defaults.get("compliance") if isinstance(defaults, dict) else None
    if not isinstance(section, dict):
        return ComplianceInfo()
    address = section.get("postal_address")
    text = section.get("footer_text")
    return ComplianceInfo(
        postal_address=address.strip()[:POSTAL_ADDRESS_MAX_CHARS]
        if isinstance(address, str)
        else "",
        footer_text=text.strip()[:FOOTER_TEXT_MAX_CHARS]
        if isinstance(text, str)
        else "",
    )


def validate_compliance_section(section: Any) -> dict[str, str]:
    """Check the `compliance` object before it is saved to `workspaces.defaults`.

    Reading is forgiving (see `read_compliance`); writing is strict, so what is
    stored is always well formed: only the two known text fields, trimmed and
    length-capped. Raises a 422 AppError otherwise.
    """
    if not isinstance(section, dict):
        raise AppError(
            "validation_error", "compliance must be an object", status_code=422
        )
    limits = {
        "postal_address": POSTAL_ADDRESS_MAX_CHARS,
        "footer_text": FOOTER_TEXT_MAX_CHARS,
    }
    unknown = sorted(set(section) - set(limits))
    if unknown:
        raise AppError(
            "validation_error",
            f"Unknown compliance field(s): {', '.join(unknown)}",
            status_code=422,
        )
    clean: dict[str, str] = {}
    for name, max_chars in limits.items():
        value = section.get(name, "")
        if not isinstance(value, str):
            raise AppError(
                "validation_error", f"{name} must be text", status_code=422
            )
        value = value.strip()
        if len(value) > max_chars:
            raise AppError(
                "validation_error",
                f"{name} must be at most {max_chars} characters",
                status_code=422,
            )
        clean[name] = value
    return clean


def build_footer_html(url: str, info: ComplianceInfo) -> str:
    lines = [escape(info.footer_text or DEFAULT_FOOTER_TEXT)]
    if info.postal_address:
        lines.append(
            "<br>".join(escape(part) for part in info.postal_address.splitlines())
        )
    lines.append(
        f'<a href="{escape(url, quote=True)}" style="color:#6b7280">Unsubscribe</a>'
    )
    return (
        '<div style="margin-top:24px;padding-top:12px;border-top:1px solid #e5e7eb;'
        "font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.5;"
        'color:#6b7280">' + "<br>".join(lines) + "</div>"
    )


def build_footer_text(url: str, info: ComplianceInfo) -> str:
    parts = ["--", info.footer_text or DEFAULT_FOOTER_TEXT, f"Unsubscribe: {url}"]
    if info.postal_address:
        parts.append(info.postal_address)
    return "\n".join(parts)


def inject_footer(html: str, footer_html: str) -> str:
    """Add the footer to an HTML body (before </body> when present)."""
    matches = list(_BODY_CLOSE_RE.finditer(html))
    if matches:
        last = matches[-1]
        return html[: last.start()] + footer_html + html[last.start() :]
    return html + footer_html


_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "table", "ul", "ol",
}  # fmt: skip
_SKIP_TAGS = {"script", "style", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            href, shown = self._href, "".join(self._link_text).strip()
            self._href = None
            if href and href.startswith(("http://", "https://")) and href != shown:
                self.parts.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)


def html_to_text(html: str) -> str:
    """A readable plain-text alternative for an HTML body. Spam filters score a
    message with a missing or placeholder text part, and some clients show it."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    text = "".join(extractor.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return collapsed.strip()
