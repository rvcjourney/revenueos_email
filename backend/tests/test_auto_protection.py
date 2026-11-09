"""Automatic protection: the thresholds and the hook into reply sync (no database)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.modules.replies.service import ReplySyncService
from app.modules.safety.evaluator import (
    auto_bounce_identity,
    hold_reason,
)
from app.modules.safety.holds_service import hold_kind
from app.modules.safety.thresholds import (
    BOUNCE_CRITICAL_PERCENT,
    BOUNCE_MIN_SENDS,
    BOUNCE_WARNING_PERCENT,
    COMPLAINT_CRITICAL_PERCENT,
    COMPLAINT_MIN_SENDS,
    bounce_level,
    complaints_critical,
    rate_percent,
)


class TestThresholds:
    def test_the_numbers_are_the_documented_ones(self) -> None:
        assert (BOUNCE_WARNING_PERCENT, BOUNCE_CRITICAL_PERCENT) == (2.0, 5.0)
        assert BOUNCE_MIN_SENDS == 20
        assert (COMPLAINT_CRITICAL_PERCENT, COMPLAINT_MIN_SENDS) == (0.1, 50)

    @pytest.mark.parametrize(
        ("rate", "sent", "expected"),
        [
            (0.0, 1000, None),
            (2.0, 1000, None),  # the threshold itself is not exceeded
            (2.01, 1000, "WARNING"),
            (5.0, 1000, "WARNING"),
            (5.01, 1000, "CRITICAL"),
            (100.0, 20, "CRITICAL"),
            (100.0, 19, None),  # too few sends for the rate to mean anything
            (6.0, 0, None),
        ],
    )
    def test_bounce_level(self, rate: float, sent: int, expected: str | None) -> None:
        assert bounce_level(rate, sent) == expected

    @pytest.mark.parametrize(
        ("rate", "sent", "expected"),
        [(0.1, 1000, False), (0.11, 1000, True), (5.0, 49, False), (5.0, 50, True)],
    )
    def test_complaints(self, rate: float, sent: int, expected: bool) -> None:
        assert complaints_critical(rate, sent) is expected

    def test_rate_percent(self) -> None:
        assert rate_percent(1, 3) == 33.33
        assert rate_percent(0, 0) == 0.0
        assert rate_percent(5, 100) == 5.0


class TestIdentities:
    def test_one_hold_identity_per_mailbox_per_utc_day(self) -> None:
        mailbox = uuid.uuid4()
        morning = datetime(2026, 3, 2, 1, 0, tzinfo=UTC)
        evening = datetime(2026, 3, 2, 23, 59, tzinfo=UTC)
        tomorrow = datetime(2026, 3, 3, 0, 1, tzinfo=UTC)

        assert auto_bounce_identity(mailbox, morning) == auto_bounce_identity(
            mailbox, evening
        )
        assert auto_bounce_identity(mailbox, evening) != auto_bounce_identity(
            mailbox, tomorrow
        )
        assert len(auto_bounce_identity(mailbox, morning)) <= 100

    def test_identities_of_two_mailboxes_differ(self) -> None:
        now = datetime(2026, 3, 2, tzinfo=UTC)
        assert auto_bounce_identity(uuid.uuid4(), now) != auto_bounce_identity(
            uuid.uuid4(), now
        )

    def test_the_reason_says_what_happened_and_fits_the_column(self) -> None:
        reason = hold_reason(12, 100, 12.0)
        assert "12.0%" in reason and "12 of 100" in reason
        assert "releases it" in reason
        assert len(reason) <= 500

    def test_kinds(self) -> None:
        mailbox = uuid.uuid4()
        assert hold_kind(f"auto-bounce:{mailbox}:2026-03-02") == "HIGH_BOUNCE_RATE"
        assert hold_kind(f"resync:{mailbox}:1740000000") == "RESYNC"
        assert hold_kind("anything-else") == "OTHER"


class TestSyncHook:
    """A failure while protecting a mailbox must never break reply sync."""

    def _service(self) -> ReplySyncService:
        service = object.__new__(ReplySyncService)
        service.session = MagicMock()
        return service

    def test_it_evaluates_and_commits(self) -> None:
        service = self._service()
        ws, mb = uuid.uuid4(), uuid.uuid4()
        with patch("app.modules.replies.service.MailboxSafetyEvaluator") as evaluator:
            service._protect_mailbox(ws, mb, {})

        evaluator.assert_called_once_with(service.session)
        evaluator.return_value.evaluate.assert_called_once_with(
            workspace_id=ws, mailbox_id=mb
        )
        service.session.commit.assert_called_once()

    def test_a_failure_is_logged_and_rolled_back_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = self._service()
        with patch("app.modules.replies.service.MailboxSafetyEvaluator") as evaluator:
            evaluator.return_value.evaluate.side_effect = RuntimeError("db down")
            with caplog.at_level(logging.ERROR):
                service._protect_mailbox(uuid.uuid4(), uuid.uuid4(), {})

        service.session.rollback.assert_called_once()
        service.session.commit.assert_not_called()
        assert "mailbox_safety_evaluation_failed" in caplog.text
