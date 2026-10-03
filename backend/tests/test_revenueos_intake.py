"""RevenueOS campaign intake (ADR-0019): the route's key-only authentication,
acting user and permission, the payload rules, and the service's ordering,
idempotency and all-or-nothing behaviour, against mocked campaign services (no
database, no task broker)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.api.deps import WorkspaceContext, get_db
from app.core.config import Settings, reset_settings_cache
from app.core.errors import AppError
from app.main import create_app
from app.modules.integrations import revenueos_intake
from app.modules.integrations.revenueos_intake import (
    INTAKE_OPERATION,
    RevenueOSCampaignIntakeIn,
    RevenueOSCampaignIntakeOut,
    RevenueOSIntakeService,
    _payload_hash,
)

WS = uuid.uuid4()
OTHER_WS = uuid.uuid4()
USER = uuid.uuid4()
CAMPAIGN_ID = uuid.uuid4()
AUDIENCE_ID = uuid.uuid4()
RECEIPT_ID = uuid.uuid4()
MAILBOX_ID = uuid.uuid4()
LIST_ID = uuid.uuid4()
KEY = "test-revenueos-key-0123456789abcdef"
CTX = WorkspaceContext(workspace_id=WS, user_id=USER, role_code="MEMBER")


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reference": "STRAT-0042",
        "campaign": {"name": "Meraki - Oct", "description": "from RevenueOS"},
        "emails": [
            {"subject": "Hi {{first_name}}", "body_html": "<p>One</p>"},
            {
                "subject": "Following up",
                "body_html": "<p>Two</p>",
                "wait_days_before": 3,
            },
            {
                "subject": "Last note",
                "body_html": "<p>Three</p>",
                "wait_days_before": 4,
            },
        ],
        "schedule": {
            "timezone": "Asia/Kolkata",
            "weekdays": [1, 2, 3, 4, 5],
            "window_start_local": "09:30:00",
            "window_end_local": "17:30:00",
            "daily_limit": 50,
        },
        "audience": {"list_ids": [str(LIST_ID)], "lead_ids": []},
        "mailbox_id": str(MAILBOX_ID),
    }
    body.update(overrides)
    return body


def _payload(**overrides: Any) -> RevenueOSCampaignIntakeIn:
    return RevenueOSCampaignIntakeIn.model_validate(_body(**overrides))


# ---------------------------------------------------------------------------
# Payload rules
# ---------------------------------------------------------------------------


def test_payload_accepts_the_documented_package() -> None:
    payload = _payload()
    assert [e.wait_days_before for e in payload.emails] == [0, 3, 4]


@pytest.mark.parametrize(
    "emails",
    [
        # First email may not wait.
        [{"subject": "a", "body_html": "<p>a</p>", "wait_days_before": 2}],
        # A follow-up must wait, or the sequence would not alternate.
        [
            {"subject": "a", "body_html": "<p>a</p>"},
            {"subject": "b", "body_html": "<p>b</p>", "wait_days_before": 0},
        ],
        [],
    ],
)
def test_payload_rejects_invalid_waits(emails: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        _payload(emails=emails)


def test_payload_rejects_unknown_fields_and_blank_reference() -> None:
    with pytest.raises(ValidationError):
        _payload(campaign_type="HYPER_PERSONALIZED")
    with pytest.raises(ValidationError):
        _payload(campaign={"name": "x", "campaign_type": "HYPER_PERSONALIZED"})
    with pytest.raises(ValidationError):
        _payload(reference="   ")


def test_payload_hash_is_stable_and_content_sensitive() -> None:
    assert _payload_hash(_payload()) == _payload_hash(_payload())
    assert _payload_hash(_payload()) != _payload_hash(_payload(mailbox_id=str(WS)))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class _Harness:
    """Patches every collaborator of RevenueOSIntakeService and records the
    order they are called in."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.repo = MagicMock()
        self.repo.get_command_receipt.return_value = None
        self.repo.insert_pending_command_receipt.side_effect = self._insert_receipt
        self.repo.complete_command_receipt.side_effect = self._record("receipt.done")
        self.repo.get_campaign.return_value = {
            "id": CAMPAIGN_ID,
            "status": "DRAFT",
            "version": 3,
            "draft_audience_id": None,
        }
        self.campaigns = MagicMock()
        self.campaigns.create_campaign.side_effect = self._create_campaign
        self.sequence = MagicMock()
        self.sequence.add_step.side_effect = self._record("step")
        self.settings = MagicMock()
        self.settings.create_settings_version.side_effect = self._record("settings")
        self.mailboxes = MagicMock()
        self.mailboxes.assign_mailbox.side_effect = self._record("mailbox")
        self.audiences = MagicMock()
        self.audiences.select_audience.side_effect = self._select_audience

    def _record(self, name: str) -> Any:
        def _call(*args: Any, **kwargs: Any) -> None:
            self.calls.append(name)

        return _call

    def _create_campaign(self, context: Any, payload: Any) -> Any:
        self.calls.append("campaign")
        return MagicMock(id=CAMPAIGN_ID)

    def _insert_receipt(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("receipt.pending")
        return {"id": RECEIPT_ID}

    def _select_audience(self, context: Any, campaign_id: Any, payload: Any) -> Any:
        self.calls.append("audience")
        return MagicMock(id=AUDIENCE_ID, status="CAPTURING")

    def service(self) -> RevenueOSIntakeService:
        return RevenueOSIntakeService(MagicMock())


@pytest.fixture
def harness() -> Iterator[_Harness]:
    h = _Harness()
    with (
        patch.object(revenueos_intake, "CampaignRepository", return_value=h.repo),
        patch.object(revenueos_intake, "CampaignService", return_value=h.campaigns),
        patch.object(revenueos_intake, "SequenceService", return_value=h.sequence),
        patch.object(
            revenueos_intake, "CampaignSettingsService", return_value=h.settings
        ),
        patch.object(
            revenueos_intake, "CampaignMailboxService", return_value=h.mailboxes
        ),
        patch.object(revenueos_intake, "AudienceService", return_value=h.audiences),
    ):
        yield h


def test_store_builds_a_standard_draft_in_order(harness: _Harness) -> None:
    result = harness.service().store_campaign(CTX, _payload())

    assert result == RevenueOSCampaignIntakeOut(
        reference="STRAT-0042",
        campaign_id=CAMPAIGN_ID,
        campaign_status="DRAFT",
        audience_id=AUDIENCE_ID,
        audience_status="CAPTURING",
        duplicate=False,
    )
    # The audience capture task is enqueued only after everything else is
    # written, including the completed receipt.
    assert harness.calls == [
        "campaign",
        "receipt.pending",
        "step",
        "step",
        "step",
        "settings",
        "mailbox",
        "receipt.done",
        "audience",
    ]

    created = harness.campaigns.create_campaign.call_args.args[1]
    assert created.campaign_type == "STANDARD"
    assert created.name == "Meraki - Oct"

    steps = [call.args[2] for call in harness.sequence.add_step.call_args_list]
    assert [s.kind for s in steps] == ["EMAIL", "EMAIL", "EMAIL"]
    assert [s.position for s in steps] == [1, 2, 4]
    assert [s.leading_wait_minutes for s in steps] == [None, 3 * 1440, 4 * 1440]
    assert steps[0].email_subject == "Hi {{first_name}}"

    receipt = harness.repo.insert_pending_command_receipt.call_args.kwargs
    assert receipt["workspace_id"] == WS
    assert receipt["actor_id"] == USER
    assert receipt["operation"] == INTAKE_OPERATION
    assert receipt["request_key"] == "STRAT-0042"
    assert receipt["resource_id"] == CAMPAIGN_ID
    assert harness.repo.complete_command_receipt.call_args.kwargs == {
        "workspace_id": WS,
        "receipt_id": RECEIPT_ID,
        "response_version": 3,
    }
    assert harness.mailboxes.assign_mailbox.call_args.args[2].mailbox_id == MAILBOX_ID
    assert harness.audiences.select_audience.call_args.args[2].list_ids == [LIST_ID]


def test_every_lookup_and_write_is_scoped_to_the_callers_workspace(
    harness: _Harness,
) -> None:
    harness.service().store_campaign(CTX, _payload())

    lookup = harness.repo.get_command_receipt.call_args.kwargs
    assert lookup["workspace_id"] == WS and lookup["actor_id"] == USER
    for collaborator in (
        harness.campaigns.create_campaign,
        harness.sequence.add_step,
        harness.settings.create_settings_version,
        harness.mailboxes.assign_mailbox,
        harness.audiences.select_audience,
    ):
        assert collaborator.call_args.args[0] is CTX


def test_replay_of_the_same_reference_creates_nothing(harness: _Harness) -> None:
    payload = _payload()
    harness.repo.get_command_receipt.return_value = {
        "id": RECEIPT_ID,
        "status": "COMPLETED",
        "payload_hash": _payload_hash(payload),
        "resource_id": CAMPAIGN_ID,
    }
    harness.repo.get_campaign.return_value = {
        "id": CAMPAIGN_ID,
        "status": "DRAFT",
        "version": 3,
        "draft_audience_id": None,
    }
    harness.repo.get_latest_audience.return_value = {
        "id": AUDIENCE_ID,
        "status": "READY",
    }

    result = harness.service().store_campaign(CTX, payload)

    assert result.duplicate is True
    assert result.campaign_id == CAMPAIGN_ID
    assert (result.audience_id, result.audience_status) == (AUDIENCE_ID, "READY")
    assert harness.calls == []
    harness.campaigns.create_campaign.assert_not_called()
    harness.audiences.select_audience.assert_not_called()


def test_replay_reports_the_committed_audience_when_there_is_one(
    harness: _Harness,
) -> None:
    payload = _payload()
    committed = uuid.uuid4()
    harness.repo.get_command_receipt.return_value = {
        "id": RECEIPT_ID,
        "status": "COMPLETED",
        "payload_hash": _payload_hash(payload),
        "resource_id": CAMPAIGN_ID,
    }
    harness.repo.get_campaign.return_value = {
        "id": CAMPAIGN_ID,
        "status": "RUNNING",
        "version": 9,
        "draft_audience_id": committed,
    }
    harness.repo.get_audience.return_value = {"id": committed, "status": "READY"}

    result = harness.service().store_campaign(CTX, payload)

    assert result.campaign_status == "RUNNING"
    assert result.audience_id == committed
    assert harness.repo.get_audience.call_args.kwargs["workspace_id"] == WS


def test_same_reference_with_different_content_is_refused(harness: _Harness) -> None:
    harness.repo.get_command_receipt.return_value = {
        "id": RECEIPT_ID,
        "status": "COMPLETED",
        "payload_hash": "0" * 64,
        "resource_id": CAMPAIGN_ID,
    }

    with pytest.raises(AppError) as excinfo:
        harness.service().store_campaign(CTX, _payload())

    assert excinfo.value.status_code == 409
    harness.campaigns.create_campaign.assert_not_called()


def test_replay_never_recreates_a_removed_campaign(harness: _Harness) -> None:
    payload = _payload()
    harness.repo.get_command_receipt.return_value = {
        "id": RECEIPT_ID,
        "status": "COMPLETED",
        "payload_hash": _payload_hash(payload),
        "resource_id": CAMPAIGN_ID,
    }
    harness.repo.get_campaign.return_value = None

    with pytest.raises(AppError) as excinfo:
        harness.service().store_campaign(CTX, payload)

    assert excinfo.value.status_code == 409
    harness.campaigns.create_campaign.assert_not_called()


def test_concurrent_request_with_the_same_reference_is_a_conflict(
    harness: _Harness,
) -> None:
    harness.repo.insert_pending_command_receipt.side_effect = IntegrityError(
        "insert", {}, Exception("command_receipts_identity_key")
    )

    with pytest.raises(AppError) as excinfo:
        harness.service().store_campaign(CTX, _payload())

    assert excinfo.value.status_code == 409
    harness.sequence.add_step.assert_not_called()
    harness.audiences.select_audience.assert_not_called()


def test_a_rejected_part_stops_before_the_capture_task_is_enqueued(
    harness: _Harness,
) -> None:
    # e.g. a mailbox id from another workspace: invisible there, so 422.
    harness.mailboxes.assign_mailbox.side_effect = AppError(
        "validation_error", "Mailbox not found in this workspace", status_code=422
    )

    with pytest.raises(AppError) as excinfo:
        harness.service().store_campaign(CTX, _payload())

    assert excinfo.value.status_code == 422
    harness.repo.complete_command_receipt.assert_not_called()
    harness.audiences.select_audience.assert_not_called()


# ---------------------------------------------------------------------------
# Route: key-only authentication, acting user, permission, status codes
# ---------------------------------------------------------------------------


def _client(role: str | None = "MEMBER") -> tuple[TestClient, MagicMock]:
    """A client whose database answers app_current_workspace_role() with
    `role` (None = the acting user is not an active member of that workspace)."""
    app = create_app()
    db = MagicMock()
    db.execute.return_value.scalar.return_value = role
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


def _url(workspace_id: uuid.UUID = WS) -> str:
    return f"/api/v1/workspaces/{workspace_id}/integrations/revenueos/campaigns"


HEADERS = {"X-RevenueOS-Key": KEY}


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVENUEOS_INTAKE_KEY", KEY)
    monkeypatch.setenv("REVENUEOS_ACTOR_USER_ID", str(USER))
    reset_settings_cache()


@pytest.fixture
def service_mock() -> Iterator[MagicMock]:
    with patch("app.api.v1.integrations.RevenueOSIntakeService") as cls:
        cls.return_value.store_campaign.return_value = RevenueOSCampaignIntakeOut(
            reference="STRAT-0042",
            campaign_id=CAMPAIGN_ID,
            campaign_status="DRAFT",
            audience_id=AUDIENCE_ID,
            audience_status="CAPTURING",
            duplicate=False,
        )
        yield cls


@pytest.mark.parametrize(("key", "actor"), [("", str(USER)), (KEY, ""), ("", "")])
def test_route_is_disabled_until_key_and_actor_are_configured(
    monkeypatch: pytest.MonkeyPatch, service_mock: MagicMock, key: str, actor: str
) -> None:
    monkeypatch.setenv("REVENUEOS_INTAKE_KEY", key)
    monkeypatch.setenv("REVENUEOS_ACTOR_USER_ID", actor)
    reset_settings_cache()
    client, db = _client()

    response = client.post(_url(), json=_body(), headers=HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "integration_not_configured"
    db.execute.assert_not_called()
    service_mock.return_value.store_campaign.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-RevenueOS-Key": "wrong"},
        {"X-RevenueOS-Key": KEY + "x"},
        # A user token is not a substitute for the key.
        {"Authorization": "Bearer some-user-token"},
    ],
)
def test_route_rejects_a_missing_or_wrong_key_before_touching_the_database(
    configured: None, service_mock: MagicMock, headers: dict[str, str]
) -> None:
    client, db = _client()

    response = client.post(_url(), json=_body(), headers=headers)

    assert response.status_code == 401
    assert KEY not in response.text
    db.execute.assert_not_called()
    service_mock.return_value.store_campaign.assert_not_called()


