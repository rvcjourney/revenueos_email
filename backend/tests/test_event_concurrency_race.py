from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from tests.support.rate_policies import mailbox_daily_cap
from app.core.config import Settings
from app.modules.events.processor import InboundEventProcessor
from app.modules.mailboxes.providers.base import ProviderSendResult
from app.modules.mailboxes.providers.registry import ProviderRegistry
from app.modules.rate_limit.schemas import RateReservation
from app.modules.scheduler.schemas import SendTaskPayload
from app.modules.sending import gates
from app.modules.sending.schemas import LoadedSendContext
from app.modules.sending.service import SendingService


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
    subject, body, renderer_version = "Subject", "<p>Body</p>", 1
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
        frozen_destination="target@example.com",
        frozen_sender_address="sender@example.com",
        frozen_sender_name="Sender",
        rfc_message_id="msg-123@example.com",
        campaign_status="RUNNING",
        campaign_planning_status="READY",
        enrollment_state="ACTIVE",
        canonical_address="target@example.com",
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
        mailbox_sender_display_name="Sender Display",
        raw={},
        mailbox_pending_safety_count=0,
    )
    defaults.update(overrides)
    return LoadedSendContext(**defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    yield
    ProviderRegistry.reset()


def _make_service(ctx: LoadedSendContext) -> SendingService:
    session = MagicMock()
    session.execute.return_value.first.return_value = None
    service = SendingService(session, settings=Settings.current())
    service.repository = MagicMock()
    service.rate_policy_repository = MagicMock()
    service.rate_limiter = MagicMock()

    service.repository.load_message_for_send.return_value = ctx
    service.rate_policy_repository.resolve_applicable_policies.return_value = [
        mailbox_daily_cap()
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
    service.repository.next_attempt_ordinal.return_value = 1
    service.repository.transition_message_to_sending.return_value = True
    return service


class TestEventConcurrencyAndRaceProtection:
    def test_near_send_race_suppression_blocks_in_flight_send(self) -> None:
        """Simulate near-send race:
        A message is QUEUED and claimed by sending worker.
        Right before pre-send authorization, an inbound event commits a suppression row.
        SendingService must detect the suppression, transition message to SKIPPED,
        and NEVER call the provider.
        """
        fake_provider = MagicMock()
        ProviderRegistry.register("GMAIL", fake_provider)

        ctx = _ctx()
        service = _make_service(ctx)

        # Simulate suppression committed in DB right before check_suppression runs:
        # session.execute(...).first() returns a suppression row
        service.session.execute.return_value.first.return_value = (1,)

        outcome = service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

        # Provider must NEVER be touched
        fake_provider.send_message.assert_not_called()
        assert outcome.outcome == "SKIPPED"
        assert outcome.reason == "suppressed"
        service.repository.skip_message.assert_called_once_with(
            workspace_id=ctx.workspace_id,
            message_id=ctx.message_id,
            expected_version=ctx.message_version,
            terminal_reason="suppressed",
        )

    def test_mailbox_pending_safety_hold_defers_send_and_never_calls_provider(self) -> None:
        """When an inbound safety event arrives, pending_safety_count is incremented on the mailbox.
        The sending worker must defer sending until the event is resolved.
        """
        fake_provider = MagicMock()
        ProviderRegistry.register("GMAIL", fake_provider)

        # Mailbox has a pending safety hold
        ctx = _ctx(mailbox_pending_safety_count=1)
        service = _make_service(ctx)

        outcome = service.execute(
            _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
        )

        fake_provider.send_message.assert_not_called()
        assert outcome.outcome == "DEFERRED"
        assert outcome.reason == "safety_hold_active"
        # Message was not skipped; it was deferred for safety
        service.repository.skip_message.assert_not_called()

    def test_resolved_safety_hold_allows_send_to_proceed(self) -> None:
        """Once safety hold is resolved and pending_safety_count returns to 0,
        the message proceeds cleanly to provider dispatch.
        """
        fake_provider = MagicMock()
        fake_provider.capabilities = frozenset()
        fake_provider.send_message.return_value = ProviderSendResult(
            status="ACCEPTED", provider_message_id="pmid-safe-001"
        )
        ProviderRegistry.register("GMAIL", fake_provider)

        # Resolved: pending_safety_count is 0
        ctx = _ctx(mailbox_pending_safety_count=0)
        service = _make_service(ctx)
        service.repository.get_mailbox_connection.return_value = {
            "credential_ciphertext": b"ciphertext",
            "encryption_key_id": "v1",
            "nonce": b"nonce",
            "protected_config": None,
            "expires_at": None,
        }

        with patch(
            "app.modules.sending.service.decrypt_credentials",
            return_value={"access_token": "valid-token"},
        ):
            outcome = service.execute(
                _payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id)
            )

        assert outcome.outcome == "SENT"
        assert outcome.provider_message_id == "pmid-safe-001"
        fake_provider.send_message.assert_called_once()

    def test_event_processing_cancels_future_non_terminal_messages(self) -> None:
        """Verify that when an inbound hard bounce or unsubscribe occurs,
        non-terminal messages for that recipient address are cancelled.
        """
        session = MagicMock()
        processor = InboundEventProcessor(session)
        processor.repo = MagicMock()

        receipt_id = uuid.uuid4()
        workspace_id = uuid.uuid4()
        address_id = uuid.uuid4()

        payload = {
            "event_type": "UNSUBSCRIBE",
            "recipient_email": "optout@example.com",
        }
        processor.repo.claim_receipt_for_processing.return_value = {
            "receipt_id": receipt_id,
            "workspace_id": workspace_id,
            "provider": "SMTP",
            "event_identity": "test:unsub:1",
            "source_schema": "provider.event.v1",
            "receipt_status": "RECEIVED",
            "payload_ref": '{"event_type": "UNSUBSCRIBE", "recipient_email": "optout@example.com"}',
        }
        processor.repo.ensure_recipient_address.return_value = address_id
        processor.repo.upsert_suppression.return_value = (uuid.uuid4(), True)
        processor.repo.stop_active_enrollments.return_value = []
        processor.repo.cancel_non_terminal_messages.return_value = 5

        result = processor.process_receipt(receipt_id)

        assert result.status == "processed"
        assert result.messages_cancelled == 5
        processor.repo.cancel_non_terminal_messages.assert_called_once_with(
            workspace_id=workspace_id,
            address_id=address_id,
            terminal_reason="unsubscribed",
        )
