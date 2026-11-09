from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.core.metrics import metrics
from app.modules.mailboxes.providers.base import (
    ErrorCategory,
    ProviderSendResult,
)
from app.modules.mailboxes.providers.registry import ProviderRegistry
from app.modules.rate_limit.schemas import RateReservation
from app.modules.scheduler.schemas import SendTaskPayload
from app.modules.sending.recovery_service import RecoveryService
from app.modules.sending.schemas import LoadedSendContext
from app.modules.sending.service import SendingService
from tests.support.rate_policies import mailbox_daily_cap


def _payload(
    *, workspace_id: uuid.UUID, message_id: uuid.UUID, dispatch_generation: int = 1
) -> SendTaskPayload:
    return SendTaskPayload(
        schema_version=1,
        work_id=str(uuid.uuid4()),
        delivery_id=str(uuid.uuid4()),
        message_id=str(message_id),
        dispatch_id=str(uuid.uuid4()),
        resource_id=str(message_id),
        workspace_id=str(workspace_id),
        dispatch_generation=dispatch_generation,
    )


def _ctx(**overrides: object) -> LoadedSendContext:
    now = datetime.now(UTC)
    subject, body, renderer_version = "Hello", "<p>Hi</p>", 1
    digest = hashlib.sha256(
        f"{subject}\x00{body}\x00{renderer_version}".encode()
    ).hexdigest()
    workspace_id = overrides.get("workspace_id", uuid.uuid4())
    defaults: dict[str, object] = dict(
        message_id=uuid.uuid4(),
        workspace_id=workspace_id,
        purpose="CAMPAIGN",
        campaign_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        mailbox_id=uuid.uuid4(),
        address_id=uuid.uuid4(),
        message_status="QUEUED",
        dispatch_generation=1,
        message_version=5,
        retry_count=0,
        retry_budget=3,
        content_subject=subject,
        content_body_html=body,
        content_digest=digest,
        renderer_version=renderer_version,
        rendered_at=now,
        frozen_destination="lead@example.com",
        frozen_sender_address="sender@example.com",
        frozen_sender_name="Sender",
        rfc_message_id="<fail-inject@example.com>",
        campaign_status="RUNNING",
        campaign_planning_status="READY",
        enrollment_state="ACTIVE",
        canonical_address="lead@example.com",
        mailbox_workspace_id=workspace_id,
        mailbox_provider="GMAIL",
        mailbox_provider_account_id="acct-1",
        mailbox_connection_state="CONNECTED",
        mailbox_health_state="HEALTHY",
        mailbox_policy_state="ENABLED",
        mailbox_policy_reason=None,
        mailbox_circuit_state="CLOSED",
        mailbox_blocked_until=None,
        mailbox_current_connection_generation=1,
        mailbox_original_address="sender@example.com",
        mailbox_sender_display_name="Sender",
        raw={"anchor_at": now},
    )
    defaults.update(overrides)
    return LoadedSendContext(**defaults)  # type: ignore[arg-type]


def _make_service(ctx: LoadedSendContext | None = None) -> SendingService:
    session = MagicMock()
    session.execute.return_value.first.return_value = None
    service = SendingService(session)
    service.repository = MagicMock()
    service.rate_policy_repository = MagicMock()
    service.rate_limiter = MagicMock()

    service.repository.load_message_for_send.return_value = ctx
    service.rate_policy_repository.resolve_applicable_policies.return_value = [
        mailbox_daily_cap()
    ]
    service.rate_limiter.current_generation.return_value = 1
    now = datetime.now(UTC)
    service.rate_limiter.reserve.return_value = RateReservation(
        reservation_id="res-1",
        generation=1,
        granted_at=now,
        expires_at=now,
        scopes=(),
    )
    service.repository.get_rate_control_status.return_value = {
        "generation": 1,
        "status": "READY",
    }
    service.repository.transition_message_to_sending.return_value = True
    return service


def test_provider_5xx_temporary_error_retry() -> None:
    """When a provider returns a 5xx error, the service schedules a retry."""
    metrics.reset()
    ws = uuid.uuid4()
    mid = uuid.uuid4()
    payload = _payload(workspace_id=ws, message_id=mid)
    ctx = _ctx(workspace_id=ws, message_id=mid, retry_count=0)

    service = _make_service(ctx)

    mock_provider = MagicMock()
    mock_provider.capabilities = frozenset()
    mock_provider.send_message.return_value = ProviderSendResult(
        status="DEFINITIVELY_REJECTED",
        error_category="TEMPORARY_PROVIDER_ERROR",
        error_code="backend_error_503",
    )

    with patch.object(ProviderRegistry, "get", return_value=mock_provider):
        with patch.object(
            service,
            "_load_and_refresh_credential",
            return_value=({"access_token": "tok"}, 1),
        ):
            outcome = service.execute(payload)

            assert outcome.outcome == "RETRY_SCHEDULED"
            assert outcome.reason == "TEMPORARY_PROVIDER_ERROR"
            assert (
                metrics.get_count("email_retry_scheduled_total", provider="GMAIL") == 1
            )


