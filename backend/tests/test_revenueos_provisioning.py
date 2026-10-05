"""RevenueOS user provisioning (ADR-0020): the payload rules, the route's key,
on/off switch and ordering, and the Supabase Auth admin client, against mocks
(no database, no network). The database behaviour is proven separately in
test_revenueos_provisioning_real_db.py."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import get_db
from app.core.config import Settings, reset_settings_cache
from app.main import create_app
from app.modules.integrations.revenueos_provisioning import (
    RevenueOSUserProvisionIn,
    RevenueOSUserProvisionOut,
    _bootstrap_request_key,
    _payload_hash,
)
from app.modules.integrations.supabase_auth_admin import (
    AuthAdminError,
    AuthAdminRejectedError,
    AuthAdminUnavailableError,
    EnsuredUser,
    SupabaseAuthAdminClient,
)

ACTOR = uuid.uuid4()
PERSON = uuid.uuid4()
WORKSPACE = uuid.uuid4()
KEY = "test-revenueos-key-0123456789abcdef"
SERVICE_KEY = "service-role-key-for-tests"
URL = "/api/v1/integrations/revenueos/users"
HEADERS = {"X-RevenueOS-Key": KEY}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"reference": "CLIENT-0017", "email": "cam@motm.tech"}
    body.update(overrides)
    return body


def _payload(**overrides: Any) -> RevenueOSUserProvisionIn:
    return RevenueOSUserProvisionIn.model_validate(_body(**overrides))


# ---------------------------------------------------------------------------
# Payload rules
# ---------------------------------------------------------------------------


def test_payload_defaults_to_admin_and_an_email_named_workspace() -> None:
    payload = _payload(email="  CAM@Motm.Tech ")
    assert payload.email == "cam@motm.tech"
    assert payload.role == "ADMIN"
    assert payload.resolved_workspace_name == "cam@motm.tech Workspace"
    assert _payload(workspace_name=" Acme ").resolved_workspace_name == "Acme"


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"email": "a@b"},
        {"email": "a b@motm.tech"},
        {"email": "a@b@motm.tech"},
        {"reference": "   "},
        {"workspace_name": "   "},
        # The acting user owns the workspace; OWNER is never granted.
        {"role": "OWNER"},
        # A password is neither accepted nor returned.
        {"password": "hunter2"},
    ],
)
def test_payload_rejects_invalid_input(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _payload(**overrides)


def test_payload_hash_covers_what_would_change_the_outcome() -> None:
    assert _payload_hash(_payload()) == _payload_hash(_payload())
    # An omitted name and the name it defaults to are the same request.
    assert _payload_hash(_payload()) == _payload_hash(
        _payload(workspace_name="cam@motm.tech Workspace")
    )
    for other in (
        {"email": "x@motm.tech"},
        {"role": "VIEWER"},
        {"workspace_name": "Z"},
    ):
        assert _payload_hash(_payload()) != _payload_hash(_payload(**other))


def test_bootstrap_key_fits_the_ledger_for_the_longest_reference() -> None:
    assert len(_bootstrap_request_key("r" * 200)) <= 200
    assert _bootstrap_request_key("a") != _bootstrap_request_key("b")


def test_response_has_no_field_that_could_carry_a_password() -> None:
    assert not any("pass" in name for name in RevenueOSUserProvisionOut.model_fields)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _client() -> tuple[TestClient, MagicMock]:
    app = create_app()
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVENUEOS_INTAKE_KEY", KEY)
    monkeypatch.setenv("REVENUEOS_ACTOR_USER_ID", str(ACTOR))
    monkeypatch.setenv("REVENUEOS_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", SERVICE_KEY)
    reset_settings_cache()


@pytest.fixture
def auth_admin() -> Iterator[MagicMock]:
    with patch("app.api.v1.integrations.SupabaseAuthAdminClient") as cls:
        cls.return_value.ensure_user.return_value = EnsuredUser(
            user_id=PERSON, created=True
        )
        yield cls


@pytest.fixture
def service() -> Iterator[MagicMock]:
    with patch("app.api.v1.integrations.RevenueOSProvisioningService") as cls:
        cls.return_value.provision.return_value = RevenueOSUserProvisionOut(
            reference="CLIENT-0017",
            user_id=PERSON,
            email="cam@motm.tech",
            workspace_id=WORKSPACE,
            workspace_name="cam@motm.tech Workspace",
            role="ADMIN",
            user_created=True,
            duplicate=False,
        )
        yield cls


def test_route_provisions_as_the_configured_acting_user(
    configured: None, auth_admin: MagicMock, service: MagicMock
) -> None:
    client, _ = _client()

    response = client.post(URL, json=_body(), headers=HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert body["workspace_id"] == str(WORKSPACE) and body["user_id"] == str(PERSON)
    assert body["duplicate"] is False and "password" not in response.text
    auth_admin.return_value.ensure_user.assert_called_once_with("cam@motm.tech")
    kwargs = service.return_value.provision.call_args.kwargs
    assert kwargs["actor_id"] == ACTOR
    assert kwargs["user"] == EnsuredUser(user_id=PERSON, created=True)


def test_route_answers_200_for_a_repeated_reference(
    configured: None, auth_admin: MagicMock, service: MagicMock
) -> None:
    service.return_value.provision.return_value.duplicate = True
    client, _ = _client()

    assert client.post(URL, json=_body(), headers=HEADERS).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-RevenueOS-Key": "wrong"}, {"Authorization": "Bearer some-user-token"}],
)
def test_wrong_key_creates_no_account_and_touches_no_database(
    configured: None,
    auth_admin: MagicMock,
    service: MagicMock,
    headers: dict[str, str],
) -> None:
    client, db = _client()

    response = client.post(URL, json=_body(), headers=headers)

    assert response.status_code == 401
    auth_admin.return_value.ensure_user.assert_not_called()
    service.return_value.provision.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    ("enabled", "service_key"), [("false", SERVICE_KEY), ("true", "")]
)
def test_route_is_off_unless_enabled_and_able_to_create_accounts(
    monkeypatch: pytest.MonkeyPatch,
    auth_admin: MagicMock,
    service: MagicMock,
    enabled: str,
    service_key: str,
) -> None:
    monkeypatch.setenv("REVENUEOS_INTAKE_KEY", KEY)
    monkeypatch.setenv("REVENUEOS_ACTOR_USER_ID", str(ACTOR))
    monkeypatch.setenv("REVENUEOS_PROVISIONING_ENABLED", enabled)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service_key)
    reset_settings_cache()
    client, db = _client()

    response = client.post(URL, json=_body(), headers=HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "integration_not_configured"
    auth_admin.return_value.ensure_user.assert_not_called()
    db.execute.assert_not_called()


def test_provisioning_is_off_by_default() -> None:
    assert Settings.model_fields["revenueos_provisioning_enabled"].default is False


def test_an_invalid_body_creates_no_account(
    configured: None, auth_admin: MagicMock, service: MagicMock
) -> None:
    client, _ = _client()

    response = client.post(URL, json=_body(role="OWNER"), headers=HEADERS)

    assert response.status_code == 422
    auth_admin.return_value.ensure_user.assert_not_called()


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (AuthAdminUnavailableError("timeout"), 503),
        (AuthAdminRejectedError("create user: 422"), 422),
        (AuthAdminError("no id"), 502),
    ],
)
def test_account_service_failures_stop_before_the_database(
    configured: None,
    auth_admin: MagicMock,
    service: MagicMock,
    error: Exception,
    status_code: int,
) -> None:
    auth_admin.return_value.ensure_user.side_effect = error
    client, db = _client()

    response = client.post(URL, json=_body(), headers=HEADERS)

    assert response.status_code == status_code
    assert SERVICE_KEY not in response.text
    service.return_value.provision.assert_not_called()
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Supabase Auth admin client
# ---------------------------------------------------------------------------


def _admin(*, password_is_email: bool = False) -> SupabaseAuthAdminClient:
    settings = MagicMock()
    settings.revenueos_initial_password_is_email = password_is_email
    settings.supabase_url = "https://project.supabase.co/"
    settings.supabase_service_role_key = SERVICE_KEY
    return SupabaseAuthAdminClient(settings)


def _response(status_code: int, payload: Any) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_new_account_is_confirmed_with_a_random_password() -> None:
    with patch("httpx.post", return_value=_response(200, {"id": str(PERSON)})) as post:
        first = _admin().ensure_user("cam@motm.tech")
        _admin().ensure_user("cam@motm.tech")

    assert first == EnsuredUser(user_id=PERSON, created=True)
    call = post.call_args_list[0]
    assert call.args[0] == "https://project.supabase.co/auth/v1/admin/users"
    assert call.kwargs["headers"]["apikey"] == SERVICE_KEY
    sent = call.kwargs["json"]
    assert sent["email"] == "cam@motm.tech" and sent["email_confirm"] is True
    assert len(sent["password"]) >= 32
    # A fresh password every time: nothing is derived from the email.
    assert sent["password"] != post.call_args_list[1].kwargs["json"]["password"]


@pytest.mark.parametrize(
    "duplicate",
    [
        _response(422, {"error_code": "email_exists", "msg": "exists"}),
        _response(
            422, {"msg": "A user with this email address has already been registered"}
        ),
    ],
)
def test_existing_account_is_found_by_email(duplicate: httpx.Response) -> None:
    page = {
        "users": [
            {"id": str(uuid.uuid4()), "email": "other@motm.tech"},
            {"id": str(PERSON), "email": "CAM@motm.tech"},
        ]
    }
    with (
        patch("httpx.post", return_value=duplicate),
        patch("httpx.get", return_value=_response(200, page)) as get,
    ):
        user = _admin().ensure_user("cam@motm.tech")

    assert user == EnsuredUser(user_id=PERSON, created=False)
    assert get.call_args.kwargs["params"]["page"] == 1


def test_lookup_pages_until_the_account_is_found() -> None:
    full = {
        "users": [
            {"id": str(uuid.uuid4()), "email": f"u{i}@x.test"} for i in range(200)
        ]
    }
    last = {"users": [{"id": str(PERSON), "email": "cam@motm.tech"}]}
    with (
        patch(
            "httpx.post", return_value=_response(422, {"error_code": "email_exists"})
        ),
        patch("httpx.get", side_effect=[_response(200, full), _response(200, last)]),
    ):
        assert _admin().ensure_user("cam@motm.tech").user_id == PERSON


def test_lookup_that_finds_nothing_is_an_error_not_a_guess() -> None:
    with (
        patch(
            "httpx.post", return_value=_response(422, {"error_code": "email_exists"})
        ),
        patch("httpx.get", return_value=_response(200, {"users": []})),
        pytest.raises(AuthAdminError),
    ):
        _admin().ensure_user("cam@motm.tech")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_response(500, {}), AuthAdminUnavailableError),
        (_response(429, {}), AuthAdminUnavailableError),
        (_response(401, {}), AuthAdminRejectedError),
        (_response(422, {"error_code": "validation_failed"}), AuthAdminRejectedError),
        (_response(200, {"email": "no id here"}), AuthAdminError),
    ],
)
def test_failures_are_classified(
    response: httpx.Response, expected: type[Exception]
) -> None:
    with patch("httpx.post", return_value=response), pytest.raises(expected):
        _admin().ensure_user("cam@motm.tech")


def test_network_errors_are_retryable() -> None:
    with (
        patch("httpx.post", side_effect=httpx.ConnectError("down")),
        pytest.raises(AuthAdminUnavailableError),
    ):
        _admin().ensure_user("cam@motm.tech")


# ---------------------------------------------------------------------------
# Optional SMTP block
# ---------------------------------------------------------------------------

SMTP = {
    "host": "smtp.gmail.com",
    "security_mode": "STARTTLS",
    "port": 587,
    "username": "sales@acme.com",
    "password": "app-password",
    "email_address": "sales@acme.com",
    "sender_display_name": "Acme Sales",
}


def test_payload_accepts_the_connect_form_as_an_smtp_block() -> None:
    payload = _payload(smtp=SMTP)
    assert payload.smtp is not None and payload.smtp.port == 587
    assert _payload().smtp is None


@pytest.mark.parametrize(
    "smtp",
    [
        {**SMTP, "port": 25},
        {**SMTP, "security_mode": "NONE"},
        {key: value for key, value in SMTP.items() if key != "password"},
        {**SMTP, "email_address": "no-at-sign"},
    ],
)
def test_payload_rejects_an_invalid_smtp_block(smtp: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _payload(smtp=smtp)


def test_smtp_details_are_not_part_of_the_references_identity() -> None:
    # Otherwise a changed password would be refused as a conflicting request.
    assert _payload_hash(_payload()) == _payload_hash(_payload(smtp=SMTP))
    assert _payload_hash(_payload(smtp=SMTP)) == _payload_hash(
        _payload(smtp={**SMTP, "password": "rotated", "host": "smtp2.acme.com"})
    )


def test_route_reports_the_mailbox_without_echoing_the_smtp_password(
    configured: None, auth_admin: MagicMock, service: MagicMock
) -> None:
    mailbox_id = uuid.uuid4()
    out = service.return_value.provision.return_value
    out.mailbox_id, out.mailbox_status = mailbox_id, "connected"
    client, _ = _client()

    response = client.post(URL, json=_body(smtp=SMTP), headers=HEADERS)

    assert response.status_code == 201
    assert response.json()["mailbox_id"] == str(mailbox_id)
    assert response.json()["mailbox_status"] == "connected"
    assert "app-password" not in response.text
    sent = service.return_value.provision.call_args.kwargs["payload"]
    assert sent.smtp.password == "app-password"


def test_initial_password_is_the_email_only_when_switched_on() -> None:
    assert Settings.model_fields["revenueos_initial_password_is_email"].default is False
    with patch("httpx.post", return_value=_response(200, {"id": str(PERSON)})) as post:
        _admin(password_is_email=True).ensure_user("cam@motm.tech")

    assert post.call_args.kwargs["json"]["password"] == "cam@motm.tech"


def test_an_existing_account_never_gets_a_new_password() -> None:
    page = {"users": [{"id": str(PERSON), "email": "cam@motm.tech"}]}
    with (
        patch(
            "httpx.post", return_value=_response(422, {"error_code": "email_exists"})
        ),
        patch("httpx.get", return_value=_response(200, page)),
        patch("httpx.put") as put,
        patch("httpx.patch") as patch_call,
    ):
        user = _admin(password_is_email=True).ensure_user("cam@motm.tech")

    assert user == EnsuredUser(user_id=PERSON, created=False)
    put.assert_not_called()
    patch_call.assert_not_called()
