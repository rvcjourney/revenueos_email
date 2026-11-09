from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.modules.mailboxes.providers.base import ProviderSendResult
from app.modules.mailboxes.providers.registry import ProviderRegistry
from app.modules.rate_limit.schemas import (
    RateDenial,
    RateLimitDenied,
    RateLimitUnavailable,
    RatePolicySpec,
    RateReservation,
    RateScopeKind,
)
from app.modules.scheduler.schemas import SendTaskPayload
from app.modules.sending.repository import (
    AttemptAlreadyClaimed,
    MessageLockedByAnotherWorker,
)
from app.modules.sending.schemas import LoadedSendContext
from app.modules.sending.service import (
    MAX_DEFERRAL,
    MIN_DEFERRAL,
    SAFETY_HOLD_RETRY,
    SendingService,
    deferral_delay,
)


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
        rfc_message_id="abc@example.com",
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
        mailbox_original_address="mailbox@example.com",
        mailbox_sender_display_name="Mailbox Sender",
        raw={},
    )
    defaults.update(overrides)
    return LoadedSendContext(**defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_provider_registry() -> None:
    yield
    ProviderRegistry.reset()


def _mailbox_policy() -> RatePolicySpec:
    """The daily cap every connected mailbox has (50 per rolling 24 h)."""
    return RatePolicySpec(
        kind=RateScopeKind.MAILBOX,
        source_id=str(uuid.uuid4()),
        scope_key="mailbox-1",
        unit="MESSAGE",
        window_seconds=86_400,
        limit_value=50,
        min_spacing_seconds=60,
    )


def _make_service(ctx: LoadedSendContext | None = None) -> SendingService:
    session = MagicMock()
    # Default: no suppression row found (is_address_suppressed /
    # is_platform_suppressed both do `session.execute(...).first()`).
    # Tests that specifically exercise suppression override this.
    session.execute.return_value.first.return_value = None
    service = SendingService(session, settings=Settings.current())
    service.repository = MagicMock()
    service.rate_policy_repository = MagicMock()
    service.rate_limiter = MagicMock()

    service.repository.load_message_for_send.return_value = ctx
    service.rate_policy_repository.resolve_applicable_policies.return_value = [
        _mailbox_policy()
    ]
    service.rate_limiter.current_generation.return_value = 1
    reservation = RateReservation(
        reservation_id="r1",
        generation=1,
        granted_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
        scopes=(),
    )
    service.rate_limiter.reserve.return_value = reservation
    service.repository.get_rate_control_status.return_value = {
        "generation": 1,
        "status": "READY",
    }
    # Re-fetch inside the authorization transaction returns the same ctx by
    # default; tests override with side_effect where a second, different
    # load is meaningful.
    service.repository.load_message_for_send.side_effect = None
    service.repository.next_attempt_ordinal.return_value = 1
    service.repository.transition_message_to_sending.return_value = True
    return service


# ---------------------------------------------------------------------------
# Message existence / locking
# ---------------------------------------------------------------------------


def test_message_not_found_is_noop_and_never_touches_provider() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ws, mid = uuid.uuid4(), uuid.uuid4()
    service = _make_service()
    service.repository.load_message_for_send.return_value = None

    outcome = service.execute(_payload(workspace_id=ws, message_id=mid))

    assert outcome.outcome == "NOOP"
    assert outcome.reason == "message_not_found"
    fake_provider.send_message.assert_not_called()


def test_message_locked_by_concurrent_worker_is_noop() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ws, mid = uuid.uuid4(), uuid.uuid4()
    service = _make_service()
    service.repository.load_message_for_send.side_effect = MessageLockedByAnotherWorker(
        str(mid)
    )

    outcome = service.execute(_payload(workspace_id=ws, message_id=mid))

    assert outcome.outcome == "NOOP"
    assert outcome.reason == "locked"
    fake_provider.send_message.assert_not_called()


@pytest.mark.parametrize(
    "status", ["SENT", "FAILED", "SKIPPED", "CANCELLED", "UNKNOWN_OUTCOME"]
)
def test_duplicate_delivery_after_resolution_is_noop(status: str) -> None:
    """Redelivery after the message already reached a terminal state must
    never call the provider again."""
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx(message_status=status)
    service = _make_service(ctx)

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "NOOP"
    fake_provider.send_message.assert_not_called()


def test_stale_dispatch_generation_is_noop() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx(dispatch_generation=5)
    service = _make_service(ctx)

    outcome = service.execute(
        _payload(
            workspace_id=ctx.workspace_id,
            message_id=ctx.message_id,
            dispatch_generation=1,
        )
    )

    assert outcome.outcome == "NOOP"
    fake_provider.send_message.assert_not_called()


def test_attempt_already_claimed_is_noop_and_releases_reservation() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _make_service(ctx)
    service.repository.insert_prepared_attempt.side_effect = AttemptAlreadyClaimed(
        str(ctx.message_id)
    )

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "NOOP"
    assert outcome.reason == "already_claimed"
    fake_provider.send_message.assert_not_called()
    service.rate_limiter.release.assert_called_once_with("r1")


# ---------------------------------------------------------------------------
# Rate limiting: must defer, never call provider, and fail closed
# ---------------------------------------------------------------------------


def test_rate_limit_unavailable_defers_and_never_calls_provider() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _make_service(ctx)
    service.rate_limiter.reserve.side_effect = RateLimitUnavailable("redis down")

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "DEFERRED"
    assert outcome.reason == "rate_limit_unavailable"
    fake_provider.send_message.assert_not_called()


def test_rate_limit_denied_defers_and_never_calls_provider() -> None:
    from app.modules.rate_limit.schemas import RateDenial

    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _make_service(ctx)
    service.rate_limiter.reserve.side_effect = RateLimitDenied(
        RateDenial(
            reason="CAPACITY_DENIED",
            failing_kind=None,
            failing_scope_key=None,
            retry_after_seconds=5.0,
        )
    )

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "DEFERRED"
    assert outcome.reason == "rate_limit_denied"
    fake_provider.send_message.assert_not_called()


def _denial(retry_after_seconds: float | None) -> RateLimitDenied:
    return RateLimitDenied(
        RateDenial(
            reason="CAPACITY_DENIED",
            failing_kind=RateScopeKind.MAILBOX,
            failing_scope_key="mailbox-1",
            retry_after_seconds=retry_after_seconds,
        )
    )


def _handed_back(service: SendingService) -> tuple[dict[str, object], float]:
    """The one hand-back the service made, and how far in the future it is due."""
    service.repository.defer_for_capacity.assert_called_once()
    kwargs = service.repository.defer_for_capacity.call_args.kwargs
    return kwargs, (kwargs["due_at"] - datetime.now(UTC)).total_seconds()


def test_a_denied_message_is_handed_back_to_the_scheduler_not_left_queued() -> None:
    """A mailbox at its cap used to leave the message QUEUED until the claim lease
    expired, then claim it again at once. It must be rescheduled for later."""
    ctx = _ctx(dispatch_generation=3)
    service = _make_service(ctx)
    service.rate_limiter.reserve.side_effect = _denial(3600.0)

    outcome = service.execute(
        _payload(
            workspace_id=ctx.workspace_id,
            message_id=ctx.message_id,
            dispatch_generation=3,
        )
    )

    assert (outcome.outcome, outcome.reason) == ("DEFERRED", "rate_limit_denied")
    kwargs, wait = _handed_back(service)
    assert kwargs["workspace_id"] == ctx.workspace_id
    assert kwargs["message_id"] == ctx.message_id
    assert kwargs["dispatch_generation"] == 3
    assert 3600 <= wait <= 3600 * 1.1 + 1
    service.session.commit.assert_called()


def test_deferral_delay_is_clamped_and_jittered() -> None:
    assert MIN_DEFERRAL.total_seconds() <= deferral_delay(None).total_seconds() <= 33.1
    assert MIN_DEFERRAL.total_seconds() <= deferral_delay(0.5).total_seconds() <= 33.1
    assert deferral_delay(-5.0) >= MIN_DEFERRAL
    capped = deferral_delay(10_000_000.0).total_seconds()
    assert MAX_DEFERRAL.total_seconds() <= capped <= MAX_DEFERRAL.total_seconds() * 1.1
    # Jitter spreads a burst: not every call returns the same instant.
    assert len({deferral_delay(600.0) for _ in range(20)}) > 1


def test_a_campaign_mailbox_with_no_daily_cap_never_sends_unlimited() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _make_service(ctx)
    service.rate_policy_repository.resolve_applicable_policies.return_value = []

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert (outcome.outcome, outcome.reason) == ("DEFERRED", "mailbox_limits_missing")
    service.rate_limiter.reserve.assert_not_called()
    fake_provider.send_message.assert_not_called()
    _, wait = _handed_back(service)
    assert wait == pytest.approx(SAFETY_HOLD_RETRY.total_seconds(), abs=5)


def test_a_short_window_pacing_policy_is_not_a_daily_cap() -> None:
    """One message per 10 s paces a mailbox but would still allow thousands a day."""
    ctx = _ctx()
    service = _make_service(ctx)
    pacing_only = RatePolicySpec(
        **{**_mailbox_policy().__dict__, "window_seconds": 10, "limit_value": 1}
    )
    policies = service.rate_policy_repository.resolve_applicable_policies
    policies.return_value = [pacing_only]

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.reason == "mailbox_limits_missing"
    service.rate_limiter.reserve.assert_not_called()


def test_a_message_that_is_not_a_campaign_send_does_not_need_a_mailbox_cap() -> None:
    ctx = _ctx(purpose="CONTROLLED_TEST")
    service = _make_service(ctx)
    service.rate_policy_repository.resolve_applicable_policies.return_value = []

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.reason != "mailbox_limits_missing"


def test_a_held_mailbox_hands_its_messages_back_for_fifteen_minutes() -> None:
    ctx = _ctx(mailbox_pending_safety_count=1)
    service = _make_service(ctx)

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert (outcome.outcome, outcome.reason) == ("DEFERRED", "safety_hold_active")
    _, wait = _handed_back(service)
    assert wait == pytest.approx(SAFETY_HOLD_RETRY.total_seconds(), abs=5)


def test_rate_control_generation_mismatch_inside_transaction_aborts() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _make_service(ctx)
    service.repository.get_rate_control_status.return_value = {
        "generation": 2,
        "status": "READY",
    }

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "DEFERRED"
    fake_provider.send_message.assert_not_called()
    service.rate_limiter.release.assert_called_once()


# ---------------------------------------------------------------------------
# Gate rejections that reach SendingService.execute end to end
# ---------------------------------------------------------------------------


def test_suppressed_message_is_skipped_never_calls_provider() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _make_service(ctx)
    service.session.execute.return_value.first.return_value = (1,)  # suppressed

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "SKIPPED"
    fake_provider.send_message.assert_not_called()
    service.repository.skip_message.assert_called_once()


def test_paused_campaign_defers_leaves_message_untouched() -> None:
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx(campaign_status="PAUSED")
    service = _make_service(ctx)

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "DEFERRED"
    fake_provider.send_message.assert_not_called()
    service.repository.skip_message.assert_not_called()
    service.repository.finalize_attempt_result.assert_not_called()


def test_message_from_before_a_resume_is_handed_back_not_sent() -> None:
    """ADR-0020: claimed before the pause, so its time/content were never
    re-validated by the reconcile pass. Released with a dispatch-generation bump
    (not skipped, not sent) so the reconcile pass can re-stamp it."""
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx(message_schedule_generation=1, campaign_schedule_generation=2)
    service = _make_service(ctx)

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "DEFERRED"
    assert outcome.reason == "stale_schedule_generation"
    fake_provider.send_message.assert_not_called()
    service.repository.skip_message.assert_not_called()
    service.repository.defer_for_capacity.assert_called_once()


# ---------------------------------------------------------------------------
# Provider outcomes, once authorization succeeds
# ---------------------------------------------------------------------------


def _authorized_service(ctx: LoadedSendContext) -> SendingService:
    service = _make_service(ctx)
    service.repository.get_mailbox_connection.return_value = {
        "credential_ciphertext": b"ciphertext",
        "encryption_key_id": "v1",
        "nonce": b"nonce",
        "protected_config": None,
        "expires_at": None,
    }
    return service


def test_provider_accepted_marks_sent() -> None:
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    fake_provider.send_message.return_value = ProviderSendResult(
        status="ACCEPTED", provider_message_id="pmid-1"
    )
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _authorized_service(ctx)

    with patch(
        "app.modules.sending.service.decrypt_credentials",
        return_value={"access_token": "x"},
    ):
        outcome = service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

    assert outcome.outcome == "SENT"
    fake_provider.send_message.assert_called_once()
    kwargs = service.repository.finalize_attempt_result.call_args.kwargs
    assert kwargs["message_status"] == "SENT"
    assert kwargs["evidence_state"] == "ACCEPTED"
    assert kwargs["provider_request_id"] == ctx.rfc_message_id


def test_provider_unknown_marks_unknown_outcome_no_retry() -> None:
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    fake_provider.send_message.return_value = ProviderSendResult(
        status="UNKNOWN", error_category="NETWORK_ERROR", error_code="timeout"
    )
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _authorized_service(ctx)

    with patch(
        "app.modules.sending.service.decrypt_credentials",
        return_value={"access_token": "x"},
    ):
        outcome = service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

    assert outcome.outcome == "UNKNOWN_OUTCOME"
    kwargs = service.repository.finalize_attempt_result.call_args.kwargs
    assert kwargs["message_status"] == "UNKNOWN_OUTCOME"
    assert kwargs["evidence_state"] == "UNKNOWN"
    assert kwargs.get("retry_count") is None  # never auto-retried


def test_provider_exception_treated_as_ambiguous_not_failed() -> None:
    """An exception escaping the adapter must never be classified as a safe
    non-send -- it becomes UNKNOWN_OUTCOME, exactly like a provider timeout."""
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    fake_provider.send_message.side_effect = RuntimeError("boom")
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _authorized_service(ctx)

    with patch(
        "app.modules.sending.service.decrypt_credentials",
        return_value={"access_token": "x"},
    ):
        outcome = service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

    assert outcome.outcome == "UNKNOWN_OUTCOME"


def test_provider_permanent_rejection_marks_failed_no_retry() -> None:
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    fake_provider.send_message.return_value = ProviderSendResult(
        status="DEFINITIVELY_REJECTED",
        error_category="PERMANENT_RECIPIENT_FAILURE",
        error_code="invalid_recipient",
    )
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _authorized_service(ctx)

    with patch(
        "app.modules.sending.service.decrypt_credentials",
        return_value={"access_token": "x"},
    ):
        outcome = service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

    assert outcome.outcome == "FAILED"
    kwargs = service.repository.finalize_attempt_result.call_args.kwargs
    assert kwargs["message_status"] == "FAILED"
    assert kwargs["terminal_reason"]


def test_provider_transient_rejection_schedules_retry() -> None:
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    fake_provider.send_message.return_value = ProviderSendResult(
        status="DEFINITIVELY_REJECTED",
        error_category="TEMPORARY_PROVIDER_ERROR",
        error_code="server_5xx",
    )
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx(retry_count=0, retry_budget=3)
    service = _authorized_service(ctx)

    with patch(
        "app.modules.sending.service.decrypt_credentials",
        return_value={"access_token": "x"},
    ):
        outcome = service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

    assert outcome.outcome == "RETRY_SCHEDULED"
    kwargs = service.repository.finalize_attempt_result.call_args.kwargs
    assert kwargs["message_status"] == "RETRY_SCHEDULED"
    assert kwargs["retry_count"] == 1
    assert kwargs["next_retry_at"] is not None
    assert kwargs["due_at"] == kwargs["next_retry_at"]


def test_retry_budget_exhausted_fails_instead_of_retrying() -> None:
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    fake_provider.send_message.return_value = ProviderSendResult(
        status="DEFINITIVELY_REJECTED",
        error_category="TEMPORARY_PROVIDER_ERROR",
        error_code="server_5xx",
    )
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx(retry_count=3, retry_budget=3)
    service = _authorized_service(ctx)

    with patch(
        "app.modules.sending.service.decrypt_credentials",
        return_value={"access_token": "x"},
    ):
        outcome = service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

    assert outcome.outcome == "FAILED"


def test_credential_missing_marks_not_invoked_failed_never_calls_provider() -> None:
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _authorized_service(ctx)
    service.repository.get_mailbox_connection.return_value = None

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "FAILED"
    fake_provider.send_message.assert_not_called()
    kwargs = service.repository.finalize_attempt_result.call_args.kwargs
    assert kwargs["evidence_state"] == "NOT_INVOKED"


# ---------------------------------------------------------------------------
# Message-ID capture and open-tracking pixel (reply/bounce correlation)
# ---------------------------------------------------------------------------


def _send_capturing_envelope(
    ctx: LoadedSendContext, result: ProviderSendResult, **settings_overrides: object
):
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    fake_provider.send_message.return_value = result
    ProviderRegistry.register("GMAIL", fake_provider)
    service = _authorized_service(ctx)
    configured = Settings.current().model_copy(update=settings_overrides)
    service.settings = configured
    with (
        patch(
            "app.modules.sending.service.decrypt_credentials",
            return_value={"access_token": "x"},
        ),
        patch("app.modules.sending.service.Settings.current", return_value=configured),
    ):
        service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )
    envelope = fake_provider.send_message.call_args.args[1]
    return envelope, service.repository.finalize_attempt_result.call_args.kwargs


