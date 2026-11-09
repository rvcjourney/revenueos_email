"""Late provider results (issue I-01): a result that arrives after recovery or
reconciliation moved the message on must not overwrite it. Mocked repository
here; the guarded SQL itself is proven in test_late_provider_result_real_db.py."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from app.modules.mailboxes.providers.base import ErrorCategory, ProviderSendResult
from app.modules.mailboxes.providers.registry import ProviderRegistry
from app.modules.sending.repository import FINALIZE_LATE, FINALIZE_LATE_ACCEPTED
from tests.test_sending_service import _authorized_service, _ctx, _payload


def _run(result: ProviderSendResult, finalize_returns):
    fake = MagicMock()
    fake.capabilities = frozenset()
    fake.send_message.return_value = result
    ProviderRegistry.register("GMAIL", fake)
    ctx = _ctx()
    service = _authorized_service(ctx)
    service.repository.finalize_attempt_result.return_value = finalize_returns
    with patch("app.modules.sending.service.decrypt_credentials", return_value={"access_token": "x"}):
        outcome = service.execute(_payload(workspace_id=ctx.workspace_id, message_id=ctx.message_id))
    audits = [(c.kwargs["action"], c.kwargs["reason"]) for c in service.repository.record_audit_event.call_args_list]
    ProviderRegistry.reset()
    return outcome, audits, fake, service


ACCEPTED = ProviderSendResult(status="ACCEPTED", provider_message_id="p-1", rfc_message_id="<m@x>")
RETRYABLE = ProviderSendResult(status="DEFINITIVELY_REJECTED", error_category=ErrorCategory.TEMPORARY_PROVIDER_ERROR, error_code="http_503")


def test_a_late_acceptance_after_recovery_is_reported_sent() -> None:
    outcome, audits, _, _ = _run(ACCEPTED, FINALIZE_LATE_ACCEPTED)
    assert outcome.outcome == "SENT"
    assert ("message.sent", "accepted_after_recovery") in audits


def test_a_late_retryable_rejection_schedules_nothing_and_is_audited() -> None:
    outcome, audits, _, service = _run(RETRYABLE, FINALIZE_LATE)
    assert outcome.outcome == "NOOP" and outcome.reason == "late_provider_result"
    actions = [a for a, _ in audits]
    assert "message.late_provider_result" in actions
    assert "message.retry_scheduled" not in actions
    # It was asked for (guarded); the repository decided it was late.
    assert service.repository.finalize_attempt_result.call_count == 1


def test_a_late_acceptance_after_reconciliation_changes_nothing() -> None:
    outcome, audits, _, _ = _run(ACCEPTED, FINALIZE_LATE)
    assert outcome.outcome == "NOOP"
    assert ("message.late_provider_result", "ACCEPTED") in audits
    assert "message.sent" not in [a for a, _ in audits]


def test_the_normal_path_is_unchanged() -> None:
    outcome, audits, _, _ = _run(ACCEPTED, None)
    assert outcome.outcome == "SENT"
    assert ("message.sent", None) in audits
    outcome, audits, _, _ = _run(RETRYABLE, None)
    assert outcome.outcome == "RETRY_SCHEDULED"
    assert "message.late_provider_result" not in [a for a, _ in audits]


def test_the_provider_is_given_the_authorization_deadline() -> None:
    before = datetime.now(UTC)
    _, _, fake, _ = _run(ACCEPTED, None)
    deadline = fake.send_message.call_args.kwargs["authorization_deadline"]
    assert before + timedelta(seconds=29) <= deadline <= datetime.now(UTC) + timedelta(seconds=30)
