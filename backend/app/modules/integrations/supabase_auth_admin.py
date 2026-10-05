from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from app.core.config import Settings


@dataclass(frozen=True)
class EnsuredUser:
    user_id: UUID
    # False when an account with this email already existed.
    created: bool


class AuthAdminError(Exception):
    """Base for failures of the Supabase Auth admin API."""


class AuthAdminUnavailableError(AuthAdminError):
    """Temporary: network error or 5xx. Retryable."""


class AuthAdminRejectedError(AuthAdminError):
    """Permanent: the service-role key or the email address was refused."""


# Bounds the email lookup for an already-registered address; the admin API
# offers no lookup by email, only paging.
_PAGE_SIZE = 200
_MAX_PAGES = 50


class SupabaseAuthAdminClient:
    """Thin wrapper over the Supabase Auth admin API (ADR-0020).

    Always authenticates with the service-role key, which bypasses every
    signup restriction of the project. Backend-only: it must never be reachable
    from, or expose credentials to, client-facing code.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings.current()

    def _headers(self) -> dict[str, str]:
        key = self._settings.supabase_service_role_key
        return {"Authorization": f"Bearer {key}", "apikey": key}

    def _base_url(self) -> str:
        return f"{self._settings.supabase_url.rstrip('/')}/auth/v1/admin"

    def ensure_user(self, email: str) -> EnsuredUser:
        """Return the account for `email`, creating it when it does not exist.

        A new account is confirmed (it never receives a confirmation email) and
        gets a random password that is not stored, logged or returned: the
        person sets their own through the password-reset flow.

        With REVENUEOS_INITIAL_PASSWORD_IS_EMAIL the first password is the
        email address instead (owner decision, ADR-0020): the person can log
        in at once, and so can anyone who knows the address until they change
        it. An account that already exists is never given a new password.
        """
        password = (
            email
            if self._settings.revenueos_initial_password_is_email
            else secrets.token_urlsafe(32)
        )
        try:
            response = httpx.post(
                f"{self._base_url()}/users",
                headers=self._headers(),
                json={
                    "email": email,
                    "password": password,
                    "email_confirm": True,
                },
                timeout=10.0,
            )
        except httpx.TransportError as exc:
            raise AuthAdminUnavailableError(str(exc)) from exc

        if response.status_code in (200, 201):
            return EnsuredUser(user_id=self._user_id(response), created=True)
        if self._is_email_exists(response):
            return EnsuredUser(user_id=self._find_user_id(email), created=False)
        self._raise_for_status(response, action="create user")
        raise AuthAdminError(f"create user: unexpected {response.status_code}")

    def _find_user_id(self, email: str) -> UUID:
        for page in range(1, _MAX_PAGES + 1):
            try:
                response = httpx.get(
                    f"{self._base_url()}/users",
                    headers=self._headers(),
                    params={"page": page, "per_page": _PAGE_SIZE},
                    timeout=10.0,
                )
            except httpx.TransportError as exc:
                raise AuthAdminUnavailableError(str(exc)) from exc
            self._raise_for_status(response, action="list users")
            users = self._json(response).get("users") or []
            for user in users:
                if str(user.get("email") or "").lower() == email:
                    return UUID(str(user["id"]))
            if len(users) < _PAGE_SIZE:
                break
        raise AuthAdminError("the registered account could not be found by email")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _user_id(self, response: httpx.Response) -> UUID:
        try:
            return UUID(str(self._json(response)["id"]))
        except (KeyError, ValueError) as exc:
            raise AuthAdminError("create user: response carried no user id") from exc

    def _is_email_exists(self, response: httpx.Response) -> bool:
        if response.status_code not in (400, 409, 422):
            return False
        payload = self._json(response)
        if payload.get("error_code") == "email_exists":
            return True
        # Auth servers before error codes only sent this message.
        message = str(payload.get("msg") or payload.get("message") or "")
        return "already been registered" in message

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, action: str) -> None:
        status = response.status_code
        if status < 400:
            return
        if status >= 500 or status == 429:
            raise AuthAdminUnavailableError(f"{action}: {status}")
        raise AuthAdminRejectedError(f"{action}: {status}")