def test_the_key_grants_nothing_outside_the_actors_memberships(
    configured: None, service_mock: MagicMock
) -> None:
    # A workspace the acting user is not a member of looks nonexistent.
    outsider, _ = _client(role=None)
    response = outsider.post(_url(OTHER_WS), json=_body(), headers=HEADERS)
    assert response.status_code == 404

    viewer, _ = _client(role="VIEWER")
    assert viewer.post(_url(), json=_body(), headers=HEADERS).status_code == 403

    service_mock.return_value.store_campaign.assert_not_called()


def test_route_acts_as_the_configured_user_in_the_requested_workspace(
    configured: None, service_mock: MagicMock
) -> None:
    client, db = _client()

    response = client.post(_url(), json=_body(), headers=HEADERS)

    assert response.status_code == 201
    assert response.json() == {
        "status": "stored",
        "reference": "STRAT-0042",
        "campaign_id": str(CAMPAIGN_ID),
        "campaign_status": "DRAFT",
        "audience_id": str(AUDIENCE_ID),
        "audience_status": "CAPTURING",
        "duplicate": False,
    }
    context, payload = service_mock.return_value.store_campaign.call_args.args
    assert context == WorkspaceContext(
        workspace_id=WS, user_id=USER, role_code="MEMBER"
    )
    assert payload.reference == "STRAT-0042"
    # The RLS context was bound to the configured user and the URL's workspace.
    bound = {
        call.args[1]["name"]: call.args[1]["value"]
        for call in db.execute.call_args_list
        if len(call.args) > 1 and "name" in call.args[1]
    }
    assert bound == {"app.user_id": str(USER), "app.workspace_id": str(WS)}


