"""RevenueOS campaign start (ADR-0021): the service's ordering and refusals and
the route's key, switch and permission, against mocked campaign services (no
database, no task broker). Committing an audience and activating a campaign are
proven by those services' own tests."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.deps import WorkspaceContext, get_db
from app.core.config import Settings, reset_settings_cache
from app.core.errors import AppError
from app.main import create_app
from app.modules.integrations import revenueos_start
from app.modules.integrations.revenueos_start import (
    RevenueOSCampaignStartOut,
    RevenueOSStartService,
)

WS = uuid.uuid4()
USER = uuid.uuid4()
CAMPAIGN_ID = uuid.uuid4()
AUDIENCE_ID = uuid.uuid4()
KEY = "test-revenueos-key-0123456789abcdef"
CTX = WorkspaceContext(workspace_id=WS, user_id=USER, role_code="OWNER")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class _Harness:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.repo = MagicMock()
        self.campaign: dict[str, Any] = {
            "id": CAMPAIGN_ID,
            "status": "DRAFT",
            "version": 4,
            "draft_audience_id": None,
            "activated_audience_id": None,
        }
        self.audience: dict[str, Any] | None = {"id": AUDIENCE_ID, "status": "READY"}
        self.repo.get_campaign.side_effect = lambda **_: dict(self.campaign)
        self.repo.get_latest_audience.side_effect = lambda **_: self.audience
        self.audiences = MagicMock()
        self.audiences.commit_audience.side_effect = self._commit
        self.activation = MagicMock()
        self.activation.activate.side_effect = self._activate
        self.service = RevenueOSStartService(MagicMock())
        self.service.repo = self.repo

    def _commit(self, *_: Any) -> None:
        self.calls.append("commit")
        # The touch trigger bumps the campaign version on commit.
        self.campaign.update(draft_audience_id=AUDIENCE_ID, version=5)

    def _activate(self, *_: Any) -> MagicMock:
        self.calls.append("activate")
        return MagicMock(status="RUNNING")


@pytest.fixture
def harness() -> Iterator[_Harness]:
    h = _Harness()
    with (
        patch.object(revenueos_start, "AudienceService", return_value=h.audiences),
        patch.object(
            revenueos_start, "CampaignActivationService", return_value=h.activation
        ),
    ):
        yield h


def test_start_commits_the_ready_audience_then_activates(harness: _Harness) -> None:
    result = harness.service.start(CTX, CAMPAIGN_ID)

    assert harness.calls == ["commit", "activate"]
    assert result == RevenueOSCampaignStartOut(
        status="started",
        campaign_id=CAMPAIGN_ID,
        campaign_status="RUNNING",
        audience_id=AUDIENCE_ID,
    )
    harness.audiences.commit_audience.assert_called_once_with(
        CTX, CAMPAIGN_ID, AUDIENCE_ID
    )
    context, campaign_id, payload, key = harness.activation.activate.call_args.args
    assert context == CTX and campaign_id == CAMPAIGN_ID
    # The version as it is AFTER the commit, and no start_at: start now, on the
    # campaign's own schedule.
    assert payload.expected_version == 5 and payload.start_at is None
    assert str(CAMPAIGN_ID) in key


def test_an_audience_a_person_already_confirmed_is_not_committed_again(
    harness: _Harness,
) -> None:
    harness.campaign["draft_audience_id"] = AUDIENCE_ID

    harness.service.start(CTX, CAMPAIGN_ID)

    assert harness.calls == ["activate"]
    assert harness.activation.activate.call_args.args[2].expected_version == 4


@pytest.mark.parametrize("status", ["RUNNING", "SCHEDULED"])
def test_starting_twice_changes_nothing(harness: _Harness, status: str) -> None:
    harness.campaign.update(status=status, activated_audience_id=AUDIENCE_ID)

    result = harness.service.start(CTX, CAMPAIGN_ID)

    assert result.status == "already_started" and result.campaign_status == status
    assert harness.calls == []


@pytest.mark.parametrize("status", ["PAUSED", "ERROR", "COMPLETED", "ARCHIVED"])
def test_a_campaign_a_person_stopped_is_never_restarted(
    harness: _Harness, status: str
) -> None:
    harness.campaign["status"] = status

    with pytest.raises(AppError) as excinfo:
        harness.service.start(CTX, CAMPAIGN_ID)

    assert excinfo.value.status_code == 409 and excinfo.value.code == "state_conflict"
    assert harness.calls == []


def test_capturing_audience_asks_the_caller_to_retry(harness: _Harness) -> None:
    harness.audience = {"id": AUDIENCE_ID, "status": "CAPTURING"}

    with pytest.raises(AppError) as excinfo:
        harness.service.start(CTX, CAMPAIGN_ID)

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "audience_not_ready"
    assert harness.calls == []


@pytest.mark.parametrize("audience", [None, {"id": AUDIENCE_ID, "status": "FAILED"}])
def test_a_missing_or_failed_audience_is_refused(
    harness: _Harness, audience: dict[str, Any] | None
) -> None:
    harness.audience = audience

    with pytest.raises(AppError) as excinfo:
        harness.service.start(CTX, CAMPAIGN_ID)

    assert excinfo.value.status_code == 422
    assert harness.calls == []


def test_an_unknown_campaign_is_not_found(harness: _Harness) -> None:
    harness.repo.get_campaign.side_effect = lambda **_: None

    with pytest.raises(AppError) as excinfo:
        harness.service.start(CTX, CAMPAIGN_ID)

    assert excinfo.value.status_code == 404
    assert harness.repo.get_campaign.call_args.kwargs["workspace_id"] == WS


def test_a_failed_preflight_surfaces_and_starts_nothing(harness: _Harness) -> None:
    refusal = AppError("preflight_failed", "Campaign is not ready", status_code=422)
    harness.activation.activate.side_effect = refusal

    with pytest.raises(AppError) as excinfo:
        harness.service.start(CTX, CAMPAIGN_ID)

    assert excinfo.value is refusal


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

HEADERS = {"X-RevenueOS-Key": KEY}
URL = f"/api/v1/workspaces/{WS}/integrations/revenueos/campaigns/{CAMPAIGN_ID}/start"


def _client(role: str | None = "OWNER") -> tuple[TestClient, MagicMock]:
    app = create_app()
    db = MagicMock()
    db.execute.return_value.scalar.return_value = role
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVENUEOS_INTAKE_KEY", KEY)
    monkeypatch.setenv("REVENUEOS_ACTOR_USER_ID", str(USER))
    monkeypatch.setenv("REVENUEOS_AUTO_START_ENABLED", "true")
    reset_settings_cache()


@pytest.fixture
def service() -> Iterator[MagicMock]:
    with patch("app.api.v1.integrations.RevenueOSStartService") as cls:
        cls.return_value.start.return_value = RevenueOSCampaignStartOut(
            status="started",
            campaign_id=CAMPAIGN_ID,
            campaign_status="RUNNING",
            audience_id=AUDIENCE_ID,
        )
        yield cls


def test_route_starts_as_the_acting_user(configured: None, service: MagicMock) -> None:
    client, _ = _client()

    response = client.post(URL, headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["status"] == "started"
    assert response.json()["campaign_status"] == "RUNNING"
    context, campaign_id = service.return_value.start.call_args.args
    assert context == CTX and campaign_id == CAMPAIGN_ID


def test_auto_start_is_off_by_default() -> None:
    assert Settings.model_fields["revenueos_auto_start_enabled"].default is False


def test_a_disabled_route_touches_no_database(
    monkeypatch: pytest.MonkeyPatch, service: MagicMock
) -> None:
    monkeypatch.setenv("REVENUEOS_INTAKE_KEY", KEY)
    monkeypatch.setenv("REVENUEOS_ACTOR_USER_ID", str(USER))
    monkeypatch.setenv("REVENUEOS_AUTO_START_ENABLED", "false")
    reset_settings_cache()
    client, db = _client()

    response = client.post(URL, headers=HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "integration_not_configured"
    db.execute.assert_not_called()
    service.return_value.start.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"X-RevenueOS-Key": "wrong"}])
def test_route_rejects_a_missing_or_wrong_key(
    configured: None, service: MagicMock, headers: dict[str, str]
) -> None:
    client, db = _client()

    assert client.post(URL, headers=headers).status_code == 401
    db.execute.assert_not_called()
    service.return_value.start.assert_not_called()


@pytest.mark.parametrize(("role", "status_code"), [("MEMBER", 403), ("VIEWER", 403)])
def test_an_acting_user_who_may_only_draft_cannot_start(
    configured: None, service: MagicMock, role: str, status_code: int
) -> None:
    client, _ = _client(role=role)

    assert client.post(URL, headers=HEADERS).status_code == status_code
    service.return_value.start.assert_not_called()


def test_the_key_starts_nothing_outside_the_actors_workspaces(
    configured: None, service: MagicMock
) -> None:
    client, _ = _client(role=None)

    assert client.post(URL, headers=HEADERS).status_code == 404
    service.return_value.start.assert_not_called()


@pytest.mark.parametrize("role", ["MANAGER", "ADMIN", "OWNER"])
def test_roles_that_may_activate_in_the_app_may_start(
    configured: None, service: MagicMock, role: str
) -> None:
    client, _ = _client(role=role)

    assert client.post(URL, headers=HEADERS).status_code == 200