def test_provider_4xx_permanent_recipient_failure_terminal() -> None:
    """When a provider returns a permanent recipient failure, mark FAILED."""
    metrics.reset()
    ws = uuid.uuid4()
    mid = uuid.uuid4()
    payload = _payload(workspace_id=ws, message_id=mid)
    ctx = _ctx(workspace_id=ws, message_id=mid, retry_count=0)

    service = _make_service(ctx)

    mock_provider = MagicMock()
    mock_provider.capabilities = frozenset()
    mock_provider.send_message.return_value = ProviderSendResult(
        status="DEFINITIVELY_REJECTED",
        error_category="PERMANENT_RECIPIENT_FAILURE",
        error_code="invalid_recipient_address",
    )

    with patch.object(ProviderRegistry, "get", return_value=mock_provider):
        with patch.object(
            service,
            "_load_and_refresh_credential",
            return_value=({"access_token": "tok"}, 1),
        ):
            outcome = service.execute(payload)

            assert outcome.outcome == "FAILED"
            assert outcome.reason == "PERMANENT_RECIPIENT_FAILURE"
            assert (
                metrics.get_count("email_terminal_failure_total", provider="GMAIL") == 1
            )


def test_provider_timeout_ambiguous_unknown() -> None:
    """When a provider request times out mid-flight, outcome is UNKNOWN."""
    metrics.reset()
    ws = uuid.uuid4()
    mid = uuid.uuid4()
    payload = _payload(workspace_id=ws, message_id=mid)
    ctx = _ctx(workspace_id=ws, message_id=mid, retry_count=0)

    service = _make_service(ctx)

    mock_provider = MagicMock()
    mock_provider.capabilities = frozenset()
    mock_provider.send_message.return_value = ProviderSendResult(
        status="UNKNOWN",
        error_category=ErrorCategory.UNKNOWN_OUTCOME,
        error_code="request_timeout",
    )

    with patch.object(ProviderRegistry, "get", return_value=mock_provider):
        with patch.object(
            service,
            "_load_and_refresh_credential",
            return_value=({"access_token": "tok"}, 1),
        ):
            outcome = service.execute(payload)

            assert outcome.outcome == "UNKNOWN_OUTCOME"
            assert (
                metrics.get_count("email_ambiguous_outcome_total", provider="GMAIL")
                == 1
            )
            # Never retried automatically here
            assert (
                metrics.get_count("email_retry_scheduled_total", provider="GMAIL") == 0
            )


def test_postgres_commit_failure_after_provider_acceptance() -> None:
    """If Postgres commit fails transiently, finalization retries.
    If it fails permanently, the worker raises without re-invoking provider."""
    ws = uuid.uuid4()
    mid = uuid.uuid4()
    payload = _payload(workspace_id=ws, message_id=mid)
    ctx = _ctx(workspace_id=ws, message_id=mid, retry_count=0)

    service = _make_service(ctx)

    mock_provider = MagicMock()
    mock_provider.capabilities = frozenset()
    mock_provider.send_message.return_value = ProviderSendResult(
        status="ACCEPTED",
        provider_message_id="msg-p-123",
        accepted_at=datetime.now(UTC),
    )

    # 1. Transient error recovered on retry
    # First commit call in _run_authorization_transaction succeeds;
    # next commit call fails once, then succeeds on retry.
    service.session.commit.side_effect = [
        None,
        RuntimeError("Transient DB glitch"),
        None,
    ]

    with patch.object(ProviderRegistry, "get", return_value=mock_provider):
        with patch.object(
            service,
            "_load_and_refresh_credential",
            return_value=({"access_token": "tok"}, 1),
        ):
            outcome = service.execute(payload)

            assert outcome.outcome == "SENT"
            # Provider called exactly once
            assert mock_provider.send_message.call_count == 1

    # 2. Permanent error raises without re-calling provider
    service.session.commit.side_effect = [
        None,
        RuntimeError("Permanent DB failure"),
        RuntimeError("Permanent DB failure"),
        RuntimeError("Permanent DB failure"),
    ]
    mock_provider.send_message.reset_mock()

    with patch.object(ProviderRegistry, "get", return_value=mock_provider):
        with patch.object(
            service,
            "_load_and_refresh_credential",
            return_value=({"access_token": "tok"}, 1),
        ):
            with pytest.raises(RuntimeError, match="Permanent DB failure"):
                service.execute(payload)

            # Provider called only once -- NOT repeatedly tried!
            assert mock_provider.send_message.call_count == 1


def test_celery_duplicate_task_delivery_noop() -> None:
    """When Celery redelivers an already resolved task, return safe NOOP."""
    metrics.reset()
    ws = uuid.uuid4()
    mid = uuid.uuid4()
    payload = _payload(workspace_id=ws, message_id=mid)
    # Message is already SENT
    ctx = _ctx(workspace_id=ws, message_id=mid, message_status="SENT")

    service = _make_service(ctx)

    with patch.object(ProviderRegistry, "get") as mock_get_provider:
        outcome = service.execute(payload)

        assert outcome.outcome == "NOOP"
        assert outcome.reason == "already_resolved"
        mock_get_provider.assert_not_called()
        assert (
            metrics.get_count(
                "email_duplicate_task_noop_total", reason="already_resolved"
            )
            == 1
        )


def test_cross_tenant_recovery_isolation() -> None:
    """RecoveryService scoped to Workspace A never touches Workspace B."""
    ws_a = uuid.uuid4()
    now = datetime.now(UTC)

    session = MagicMock()
    # Workspace A query returns empty
    session.execute.return_value.mappings.return_value.all.return_value = []

    recovery = RecoveryService(session)
    recovery.repository = MagicMock()

    recovered_a = recovery.recover_stale_executions(workspace_id=ws_a, now=now)
    assert recovered_a == 0

    # Verify query included AND a.workspace_id = :ws
    call_args = session.execute.call_args
    sql_text = str(call_args[0][0])
    params = call_args[0][1]
    assert "workspace_id = :ws" in sql_text
    assert params["ws"] == str(ws_a)