def test_a_message_id_is_generated_when_the_message_has_none_and_recorded() -> None:
    ctx = _ctx(rfc_message_id=None)
    envelope, kwargs = _send_capturing_envelope(
        ctx, ProviderSendResult(status="ACCEPTED", provider_message_id="pmid-1")
    )
    assert envelope.rfc_message_id and envelope.rfc_message_id.endswith("@example.com>")
    # The id that went out is the id recorded (and used as acceptance evidence).
    assert kwargs["rfc_message_id"] == envelope.rfc_message_id
    assert kwargs["provider_request_id"] == envelope.rfc_message_id


def test_the_message_id_the_provider_reports_wins() -> None:
    """Gmail/Graph may assign their own Message-ID; that is what replies quote."""
    ctx = _ctx(rfc_message_id=None)
    envelope, kwargs = _send_capturing_envelope(
        ctx,
        ProviderSendResult(
            status="ACCEPTED",
            provider_message_id="pmid-1",
            provider_thread_id="thread-9",
            rfc_message_id="<provider-assigned@mail.gmail.com>",
        ),
    )
    assert envelope.rfc_message_id != "<provider-assigned@mail.gmail.com>"
    assert kwargs["rfc_message_id"] == "<provider-assigned@mail.gmail.com>"
    assert kwargs["provider_thread_id"] == "thread-9"


