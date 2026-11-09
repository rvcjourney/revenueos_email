from __future__ import annotations

import base64
import hashlib
import hmac
from typing import NamedTuple
from uuid import UUID

# Marks a stateless token. Tokens without it are the older stored-digest kind.
TOKEN_PREFIX = "u1."
_MAC_BYTES = 16
_ID_BYTES = 32  # workspace_id + message_id
# Domain-separates unsubscribe tokens from any other HMAC made with a similar key
# (the open-tracking pixel uses its own context string).
_CONTEXT = b"outly-unsub-v1|"


class UnsubscribeToken(NamedTuple):
    workspace_id: UUID
    message_id: UUID


def _mac(payload: bytes, key: str) -> bytes:
    digest = hmac.new(key.encode("utf-8"), _CONTEXT + payload, hashlib.sha256)
    return digest.digest()[:_MAC_BYTES]


def make_unsubscribe_token(workspace_id: UUID, message_id: UUID, key: str) -> str:
    """Opaque token naming one outbound message. It carries no personal data and
    cannot be forged or reused for another message without the signing key.

    Stateless on purpose: nothing is written when an email is sent (an extra
    commit per send is a real cost), and the link keeps working for as long as
    the email exists, so it has no expiry. The recipient is resolved from the
    message when the link is used.
    """
    if not key:
        raise ValueError("an unsubscribe signing key is required")
    payload = workspace_id.bytes + message_id.bytes
    body = base64.urlsafe_b64encode(payload + _mac(payload, key))
    return TOKEN_PREFIX + body.rstrip(b"=").decode("ascii")


def is_stateless_token(token: str) -> bool:
    return token.startswith(TOKEN_PREFIX)


def parse_unsubscribe_token(token: str, key: str) -> UnsubscribeToken | None:
    """Return the token contents when valid, else None.

    Verified without touching the database, so forged or malformed requests cost
    nothing and can never write.
    """
    if not key or not token or len(token) > 128 or not token.startswith(TOKEN_PREFIX):
        return None
    body = token[len(TOKEN_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) != _ID_BYTES + _MAC_BYTES:
        return None
    payload, mac = raw[:_ID_BYTES], raw[_ID_BYTES:]
    if not hmac.compare_digest(mac, _mac(payload, key)):
        return None
    return UnsubscribeToken(UUID(bytes=payload[:16]), UUID(bytes=payload[16:32]))
