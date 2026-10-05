"""RevenueOS launch (ADR-0022): the payload rules, the route's key and switches,
and the start phase's retry loop, against mocks (no database, no network).
What the request stores is proven in test_revenueos_launch_real_db.py."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import WorkspaceContext, get_db
from app.core.config import reset_settings_cache
from app.core.errors import AppError
from app.main import create_app
from app.modules.integrations import revenueos_launch
from app.modules.integrations.revenueos_launch import (
    RevenueOSLaunchIn,
    RevenueOSLaunchOut,
    RevenueOSLaunchService,
    _payload_hash,
)
from app.modules.integrations.revenueos_start import RevenueOSCampaignStartOut
from app.modules.integrations.supabase_auth_admin import EnsuredUser

ACTOR = uuid.uuid4()
PERSON = uuid.uuid4()
WORKSPACE = uuid.uuid4()
CAMPAIGN_ID = uuid.uuid4()
KEY = "test-revenueos-key-0123456789abcdef"
URL = "/api/v1/integrations/revenueos/launch"
HEADERS = {"X-RevenueOS-Key": KEY}
CTX = WorkspaceContext(workspace_id=WORKSPACE, user_id=ACTOR, role_code="OWNER")


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reference": "LAUNCH-0001",
        "user": {"email": "cam@motm.tech", "workspace_name": "Acme"},
        "recipients": [{"email": "lead@acme.com", "first_name": "Asha"}],
        "campaign": {"name": "Acme intro"},
        "emails": [
            {"subject": "Hello", "body_html": "<p>One</p>"},
            {"subject": "Again", "body_html": "<p>Two</p>", "wait_days_before": 3},
        ],
        "schedule": {
            "timezone": "Asia/Kolkata",
            "weekdays": [1, 2, 3, 4, 5],
            "window_start_local": "09:30:00",
            "window_end_local": "17:30:00",
            "daily_limit": 10,
        },
    }
    body.update(overrides)
    return body


def _payload(**overrides: Any) -> RevenueOSLaunchIn:
    return RevenueOSLaunchIn.model_validate(_body(**overrides))


def _result(**overrides: Any) -> RevenueOSLaunchOut:
    values: dict[str, Any] = {
        "reference": "LAUNCH-0001",
        "client_reference": "LAUNCH-0001",
        "duplicate": False,
        "user_id": PERSON,
        "email": "cam@motm.tech",
        "user_created": True,
        "workspace_id": WORKSPACE,
        "workspace_name": "Acme",
        "role": "ADMIN",
        "campaign_id": CAMPAIGN_ID,
        "campaign_status": "DRAFT",
        "start_status": "blocked",
    }
    values.update(overrides)
    return RevenueOSLaunchOut(**values)


# ---------------------------------------------------------------------------
# Payload rules
# ---------------------------------------------------------------------------


def test_payload_starts_by_default_and_normalises_the_user_email() -> None:
    payload = _payload(user={"email": " CAM@Motm.Tech "})
    assert payload.auto_start is True and payload.smtp is None
    assert payload.user.email == "cam@motm.tech" and payload.user.role == "ADMIN"


@pytest.mark.parametrize(
    "overrides",
    [
        {"recipients": []},
        {"recipients": [{"email": f"l{i}@acme.com"} for i in range(501)]},
        {"user": {"email": "not-an-email"}},
        {"user": {"email": "cam@motm.tech", "role": "OWNER"}},
        {"user": {"email": "cam@motm.tech", "password": "x"}},
        {"emails": [{"subject": "a", "body_html": "<p>a</p>", "wait_days_before": 2}]},
        {
            "emails": [
                {"subject": "a", "body_html": "<p>a</p>"},
                {"subject": "b", "body_html": "<p>b</p>"},
            ]
        },
        {"reference": "  "},
        {"mailbox_id": str(uuid.uuid4())},
    ],
)
def test_payload_rejects_invalid_input(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _payload(**overrides)


def test_payload_hash_covers_campaign_and_recipients_but_not_smtp() -> None:
    smtp = {
        "host": "smtp.gmail.com",
        "security_mode": "STARTTLS",
        "port": 587,
        "username": "s@acme.com",
        "password": "p",
        "email_address": "s@acme.com",
    }
    assert _payload_hash(_payload()) == _payload_hash(_payload(smtp=smtp))
    assert _payload_hash(_payload()) == _payload_hash(_payload(auto_start=False))
    # Same people in another order or letter case are the same audience.
    two = [{"email": "A@acme.com"}, {"email": "b@acme.com"}]
    assert _payload_hash(_payload(recipients=two)) == _payload_hash(
        _payload(recipients=[{"email": "b@acme.com"}, {"email": "a@acme.com"}])
    )
    for other in (
        {"recipients": [{"email": "someone@else.com"}]},
        {"campaign": {"name": "Other"}},
    ):
        assert _payload_hash(_payload()) != _payload_hash(_payload(**other))


# ---------------------------------------------------------------------------
# Start phase
# ---------------------------------------------------------------------------


class _Start:
    def __init__(self, outcomes: list[Any]) -> None:
        self.session = MagicMock()
        self.service = RevenueOSLaunchService(self.session)
        self.outcomes = outcomes
        self.naps: list[float] = []
        self.now = 0.0
        self.starter = MagicMock()
        self.starter.start.side_effect = self._next

    def _next(self, *_: Any) -> Any:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _sleep(self, seconds: float) -> None:
        self.naps.append(seconds)
        self.now += 6.0

    def run(self) -> RevenueOSLaunchOut:
        result = _result()
        with (
            patch.object(
                revenueos_launch, "RevenueOSStartService", return_value=self.starter
            ),
            patch.object(revenueos_launch, "enter_api_scope") as scope,
        ):
            self.service._start(CTX, result, self._sleep, lambda: self.now)
        self.scope = scope
        return result


_CAPTURING = AppError("audience_not_ready", "still being captured", status_code=409)
_STARTED = RevenueOSCampaignStartOut(
    status="started", campaign_id=CAMPAIGN_ID, campaign_status="RUNNING"
)


def test_start_retries_until_the_audience_is_ready() -> None:
    run = _Start([_CAPTURING, _CAPTURING, _STARTED])

    result = run.run()

    assert result.start_status == "started" and result.campaign_status == "RUNNING"
    assert len(run.naps) == 2
    # Every attempt re-enters the API scope: the previous transaction ended.
    assert run.scope.call_count == 3
    assert run.session.rollback.call_count == 2
    run.session.commit.assert_called_once()


def test_start_gives_up_as_pending_after_the_wait() -> None:
    run = _Start([_CAPTURING] * 10)

    result = run.run()

    assert result.start_status == "pending" and result.campaign_status == "DRAFT"
    assert result.start_error == "still being captured"
    # 15 seconds of budget at 6 seconds per nap.
    assert len(run.naps) == 3
    run.session.commit.assert_not_called()


def test_a_refused_start_is_reported_and_not_retried() -> None:
    run = _Start(
        [AppError("preflight_failed", "Campaign is not ready", status_code=422)]
    )

    result = run.run()

    assert result.start_status == "failed"
    assert result.start_error == "Campaign is not ready"
    assert run.naps == []


def test_launch_commits_what_it_stored_before_trying_to_start() -> None:
    session = MagicMock()
    service = RevenueOSLaunchService(session)
    order: list[str] = []
    session.commit.side_effect = lambda: order.append("commit")
    provisioned = MagicMock(
        user_id=PERSON,
        email="cam@motm.tech",
        user_created=True,
        workspace_id=WORKSPACE,
        workspace_name="Acme",
        role="ADMIN",
        mailbox_id=None,
        mailbox_status=None,
        mailbox_error=None,
    )

    def store(_context: Any, _payload: Any, result: RevenueOSLaunchOut) -> None:
        order.append("store")
        result.campaign_id, result.campaign_status = CAMPAIGN_ID, "DRAFT"

    def start(_context: Any, result: RevenueOSLaunchOut, *_: Any) -> None:
        order.append("start")
        result.start_status = "started"

    with (
        patch.object(revenueos_launch, "RevenueOSProvisioningService") as provisioning,
        patch.object(service, "_store", side_effect=store),
        patch.object(service, "_start", side_effect=start),
    ):
        provisioning.return_value.provision.return_value = provisioned
        result = service.launch(
            actor_id=ACTOR,
            payload=_payload(),
            user=EnsuredUser(user_id=PERSON, created=True),
        )

    assert order == ["store", "commit", "start"]
    assert result.start_status == "started"


def test_launch_without_auto_start_stores_a_draft_and_stops() -> None:
    session = MagicMock()
    service = RevenueOSLaunchService(session)

    def store(_context: Any, _payload: Any, result: RevenueOSLaunchOut) -> None:
        result.campaign_id, result.campaign_status = CAMPAIGN_ID, "DRAFT"

    with (
        patch.object(revenueos_launch, "RevenueOSProvisioningService") as provisioning,
        patch.object(service, "_store", side_effect=store),
        patch.object(service, "_start") as start,
    ):
        provisioning.return_value.provision.return_value = MagicMock(
            user_id=PERSON,
            email="cam@motm.tech",
            user_created=True,
            workspace_id=WORKSPACE,
            workspace_name="Acme",
            role="ADMIN",
            mailbox_id=None,
            mailbox_status=None,
            mailbox_error=None,
        )
        result = service.launch(
            actor_id=ACTOR,
            payload=_payload(auto_start=False),
            user=EnsuredUser(user_id=PERSON, created=True),
        )

    assert result.start_status == "not_requested"
    start.assert_not_called()
    session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _client() -> tuple[TestClient, MagicMock]:
    app = create_app()
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


def _configure(
    monkeypatch: pytest.MonkeyPatch, *, provisioning: str, auto_start: str
) -> None:
    monkeypatch.setenv("REVENUEOS_INTAKE_KEY", KEY)
    monkeypatch.setenv("REVENUEOS_ACTOR_USER_ID", str(ACTOR))
    monkeypatch.setenv("REVENUEOS_PROVISIONING_ENABLED", provisioning)
    monkeypatch.setenv("REVENUEOS_AUTO_START_ENABLED", auto_start)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    reset_settings_cache()


@pytest.fixture
def collaborators() -> Iterator[tuple[MagicMock, MagicMock]]:
    with (
        patch("app.api.v1.integrations.SupabaseAuthAdminClient") as admin,
        patch("app.api.v1.integrations.RevenueOSLaunchService") as service,
    ):
        admin.return_value.ensure_user.return_value = EnsuredUser(
            user_id=PERSON, created=True
        )
        service.return_value.launch.return_value = _result(start_status="started")
        yield admin, service


def test_route_launches_as_the_acting_user(
    monkeypatch: pytest.MonkeyPatch, collaborators: tuple[MagicMock, MagicMock]
) -> None:
    _configure(monkeypatch, provisioning="true", auto_start="true")
    admin, service = collaborators
    client, _ = _client()

    response = client.post(URL, json=_body(), headers=HEADERS)

    assert response.status_code == 201
    assert response.json()["start_status"] == "started"
    assert response.json()["campaign_id"] == str(CAMPAIGN_ID)
    admin.return_value.ensure_user.assert_called_once_with("cam@motm.tech")
    kwargs = service.return_value.launch.call_args.kwargs
    assert kwargs["actor_id"] == ACTOR
    assert kwargs["user"] == EnsuredUser(user_id=PERSON, created=True)


def test_route_answers_200_for_a_repeated_reference(
    monkeypatch: pytest.MonkeyPatch, collaborators: tuple[MagicMock, MagicMock]
) -> None:
    _configure(monkeypatch, provisioning="true", auto_start="true")
    collaborators[1].return_value.launch.return_value = _result(
        duplicate=True, start_status="already_started"
    )
    client, _ = _client()

    assert client.post(URL, json=_body(), headers=HEADERS).status_code == 200


@pytest.mark.parametrize(
    ("provisioning", "auto_start", "body_auto_start", "expected"),
    [
        ("false", "true", True, 503),
        # Asking for a running campaign while starting is off is refused whole.
        ("true", "false", True, 503),
        # A draft needs only the provisioning switch.
        ("true", "false", False, 201),
    ],
)
def test_switches_gate_the_route_before_anything_is_created(
    monkeypatch: pytest.MonkeyPatch,
    collaborators: tuple[MagicMock, MagicMock],
    provisioning: str,
    auto_start: str,
    body_auto_start: bool,
    expected: int,
) -> None:
    _configure(monkeypatch, provisioning=provisioning, auto_start=auto_start)
    admin, service = collaborators
    client, db = _client()

    response = client.post(URL, json=_body(auto_start=body_auto_start), headers=HEADERS)

    assert response.status_code == expected
    if expected == 503:
        admin.return_value.ensure_user.assert_not_called()
        service.return_value.launch.assert_not_called()
        db.execute.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"X-RevenueOS-Key": "wrong"}])
def test_wrong_key_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
    collaborators: tuple[MagicMock, MagicMock],
    headers: dict[str, str],
) -> None:
    _configure(monkeypatch, provisioning="true", auto_start="true")
    admin, service = collaborators
    client, db = _client()

    assert client.post(URL, json=_body(), headers=headers).status_code == 401
    admin.return_value.ensure_user.assert_not_called()
    service.return_value.launch.assert_not_called()
    db.execute.assert_not_called()


def test_an_invalid_body_creates_no_account(
    monkeypatch: pytest.MonkeyPatch, collaborators: tuple[MagicMock, MagicMock]
) -> None:
    _configure(monkeypatch, provisioning="true", auto_start="true")
    client, _ = _client()

    response = client.post(URL, json=_body(recipients=[]), headers=HEADERS)

    assert response.status_code == 422
    collaborators[0].return_value.ensure_user.assert_not_called()


def test_client_reference_defaults_to_the_campaign_reference() -> None:
    assert _payload().resolved_client_reference == "LAUNCH-0001"
    grouped = _payload(client_reference=" CLIENT-7 ")
    assert grouped.resolved_client_reference == "CLIENT-7"
    assert grouped.reference == "LAUNCH-0001"
    with pytest.raises(ValidationError):
        _payload(client_reference="   ")
    # The client is not part of the campaign's identity.
    assert _payload_hash(_payload()) == _payload_hash(grouped)


def test_the_workspace_is_provisioned_under_the_client_reference() -> None:
    service = RevenueOSLaunchService(MagicMock())
    with (
        patch.object(revenueos_launch, "RevenueOSProvisioningService") as provisioning,
        patch.object(service, "_store"),
    ):
        provisioning.return_value.provision.return_value = MagicMock(
            user_id=PERSON,
            email="cam@motm.tech",
            user_created=False,
            workspace_id=WORKSPACE,
            workspace_name="Acme",
            role="ADMIN",
            mailbox_id=None,
            mailbox_status=None,
            mailbox_error=None,
        )
        result = service.launch(
            actor_id=ACTOR,
            payload=_payload(client_reference="CLIENT-7", reference="CAMPAIGN-2"),
            user=EnsuredUser(user_id=PERSON, created=False),
        )

    sent = provisioning.return_value.provision.call_args.kwargs["payload"]
    assert sent.reference == "CLIENT-7"
    assert result.reference == "CAMPAIGN-2" and result.client_reference == "CLIENT-7"
