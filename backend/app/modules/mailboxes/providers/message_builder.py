from __future__ import annotations

import re
from email.message import EmailMessage
from uuid import uuid4

from app.core.errors import AppError
from app.modules.mailboxes.providers.base import OutboundMessageEnvelope

_CRLF_PATTERN = re.compile(r"[\r\n]")


def validate_header_value(name: str, value: str | None) -> None:
    """Ensure no carriage return or newline characters exist in header values.

    Shared by every provider adapter (Gmail, Microsoft Graph, SMTP) so the
    CRLF/header-injection guard is defined once and applied identically
    regardless of transport (raw MIME, Graph JSON, or SMTP DATA).
    """
    if value and _CRLF_PATTERN.search(value):
        raise AppError(
            "header_injection",
            f"Header injection detected: newline characters not permitted in {name}",
            status_code=422,
        )


def normalize_message_id(value: str | None) -> str | None:
    """Canonical form for comparing RFC Message-IDs: no angle brackets or
    whitespace, lower-case. Providers and our own send path disagree on
    brackets and case, and matching must not depend on either."""
    if not value:
        return None
    cleaned = value.strip().strip("<>").strip().lower()
    return cleaned or None


def sql_normalized_message_id(column: str, *, sqlite: bool) -> str:
    """SQL expression equivalent of ``normalize_message_id`` for a column."""
    trim = "TRIM" if sqlite else "BTRIM"
    return f"LOWER({trim}({column}, '<> '))"


def generate_message_id(from_address: str) -> str:
    """A globally unique RFC 5322 Message-ID on the sender's own domain."""
    domain = ""
    if "@" in from_address:
        domain = from_address.rsplit("@", 1)[-1].strip().lower()
    domain = re.sub(r"[^a-z0-9.-]", "", domain) or "outly.local"
    return f"<{uuid4().hex}@{domain}>"


def build_rfc5322_message(envelope: OutboundMessageEnvelope) -> EmailMessage:
    """Build a safe RFC 5322 MIME message from a provider-neutral envelope.

    Callers must run ``validate_header_value`` over the envelope's
    recipient/sender/subject fields before calling this -- it does not
    re-validate, it only assembles.
    """
    msg = EmailMessage()
    if envelope.from_name:
        msg["From"] = f"{envelope.from_name} <{envelope.from_address}>"
    else:
        msg["From"] = envelope.from_address
    msg["To"] = envelope.to_address
    msg["Subject"] = envelope.subject

    if envelope.rfc_message_id:
        rfc_id = envelope.rfc_message_id.strip("<>")
        msg["Message-ID"] = f"<{rfc_id}>"

    for name, value in envelope.extra_headers:
        validate_header_value(name, name)
        validate_header_value(name, value)
        msg[name] = value

    if envelope.body_html:
        msg.set_content(
            envelope.body_text or "This message requires an HTML-capable email client."
        )
        msg.add_alternative(envelope.body_html, subtype="html")
    else:
        msg.set_content(envelope.body_text or "")

    inline = [a for a in envelope.attachments if a.content_id]
    regular = [a for a in envelope.attachments if not a.content_id]
    if inline:
        html_part = msg.get_body(preferencelist=("html",))
        if html_part is not None:
            for image in inline:
                maintype, _, subtype = image.content_type.partition("/")
                # multipart/related: the image travels with the HTML that
                # references it as cid:<content_id>.
                html_part.add_related(
                    image.data,
                    maintype,
                    subtype,
                    cid=f"<{image.content_id}>",
                    filename=image.filename,
                    disposition="inline",
                )
    for attachment in regular:
        maintype, _, subtype = attachment.content_type.partition("/")
        msg.add_attachment(
            attachment.data, maintype, subtype, filename=attachment.filename
        )

    return msg
