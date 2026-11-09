import logging
from html import escape
from typing import Any

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import Settings
from app.core.errors import AppError
from app.modules.events.unsubscribe_service import UnsubscribeService
from app.modules.unsubscribe.tokens import parse_unsubscribe_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/unsubscribe", tags=["unsubscribe"])

_INVALID_MESSAGE = "This unsubscribe link is invalid or has expired."


_VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'
_BUTTON_STYLE = (
    "background: #e53e3e; color: white; padding: 10px 20px; border: none; "
    "border-radius: 5px; cursor: pointer; font-size: 16px;"
)


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8">{_VIEWPORT}
<title>{escape(title)}</title></head>
<body style="font-family: sans-serif; text-align: center; padding: 50px;">
{body}
</body>
</html>""",
        status_code=status_code,
    )


def _wants_html(accept: str | None) -> bool:
    return bool(accept and "text/html" in accept)


def _is_valid_token(token: str) -> bool:
    return (
        parse_unsubscribe_token(token, Settings.current().unsubscribe_signing_key)
        is not None
    )


def verified_token(token: str) -> str:
    """Reject a forged or malformed token BEFORE a database connection is taken.

    Listed ahead of `get_db` in the endpoint signature, so FastAPI resolves it
    first: a bad request costs a signature check and never touches the pool.
    """
    if not _is_valid_token(token):
        raise AppError("not_found", _INVALID_MESSAGE, status_code=404)
    return token


@router.get("/{token}", response_class=Response)
def get_unsubscribe_page(
    token: str,
    accept: str | None = Header(default=None),
) -> Response:
    """Public confirmation view. No database access.

    GET never unsubscribes: automated link scanners fetch every URL in an email,
    and must not opt recipients out. The recipient confirms with a POST.
    """
    valid = _is_valid_token(token)

    if accept and "application/json" in accept:
        if not valid:
            return Response(
                content='{"valid": false, "message": "Link expired or invalid"}',
                media_type="application/json",
                status_code=404,
            )
        return Response(
            content='{"valid": true, "message": "Confirmation required"}',
            media_type="application/json",
            status_code=200,
        )

    if not valid:
        return _page(
            "Invalid link",
            f"<h2>Invalid or Expired Link</h2><p>{escape(_INVALID_MESSAGE)}</p>",
            status_code=404,
        )

    return _page(
        "Confirm unsubscribe",
        f"""<h2>Confirm Unsubscribe</h2>
<p>Are you sure you want to stop receiving emails from this sender?</p>
<form method="POST" action="/api/v1/unsubscribe/{escape(token, quote=True)}">
    <button type="submit" style="{_BUTTON_STYLE}">
        Unsubscribe
    </button>
</form>""",
    )


@router.post("/{token}", response_model=None)
def post_unsubscribe(
    token: str = Depends(verified_token),
    db: Session = Depends(get_db),
    accept: str | None = Header(default=None),
) -> Any:
    """Execute immediate durable unsubscribe.

    Supports RFC 8058 One-Click unsubscribe (mail providers POST here with
    `List-Unsubscribe=One-Click`) and the confirmation form above. Repeating it
    is harmless. Answers a page to a browser and JSON to everything else.
    """
    result = UnsubscribeService(db).execute_unsubscribe(token)

    if not result.get("success"):
        if _wants_html(accept):
            return _page(
                "Invalid link",
                f"<h2>Invalid or Expired Link</h2><p>{escape(_INVALID_MESSAGE)}</p>",
                status_code=404,
            )
        raise AppError(
            "not_found",
            str(result.get("message", _INVALID_MESSAGE)),
            status_code=404,
        )

    if _wants_html(accept):
        return _page(
            "Unsubscribed",
            "<h2>You have been unsubscribed</h2>"
            "<p>You will not receive further emails from this sender.</p>",
        )
    return result
