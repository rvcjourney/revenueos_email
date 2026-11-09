from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.errors import PROVIDER_AUTH_REJECTED_STATUS, AppError
from app.main import create_app


def test_rejected_mailbox_credentials_are_not_reported_as_an_invalid_session() -> None:
    # The browser client signs the user out on ANY 401. A wrong SMTP/IMAP
    # password (or a revoked Gmail/Microsoft grant) is a mailbox problem, not an
    # invalid platform session, so it must surface with a different status while
    # keeping the `auth_failure` code the connect form maps to friendly copy.
    app = create_app()

    @app.get("/__test__/provider-auth-rejected")
    def _raise() -> None:
        raise AppError(
            "auth_failure",
            "SMTP authentication failed",
            status_code=PROVIDER_AUTH_REJECTED_STATUS,
        )

    response = TestClient(app, raise_server_exceptions=False).get(
        "/__test__/provider-auth-rejected"
    )

    assert response.status_code == PROVIDER_AUTH_REJECTED_STATUS
    assert response.status_code != 401
    assert response.json()["error"]["code"] == "auth_failure"