def test_an_existing_message_id_is_reused_not_regenerated() -> None:
    ctx = _ctx(rfc_message_id="abc@example.com")
    envelope, _ = _send_capturing_envelope(
        ctx, ProviderSendResult(status="ACCEPTED", provider_message_id="p")
    )
    assert envelope.rfc_message_id == "abc@example.com"


def test_an_ambiguous_send_still_records_the_message_id_for_reconciliation() -> None:
    ctx = _ctx(rfc_message_id=None)
    envelope, kwargs = _send_capturing_envelope(
        ctx,
        ProviderSendResult(
            status="UNKNOWN", error_category="NETWORK_ERROR", error_code="timeout"
        ),
    )
    assert kwargs["message_status"] == "UNKNOWN_OUTCOME"
    assert kwargs["rfc_message_id"] == envelope.rfc_message_id


def test_a_rejected_send_does_not_record_a_message_id() -> None:
    ctx = _ctx(rfc_message_id=None)
    _, kwargs = _send_capturing_envelope(
        ctx,
        ProviderSendResult(
            status="DEFINITIVELY_REJECTED",
            error_category="PERMANENT_RECIPIENT_FAILURE",
            error_code="x",
        ),
    )
    assert kwargs.get("rfc_message_id") is None


TRACKING = dict(
    open_tracking_enabled=True,
    tracking_base_url="https://outly.example.com",
    tracking_signing_key="k" * 16,
)


