from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from app.core.metrics import metrics
from app.modules.mailboxes.providers.base import ErrorCategory, ProviderSendResult
from app.modules.mailboxes.providers.registry import ProviderRegistry
from app.modules.rate_limit.schemas import RateReservation
from app.modules.scheduler.schemas import SendTaskPayload
from app.modules.sending.retry_policy import classify_and_decide
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
        rfc_message_id="<test-rfc-id@example.com>",
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


def test_retry_backoff_proportional_jitter() -> None:
    """Verifies that exponential backoff applies proportional (+/- 20%) jitter
    and respects minimum (30s) and maximum (3600s) limits."""
    now = datetime.now(UTC)

    # For retry_count = 0, raw_backoff = 30s. Jittered backoff in [24s, 36s]
    # bounded by 30s min -> [30s, 36s]
    delays_attempt_0 = []
    for _ in range(50):
        decision = classify_and_decide(
            error_category=ErrorCategory.RATE_LIMIT,
            is_retryable=True,
            retry_count=0,
            retry_budget=5,
            now=now,
        )
        assert decision.should_retry is True
        assert decision.next_retry_at is not None
        delay = (decision.next_retry_at - now).total_seconds()
        assert 30.0 <= delay <= 36.0001
        delays_attempt_0.append(delay)

    # Multiple calls must not all produce identical values (proves randomness/jitter)
    assert len(set(delays_attempt_0)) > 1

    # For retry_count = 2, raw_backoff = 120s. Jittered backoff in [96s, 144s]
    delays_attempt_2 = []
    for _ in range(50):
        decision = classify_and_decide(
            error_category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
            is_retryable=True,
            retry_count=2,
            retry_budget=5,
            now=now,
        )
        assert decision.should_retry is True
        assert decision.next_retry_at is not None
        delay = (decision.next_retry_at - now).total_seconds()
        assert 96.0 <= delay <= 144.0001
        delays_attempt_2.append(delay)
    assert len(set(delays_attempt_2)) > 1


def test_retry_budget_exhaustion() -> None:
    """When retry_count reaches retry_budget, should_retry is False."""
    decision = classify_and_decide(
        error_category=ErrorCategory.RATE_LIMIT,
        is_retryable=True,
        retry_count=3,
        retry_budget=3,
    )
    assert decision.should_retry is False
    assert decision.next_retry_at is None
    assert decision.terminal_reason == "retry_budget_exhausted"


def test_retry_deadline_horizon_exceeded() -> None:
    """When the message anchor timestamp is older than the 48-hour retry horizon,
    retries cease even if retry attempts remain."""
    now = datetime.now(UTC)
    anchor_old = now - timedelta(hours=49)

    decision = classify_and_decide(
        error_category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
        is_retryable=True,
        retry_count=1,
        retry_budget=5,
        now=now,
        anchor_at=anchor_old,
    )
    assert decision.should_retry is False
    assert decision.next_retry_at is None
    assert decision.terminal_reason == "retry_deadline_exceeded"


def test_provider_retry_after_enforcement() -> None:
    """When a provider provides a Retry-After duration longer than the platform backoff,
    the provider's delay takes precedence."""
    now = datetime.now(UTC)
    # Attempt 0 backoff is ~30s-36s, but provider asks for 300 seconds
    decision = classify_and_decide(
        error_category=ErrorCategory.RATE_LIMIT,
        is_retryable=True,
        retry_count=0,
        retry_budget=3,
        now=now,
        provider_retry_after_seconds=300.0,
    )
    assert decision.should_retry is True
    assert decision.next_retry_at is not None
    delay = (decision.next_retry_at - now).total_seconds()
    assert delay >= 300.0


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


def test_suppression_recheck_during_retry() -> None:
    """If a recipient is added to the suppression list between retry attempts,
    the retry execution gate must catch it, terminal SKIPPED, zero sends."""
    ws = uuid.uuid4()
    mid = uuid.uuid4()
    payload = _payload(workspace_id=ws, message_id=mid)
    ctx = _ctx(workspace_id=ws, message_id=mid, retry_count=1)

    service = _make_service(ctx)
    service.session.execute.return_value.first.return_value = (1,)  # suppressed

    with patch.object(ProviderRegistry, "get") as mock_get_provider:
        outcome = service.execute(payload)

        assert outcome.outcome == "SKIPPED"
        assert outcome.reason == "suppressed"
        mock_get_provider.assert_not_called()
        service.repository.skip_message.assert_called_once()


