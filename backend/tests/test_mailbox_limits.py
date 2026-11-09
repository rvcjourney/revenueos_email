# ruff: noqa: E501
"""Mailbox sending limits: the pieces that need no database."""

from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.mailboxes.repository import MailboxRepository
from app.modules.rate_limit.policy_service import (
    DAILY_WINDOW_SECONDS,
    MAILBOX_LIMIT_UNIT,
    MAX_DAILY_CAP,
    MAX_SPACING_SECONDS,
    MIN_CAP_WINDOW_SECONDS,
    MailboxLimitsIn,
    has_mailbox_volume_cap,
)
from app.modules.rate_limit.schemas import RatePolicySpec, RateScopeKind
from app.modules.sending.service import CAPACITY_UNIT


def policy(kind: RateScopeKind, window: int, *, unit: str = "MESSAGE") -> RatePolicySpec:
    return RatePolicySpec(
        kind=kind,
        source_id=str(uuid.uuid4()),
        scope_key=f"{kind}-{window}",
        unit=unit,
        window_seconds=window,
        limit_value=10,
    )


def test_the_send_path_and_the_limits_api_use_the_same_unit() -> None:
    """A limit saved in one unit would never be debited by sends in another."""
    assert CAPACITY_UNIT == MAILBOX_LIMIT_UNIT


class TestHasMailboxVolumeCap:
    def test_a_daily_mailbox_policy_is_a_cap(self) -> None:
        assert has_mailbox_volume_cap([policy(RateScopeKind.MAILBOX, DAILY_WINDOW_SECONDS)])

    def test_an_hourly_mailbox_policy_is_a_cap(self) -> None:
        assert has_mailbox_volume_cap([policy(RateScopeKind.MAILBOX, MIN_CAP_WINDOW_SECONDS)])

    def test_a_short_window_only_paces(self) -> None:
        assert not has_mailbox_volume_cap([policy(RateScopeKind.MAILBOX, 60)])

    def test_other_scopes_are_not_a_mailbox_cap(self) -> None:
        others = [
            policy(RateScopeKind.WORKSPACE, DAILY_WINDOW_SECONDS),
            policy(RateScopeKind.CAMPAIGN, DAILY_WINDOW_SECONDS),
            policy(RateScopeKind.PROVIDER, DAILY_WINDOW_SECONDS),
        ]
        assert not has_mailbox_volume_cap(others)

    def test_nothing_is_not_a_cap(self) -> None:
        assert not has_mailbox_volume_cap([])

    def test_one_real_cap_among_others_is_enough(self) -> None:
        assert has_mailbox_volume_cap(
            [
                policy(RateScopeKind.MAILBOX, 10),
                policy(RateScopeKind.WORKSPACE, DAILY_WINDOW_SECONDS),
                policy(RateScopeKind.MAILBOX, DAILY_WINDOW_SECONDS),
            ]
        )


class TestLimitsInput:
    @pytest.mark.parametrize(
        ("cap", "spacing"),
        [(1, 0), (50, 60), (MAX_DAILY_CAP, MAX_SPACING_SECONDS)],
    )
    def test_accepts_the_allowed_range(self, cap: int, spacing: int) -> None:
        parsed = MailboxLimitsIn(daily_cap=cap, min_spacing_seconds=spacing)
        assert (parsed.daily_cap, parsed.min_spacing_seconds) == (cap, spacing)

    @pytest.mark.parametrize(
        ("cap", "spacing"),
        [(0, 60), (-1, 60), (MAX_DAILY_CAP + 1, 60), (50, -1), (50, MAX_SPACING_SECONDS + 1)],
    )
    def test_rejects_anything_outside_it(self, cap: int, spacing: int) -> None:
        with pytest.raises(ValidationError):
            MailboxLimitsIn(daily_cap=cap, min_spacing_seconds=spacing)

    def test_rejects_non_numbers(self) -> None:
        with pytest.raises(ValidationError):
            MailboxLimitsIn(daily_cap="lots", min_spacing_seconds=60)  # type: ignore[arg-type]


class TestSettingsDefaults:
    def test_a_new_mailbox_starts_at_fifty_a_day_a_minute_apart(self) -> None:
        settings = Settings.current()
        assert settings.mailbox_default_daily_cap == 50
        assert settings.mailbox_default_min_spacing_seconds == 60

    def test_the_default_is_inside_what_a_person_may_set(self) -> None:
        settings = Settings.current()
        assert 1 <= settings.mailbox_default_daily_cap <= MAX_DAILY_CAP
        assert 0 <= settings.mailbox_default_min_spacing_seconds <= MAX_SPACING_SECONDS


class TestEveryConnectPathGivesTheMailboxALimit:
    """Each way of connecting a mailbox (Gmail, Microsoft, SMTP) must create its
    default limit right after inserting it, in the same transaction. A mailbox
    without one cannot send. Checked structurally: the connect flows need live
    OAuth/SMTP, and the provisioning call itself is tested on a real database."""

    @staticmethod
    def _calls_in_order(function: ast.AST) -> list[str]:
        calls = [
            (node.lineno, node.func.attr)
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        return [name for _, name in sorted(calls)]

    def test_every_function_that_inserts_a_mailbox_then_gives_it_a_limit(self) -> None:
        from app.modules.mailboxes import service

        tree = ast.parse(pathlib.Path(service.__file__).read_text(encoding="utf-8"))
        inserting = [
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef)
            and "insert_mailbox" in self._calls_in_order(fn)
        ]

        assert len(inserting) == 3  # Gmail, Microsoft, SMTP
        for fn in inserting:
            order = self._calls_in_order(fn)
            assert "ensure_default_sending_limit" in order, fn.name
            assert order.index("ensure_default_sending_limit") > order.index(
                "insert_mailbox"
            ), fn.name

    def test_the_repository_method_exists(self) -> None:
        assert callable(MailboxRepository.ensure_default_sending_limit)