def test_open_pixel_is_added_at_send_time_naming_this_message() -> None:
    from app.modules.tracking.tokens import parse_open_token

    ctx = _ctx()
    envelope, _ = _send_capturing_envelope(
        ctx, ProviderSendResult(status="ACCEPTED", provider_message_id="p"), **TRACKING
    )
    assert "/api/v1/t/o/" in envelope.body_html
    assert envelope.body_html.startswith("<p>Hi</p>")
    token = envelope.body_html.split("/api/v1/t/o/")[1].split(".gif")[0]
    parsed = parse_open_token(token, TRACKING["tracking_signing_key"])
    assert parsed is not None
    assert (parsed.workspace_id, parsed.message_id) == (
        ctx.workspace_id,
        ctx.message_id,
    )
    # The send time is signed in so scanner fetches can be told from human opens.
    assert parsed.sent_at is not None
    # The frozen snapshot is untouched: the pixel exists only on the envelope.
    assert ctx.content_body_html == "<p>Hi</p>"


def test_no_pixel_when_tracking_is_off_or_unconfigured() -> None:
    accepted = ProviderSendResult(status="ACCEPTED", provider_message_id="p")
    for overrides in (
        {},
        {**TRACKING, "open_tracking_enabled": False},
        {**TRACKING, "tracking_signing_key": ""},
    ):
        envelope, _ = _send_capturing_envelope(_ctx(), accepted, **overrides)
        # Every campaign email still gets its unsubscribe footer; only the pixel
        # depends on tracking being configured.
        assert "/api/v1/t/o/" not in envelope.body_html
        assert "<img" not in envelope.body_html
        assert envelope.body_html.startswith("<p>Hi</p>")


