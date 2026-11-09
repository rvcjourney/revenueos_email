from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import get_request_id

# HTTP status for "a third-party mailbox credential was rejected" (bad SMTP/IMAP
# login, revoked or under-scoped Gmail/Microsoft grant). It must NOT be 401: the
# browser client treats every 401 as an invalid platform session and signs the
# user out, but here the user's session is fine and only the mailbox needs fixing.
PROVIDER_AUTH_REJECTED_STATUS = 422


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


def error_body(
    code: str,
    message: str,
    *,
    request_id: str | None,
    details: dict[str, Any] | Sequence[Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details,
        }
    }


_SECRET_KEY_MARKER = "password"
_REDACTED = "[redacted]"


def _redact_secrets(value: Any) -> Any:
    """A copy of `value` with every field whose name contains "password"
    replaced. Validation errors echo the submitted input, and requests such
    as the RevenueOS launch carry login and SMTP passwords."""
    if isinstance(value, dict):
        return {
            key: _REDACTED
            if _SECRET_KEY_MARKER in str(key).lower()
            else _redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _safe_validation_errors(errors: Sequence[Any]) -> list[Any]:
    safe = []
    for error in errors:
        error = dict(error)
        loc = [str(part).lower() for part in error.get("loc", ())]
        if any(_SECRET_KEY_MARKER in part for part in loc):
            error["input"] = _REDACTED
        elif "input" in error:
            error["input"] = _redact_secrets(error["input"])
        safe.append(error)
    return safe


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        request_id = get_request_id() or getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                exc.code,
                exc.message,
                request_id=request_id,
                details=exc.details,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = get_request_id() or getattr(request.state, "request_id", None)
        # Pydantic v2 puts the raised exception object itself (e.g. a
        # ValueError) under details[i]['ctx']['error'] for a custom
        # @field_validator/@model_validator failure -- plain json.dumps
        # (what Starlette's JSONResponse uses) can't serialize that and
        # would turn a clean 422 into a 500. jsonable_encoder is FastAPI's
        # own fallback-to-str()-safe encoder, same as its default handler.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body(
                "validation_error",
                "Request validation failed",
                request_id=request_id,
                details=jsonable_encoder(_safe_validation_errors(exc.errors())),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = get_request_id() or getattr(request.state, "request_id", None)
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                "http_error",
                message,
                request_id=request_id,
                details=None,
            ),
        )

    # Deliberately NOT @app.exception_handler(Exception): FastAPI/Starlette
    # pulls any handler registered under the Exception/500 key out of
    # ExceptionMiddleware and hands it to ServerErrorMiddleware instead (see
    # fastapi.applications.FastAPI.build_middleware_stack), which sits
    # OUTSIDE every app.add_middleware(...) middleware -- CORSMiddleware
    # included. A response built there never passes back through
    # CORSMiddleware, so the browser sees no Access-Control-Allow-Origin
    # header and reports a misleading "blocked by CORS policy" error instead
    # of the real 500, no matter how correctly CORS is configured. The fix is
    # unhandled_exception_middleware (app/core/middleware.py), a normal
    # app.middleware("http") handler that runs inside CORSMiddleware and
    # produces this exact same response shape.


#: Seconds a client is told to wait before retrying a 503 (Retry-After).
SERVICE_UNAVAILABLE_RETRY_AFTER_SECONDS = 2


def service_unavailable_response(request_id: str | None) -> JSONResponse:
    """503 for a transient infrastructure failure (database pool/connection).

    Distinct from the generic 500 so clients know a retry is safe and useful,
    and so the failure is never mistaken for an application bug.
    """
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=error_body(
            "service_unavailable",
            "The service is busy. Please retry.",
            request_id=request_id,
            details=None,
        ),
        headers={"Retry-After": str(SERVICE_UNAVAILABLE_RETRY_AFTER_SECONDS)},
    )


def unhandled_exception_response(request_id: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body(
            "internal_error",
            "Internal server error",
            request_id=request_id,
            details=None,
        ),
    )