def test_the_acting_user_cannot_be_chosen_by_the_caller(
    configured: None, service_mock: MagicMock
) -> None:
    client, _ = _client()
    body = _body()
    body["user_id"] = str(uuid.uuid4())

    assert client.post(_url(), json=body, headers=HEADERS).status_code == 422
    service_mock.return_value.store_campaign.assert_not_called()


def test_route_answers_200_for_a_replayed_reference(
    configured: None, service_mock: MagicMock
) -> None:
    stored = service_mock.return_value.store_campaign.return_value
    service_mock.return_value.store_campaign.return_value = stored.model_copy(
        update={"duplicate": True}
    )
    client, _ = _client()

    response = client.post(_url(), json=_body(), headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["duplicate"] is True


def test_route_rejects_an_invalid_package(
    configured: None, service_mock: MagicMock
) -> None:
    body = _body()
    body["emails"][1]["wait_days_before"] = 0
    client, _ = _client()

    response = client.post(_url(), json=body, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    service_mock.return_value.store_campaign.assert_not_called()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _settings(**overrides: str) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://x",
        supabase_url="https://x.supabase.co",
        **overrides,
    )


def test_a_weak_key_or_malformed_actor_stops_the_process_at_boot() -> None:
    with pytest.raises(ValidationError):
        _settings(revenueos_intake_key="short")
    with pytest.raises(ValidationError):
        _settings(revenueos_actor_user_id="not-a-uuid")
    ok = _settings(revenueos_intake_key=KEY, revenueos_actor_user_id=f" {USER} ")
    assert ok.revenueos_actor_user_id == str(USER)
    assert KEY not in repr(ok)