def test_the_pixel_does_not_break_the_content_digest_check() -> None:
    """Adding the pixel must not make the send gate think the body changed."""
    accepted = ProviderSendResult(status="ACCEPTED", provider_message_id="p")
    envelope, kwargs = _send_capturing_envelope(_ctx(), accepted, **TRACKING)
    assert kwargs["message_status"] == "SENT" and "<img" in envelope.body_html


# ---------------------------------------------------------------------------
# Mandatory unsubscribe: every campaign email carries a signed link
# ---------------------------------------------------------------------------

ACCEPTED = ProviderSendResult(status="ACCEPTED", provider_message_id="p")


def test_a_campaign_email_carries_footer_headers_and_a_real_text_part() -> None:
    from app.modules.unsubscribe.tokens import parse_unsubscribe_token

    ctx = _ctx()
    envelope, kwargs = _send_capturing_envelope(ctx, ACCEPTED)

    assert kwargs["message_status"] == "SENT"
    headers = dict(envelope.extra_headers)
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    url = headers["List-Unsubscribe"].strip("<>")
    assert url.startswith("https://app.test/api/v1/unsubscribe/")
    # The link names exactly this message and verifies with the configured key.
    parsed = parse_unsubscribe_token(
        url.rsplit("/", 1)[1], Settings.current().unsubscribe_signing_key
    )
    assert parsed is not None
    assert (parsed.workspace_id, parsed.message_id) == (
        ctx.workspace_id,
        ctx.message_id,
    )
    # The same link is in the visible footer and in the text alternative.
    assert f'href="{url}"' in envelope.body_html
    assert envelope.body_text and url in envelope.body_text
    assert envelope.body_text.startswith("Hi")
    # The frozen snapshot is untouched.
    assert ctx.content_body_html == "<p>Hi</p>"


