"""RevenueOS sample review and approval (ADR-0024): the routes' key, switch and
permission, against a mocked service. What approval stores is proven in
test_revenueos_launch_real_db.py."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.deps import WorkspaceContext, get_db
from app.core.config import reset_settings_cache
from app.main import create_app
from app.modules.integrations.revenueos_approval import RevenueOSSamplesOut
from app.modules.integrations.revenueos_start import RevenueOSCampaignStartOut

WS = uuid.uuid4()
USER = uuid.uuid4()
CAMPAIGN_ID = uuid.uuid4()
KEY = "test-revenueos-key-0123456789abcdef"
HEADERS = {"X-RevenueOS-Key": KEY}
BASE = f"/api/v1/workspaces/{WS}/integrations/revenueos/campaigns/{CAMPAIGN_ID}"


def _client(role: str | None = "OWNER") -> tuple[TestClient, MagicMock]:
    app = create_app()
    db = MagicMock()
    db.execute.return_value.scalar.return_value = role
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


def _configure(monkeypatch: pytest.MonkeyPatch, *, auto_start: str = "true") -> None:
    monkeypatch.setenv("REVENUEOS_INTAKE_KEY", KEY)
    monkeypatch.setenv("REVENUEOS_ACTOR_USER_ID", str(USER))
    monkeypatch.setenv("REVENUEOS_AUTO_START_ENABLED", auto_start)
    reset_settings_cache()


@pytest.fixture
def service() -> Iterator[MagicMock]:
    with patch("app.api.v1.integrations.RevenueOSApprovalService") as cls:
        cls.return_value.samples.return_value = RevenueOSSamplesOut(
            campaign_id=CAMPAIGN_ID,
            campaign_status="DRAFT",
            samples_status="ready",
            approval_status="NONE",
        )
        cls.return_value.approve_and_start.return_value = RevenueOSCampaignStartOut(
            status="started", campaign_id=CAMPAIGN_ID, campaign_status="RUNNING"
        )
        yield cls


def test_samples_are_readable_with_the_key(
    monkeypatch: pytest.MonkeyPatch, service: MagicMock
) -> None:
    # Reading needs no switch: it changes nothing.
    _configure(monkeypatch, auto_start="false")
    client, _ = _client(role="MEMBER")

    response = client.get(f"{BASE}/samples", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["samples_status"] == "ready"
    context, campaign_id = service.return_value.samples.call_args.args
    assert context == WorkspaceContext(
        workspace_id=WS, user_id=USER, role_code="MEMBER"
    )
    assert campaign_id == CAMPAIGN_ID


def test_approve_starts_the_campaign(
    monkeypatch: pytest.MonkeyPatch, service: MagicMock
) -> None:
    _configure(monkeypatch)
    client, _ = _client()

    response = client.post(f"{BASE}/approve", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["status"] == "started"
    service.return_value.approve_and_start.assert_called_once()


def test_approve_is_off_with_the_start_switch(
    monkeypatch: pytest.MonkeyPatch, service: MagicMock
) -> None:
    _configure(monkeypatch, auto_start="false")
    client, db = _client()

    response = client.post(f"{BASE}/approve", headers=HEADERS)

    assert response.status_code == 503
    db.execute.assert_not_called()
    service.return_value.approve_and_start.assert_not_called()


@pytest.mark.parametrize("path", ["/samples", "/approve"])
@pytest.mark.parametrize("headers", [{}, {"X-RevenueOS-Key": "wrong"}])
def test_wrong_key_reads_and_approves_nothing(
    monkeypatch: pytest.MonkeyPatch,
    service: MagicMock,
    path: str,
    headers: dict[str, str],
) -> None:
    _configure(monkeypatch)
    client, db = _client()
    call = client.get if path == "/samples" else client.post

    assert call(f"{BASE}{path}", headers=headers).status_code == 401
    db.execute.assert_not_called()


@pytest.mark.parametrize(("role", "expected"), [("MEMBER", 403), ("VIEWER", 403)])
def test_a_role_that_may_only_draft_cannot_approve(
    monkeypatch: pytest.MonkeyPatch, service: MagicMock, role: str, expected: int
) -> None:
    _configure(monkeypatch)
    client, _ = _client(role=role)

    assert client.post(f"{BASE}/approve", headers=HEADERS).status_code == expected
    service.return_value.approve_and_start.assert_not_called()


def test_the_key_reaches_nothing_outside_the_actors_workspaces(
    monkeypatch: pytest.MonkeyPatch, service: MagicMock
) -> None:
    _configure(monkeypatch)
    client, _ = _client(role=None)

    assert client.get(f"{BASE}/samples", headers=HEADERS).status_code == 404
    assert client.post(f"{BASE}/approve", headers=HEADERS).status_code == 404