def test_campaign_pause_recheck_during_retry() -> None:
    """If a campaign is paused between retry attempts, the message must be
    DEFERRED without state destruction or provider send."""
    ws = uuid.uuid4()
    mid = uuid.uuid4()
    payload = _payload(workspace_id=ws, message_id=mid)
    ctx = _ctx(
        workspace_id=ws,
        message_id=mid,
        retry_count=1,
        campaign_status="PAUSED",
    )

    service = _make_service(ctx)

    with patch.object(ProviderRegistry, "get") as mock_get_provider:
        outcome = service.execute(payload)

        assert outcome.outcome == "DEFERRED"
        assert outcome.reason == "campaign_not_running"
        mock_get_provider.assert_not_called()


def test_mailbox_disconnect_recheck_during_retry() -> None:
    """If a mailbox is disconnected between retry attempts, send is DEFERRED."""
    ws = uuid.uuid4()
    mid = uuid.uuid4()
    payload = _payload(workspace_id=ws, message_id=mid)
    ctx = _ctx(
        workspace_id=ws,
        message_id=mid,
        retry_count=1,
        mailbox_connection_state="RECONNECT_REQUIRED",
    )

    service = _make_service(ctx)

    with patch.object(ProviderRegistry, "get") as mock_get_provider:
        outcome = service.execute(payload)

        assert outcome.outcome == "DEFERRED"
        assert outcome.reason == "mailbox_disconnected"
        mock_get_provider.assert_not_called()


def test_mailbox_throttling_cooldown_sets_blocked_until() -> None:
    """When a provider throttles with RATE_LIMIT, the mailbox is blocked
    and throttled metrics are recorded."""
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
        error_category="RATE_LIMIT",
        error_code="rate_limit_exceeded",
        retry_after_seconds=120.0,
    )

    with patch.object(ProviderRegistry, "get", return_value=mock_provider):
        with patch.object(
            service,
            "_load_and_refresh_credential",
            return_value=({"access_token": "tok"}, 1),
        ):
            outcome = service.execute(payload)

            assert outcome.outcome == "RETRY_SCHEDULED"
            service.repository.update_mailbox_blocked_until.assert_called_once()
            assert (
                metrics.get_count("email_provider_throttle_total", provider="GMAIL")
                == 1
            )
            assert (
                metrics.get_count("email_retry_scheduled_total", provider="GMAIL") == 1
            )


def test_mailbox_auth_failure_holds_unsent_work() -> None:
    """When a provider returns AUTH_FAILURE mid-send, the mailbox is marked
    RECONNECT_REQUIRED, health_state DEGRADED, and the message is held in
    RETRY_SCHEDULED with hold_reason='mailbox_reauth_required'."""
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
        error_category="AUTH_FAILURE",
        error_code="invalid_credentials",
    )

    with patch.object(ProviderRegistry, "get", return_value=mock_provider):
        with patch.object(
            service,
            "_load_and_refresh_credential",
            return_value=({"access_token": "tok"}, 1),
        ):
            outcome = service.execute(payload)

            assert outcome.outcome == "RETRY_SCHEDULED"
            service.repository.update_mailbox_connection_state.assert_called_once_with(
                workspace_id=ws,
                mailbox_id=ctx.mailbox_id,
                connection_state="RECONNECT_REQUIRED",
                health_state="DEGRADED",
                connected_generation=None,
                current_connection_generation=1,
            )
            assert metrics.get_count("email_auth_failure_total", provider="GMAIL") == 1
            call_kwargs = service.repository.finalize_attempt_result.call_args.kwargs
            assert call_kwargs["hold_reason"] == "mailbox_reauth_required"
            assert call_kwargs["message_status"] == "RETRY_SCHEDULED"