def test_the_workspace_postal_address_is_in_the_footer() -> None:
    fake_provider = MagicMock()
    fake_provider.capabilities = frozenset()
    fake_provider.send_message.return_value = ACCEPTED
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _authorized_service(ctx)
    service.repository.get_workspace_defaults.return_value = {
        "compliance": {"postal_address": "1 Main St\nMumbai 400001"}
    }

    with patch(
        "app.modules.sending.service.decrypt_credentials",
        return_value={"access_token": "x"},
    ):
        service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

    envelope = fake_provider.send_message.call_args.args[1]
    assert "1 Main St<br>Mumbai 400001" in envelope.body_html
    assert "Mumbai 400001" in envelope.body_text
    service.repository.get_workspace_defaults.assert_called_once_with(
        workspace_id=ctx.workspace_id
    )


def test_without_unsubscribe_settings_nothing_is_reserved_or_sent() -> None:
    """Fail closed, and before any capacity is spent."""
    fake_provider = MagicMock()
    ProviderRegistry.register("GMAIL", fake_provider)
    ctx = _ctx()
    service = _make_service(ctx)
    service.settings = Settings.current().model_copy(
        update={"unsubscribe_base_url": "", "unsubscribe_signing_key": ""}
    )

    outcome = service.execute(
        _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
    )

    assert outcome.outcome == "DEFERRED"
    assert outcome.reason == "unsubscribe_not_configured"
    fake_provider.send_message.assert_not_called()
    service.rate_limiter.reserve.assert_not_called()
    service.repository.insert_prepared_attempt.assert_not_called()


def test_a_controlled_test_send_is_not_given_a_footer() -> None:
    ctx = _ctx(purpose="CONTROLLED_TEST")
    service = _make_service(ctx)

    html, text, headers = service._compose_outbound(ctx, None)

    assert (html, text, headers) == ("<p>Hi</p>", None, ())
