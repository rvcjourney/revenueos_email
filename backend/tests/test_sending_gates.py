from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.modules.sending import gates
from app.modules.sending.schemas import LoadedSendContext


def _ctx(**overrides: object) -> LoadedSendContext:
    now = datetime.now(UTC)
    subject = "Hello"
    body = "<p>Hi</p>"
    renderer_version = 1
    digest = hashlib.sha256(
        f"{subject}\x00{body}\x00{renderer_version}".encode()
    ).hexdigest()
    defaults: dict[str, object] = dict(
        message_id=uuid4(),
        workspace_id=uuid4(),
        purpose="CAMPAIGN",
        campaign_id=uuid4(),
        enrollment_id=uuid4(),
        mailbox_id=uuid4(),
        address_id=uuid4(),
        message_status="QUEUED",
        dispatch_generation=1,
        message_version=1,
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
        mailbox_workspace_id=None,  # filled below to match workspace_id
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
    if defaults["mailbox_workspace_id"] is None:
        defaults["mailbox_workspace_id"] = defaults["workspace_id"]
    return LoadedSendContext(**defaults)  # type: ignore[arg-type]


def _fake_session_no_suppression() -> MagicMock:
    session = MagicMock()
    session.execute.return_value.first.return_value = None
    return session


# ---------------------------------------------------------------------------
# Every gate: a rejection must be raisable independently and each must never
# call anything provider-like (these are pure functions -- the "provider was
# never called" property is proven at the service-layer/idempotency tests).
# ---------------------------------------------------------------------------


def test_stale_dispatch_generation_rejected() -> None:
    ctx = _ctx(dispatch_generation=2)
    with pytest.raises(gates.StaleDispatchGeneration):
        gates.check_dispatch_generation(ctx, 1)


def test_dispatch_generation_match_passes() -> None:
    ctx = _ctx(dispatch_generation=2)
    gates.check_dispatch_generation(ctx, 2)  # must not raise


@pytest.mark.parametrize(
    "status", ["SENT", "FAILED", "SKIPPED", "CANCELLED", "UNKNOWN_OUTCOME"]
)
def test_already_resolved_states_are_noop_not_rejection(status: str) -> None:
    ctx = _ctx(message_status=status)
    with pytest.raises(gates.MessageAlreadyResolved):
        gates.check_message_state(ctx)


@pytest.mark.parametrize("status", ["PLANNED", "SCHEDULED", "RETRY_SCHEDULED"])
def test_unexpected_message_states_rejected(status: str) -> None:
    ctx = _ctx(message_status=status)
    with pytest.raises(gates.MessageStateRejected):
        gates.check_message_state(ctx)


@pytest.mark.parametrize("status", ["QUEUED", "SENDING"])
def test_proceed_eligible_states_pass(status: str) -> None:
    ctx = _ctx(message_status=status)
    gates.check_message_state(ctx)  # must not raise


@pytest.mark.parametrize(
    "campaign_status,planning_status",
    [("PAUSED", "READY"), ("RUNNING", "PENDING"), ("STOPPED", "READY")],
)
def test_campaign_not_running_rejected(
    campaign_status: str, planning_status: str
) -> None:
    ctx = _ctx(
        campaign_status=campaign_status, campaign_planning_status=planning_status
    )
    with pytest.raises(gates.CampaignStateRejected):
        gates.check_campaign_state(ctx)


def test_stale_schedule_generation_rejected() -> None:
    """ADR-0020: a message claimed before a pause/resume carries the old
    generation; it must be handed back, not sent."""
    ctx = _ctx(message_schedule_generation=1, campaign_schedule_generation=2)
    with pytest.raises(gates.StaleScheduleGeneration) as info:
        gates.check_schedule_generation(ctx)
    assert info.value.terminal_reason == "stale_schedule_generation"


def test_matching_or_unknown_schedule_generation_passes() -> None:
    gates.check_schedule_generation(
        _ctx(message_schedule_generation=3, campaign_schedule_generation=3)
    )
    gates.check_schedule_generation(_ctx())  # not loaded (None) -> not enforced
    gates.check_schedule_generation(
        _ctx(
            purpose="CONTROLLED_TEST",
            message_schedule_generation=1,
            campaign_schedule_generation=None,
        )
    )


def test_stale_schedule_generation_is_part_of_the_pre_authorization_gates() -> None:
    ctx = _ctx(message_schedule_generation=1, campaign_schedule_generation=2)
    with pytest.raises(gates.StaleScheduleGeneration):
        gates.run_pre_authorization_gates(
            _fake_session_no_suppression(), ctx, expected_dispatch_generation=1
        )


def test_controlled_test_purpose_skips_campaign_gate() -> None:
    ctx = _ctx(
        purpose="CONTROLLED_TEST", campaign_status=None, campaign_planning_status=None
    )
    gates.check_campaign_state(ctx)  # must not raise


@pytest.mark.parametrize("state", ["COMPLETED", "STOPPED", "FAILED"])
def test_enrollment_not_active_rejected(state: str) -> None:
    ctx = _ctx(enrollment_state=state)
    with pytest.raises(gates.EnrollmentStateRejected):
        gates.check_enrollment_state(ctx)


def test_suppression_workspace_scope_rejected() -> None:
    ctx = _ctx()
    session = MagicMock()
    # First call (workspace suppression check) returns a row -> suppressed.
    session.execute.return_value.first.return_value = (1,)
    with pytest.raises(gates.SuppressionRejected):
        gates.check_suppression(session, ctx)


def test_suppression_platform_scope_rejected() -> None:
    ctx = _ctx()
    session = MagicMock()
    calls = iter([None, (1,)])  # workspace: not suppressed, platform: suppressed

    def _execute(*args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.first.return_value = next(calls)
        return result

    session.execute.side_effect = _execute
    with pytest.raises(gates.SuppressionRejected):
        gates.check_suppression(session, ctx)


def test_not_suppressed_passes() -> None:
    ctx = _ctx()
    session = _fake_session_no_suppression()
    gates.check_suppression(session, ctx)  # must not raise


@pytest.mark.parametrize(
    "field,value",
    [
        ("mailbox_connection_state", "DISCONNECTED"),
        ("mailbox_connection_state", "RECONNECT_REQUIRED"),
        ("mailbox_policy_state", "RESTRICTED"),
        ("mailbox_health_state", "DEGRADED"),
        ("mailbox_circuit_state", "OPEN"),
    ],
)
def test_mailbox_state_rejected(field: str, value: str) -> None:
    ctx = _ctx(**{field: value})
    with pytest.raises(gates.MailboxStateRejected):
        gates.check_mailbox_state(ctx)


def test_mailbox_blocked_until_future_rejected() -> None:
    ctx = _ctx(mailbox_blocked_until=datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(gates.MailboxStateRejected):
        gates.check_mailbox_state(ctx)


def test_mailbox_blocked_until_past_passes() -> None:
    ctx = _ctx(mailbox_blocked_until=datetime.now(UTC) - timedelta(hours=1))
    gates.check_mailbox_state(ctx)  # must not raise


def test_cross_tenant_mailbox_rejected() -> None:
    ctx = _ctx()
    ctx = _ctx(mailbox_workspace_id=uuid4(), workspace_id=ctx.workspace_id)
    with pytest.raises(gates.MailboxOwnershipRejected):
        gates.check_mailbox_ownership(ctx)


def test_unsupported_provider_rejected() -> None:
    ctx = _ctx(mailbox_provider="UNKNOWN_PROVIDER")
    with pytest.raises(gates.ProviderMismatchRejected):
        gates.check_provider_match(ctx)


def test_missing_snapshot_rejected() -> None:
    ctx = _ctx(
        rendered_at=None,
        content_subject=None,
        content_body_html=None,
        content_digest=None,
    )
    with pytest.raises(gates.SnapshotIntegrityRejected):
        gates.check_snapshot_integrity(ctx)


def test_corrupted_digest_rejected() -> None:
    ctx = _ctx(content_digest="0" * 64)
    with pytest.raises(gates.SnapshotIntegrityRejected):
        gates.check_snapshot_integrity(ctx)


def test_controlled_test_digest_formula_differs_from_campaign() -> None:
    subject, body = "Test subject", "<p>Test body</p>"
    digest = hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()
    ctx = _ctx(
        purpose="CONTROLLED_TEST",
        content_subject=subject,
        content_body_html=body,
        content_digest=digest,
        campaign_id=None,
        enrollment_id=None,
        campaign_status=None,
        campaign_planning_status=None,
        enrollment_state=None,
    )
    gates.check_snapshot_integrity(ctx)  # must not raise


def test_valid_snapshot_passes() -> None:
    ctx = _ctx()
    gates.check_snapshot_integrity(ctx)  # must not raise


@pytest.mark.parametrize(
    "destination", ["not-an-email", "has\r\nCRLF@example.com", "", "no-at-sign.com"]
)
def test_invalid_recipient_rejected(destination: str) -> None:
    ctx = _ctx(frozen_destination=destination)
    with pytest.raises(gates.RecipientValidationRejected):
        gates.check_recipient(ctx)


def test_valid_recipient_passes() -> None:
    ctx = _ctx()
    gates.check_recipient(ctx)  # must not raise


def test_run_pre_authorization_gates_happy_path_passes() -> None:
    ctx = _ctx()
    session = _fake_session_no_suppression()
    gates.run_pre_authorization_gates(session, ctx, expected_dispatch_generation=1)


def test_run_pre_authorization_gates_stops_at_first_failure() -> None:
    """Suppression must be checked even when other gates would pass --
    proves gate ordering doesn't silently skip a later check."""
    ctx = _ctx()
    session = MagicMock()
    session.execute.return_value.first.return_value = (1,)  # suppressed
    with pytest.raises(gates.SuppressionRejected):
        gates.run_pre_authorization_gates(session, ctx, expected_dispatch_generation=1)
