"""When should a personalized follow-up go out, given the waits as they stand NOW?

One rule, used by both the writer that finishes a follow-up just before it is sent
(`personalization/jit.py`) and the post-resume re-time pass (`retime.py`), so they can
never disagree (ADR-0020 amendment 2).

A wait can be edited while a campaign is paused, after a follow-up was planned with the
old one. `planned_due` is what it was planned with; it may be LATER than its wait alone
implies, because an overdue follow-up is spread over the sending windows. So:

* if a wait before the step was edited after the message was planned, the new wait
  decides, earlier or later;
* otherwise the planned time stands, but never earlier than the wait implies;
* a first email has no anchor and nothing to wait for: its planned time is left alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, time
from typing import Any

from app.core.errors import AppError
from app.modules.campaigns.progression import compute_due_at
from app.modules.campaigns.reconcile import wait_before_step


def wait_edited_after(
    steps: Sequence[Mapping[str, Any]],
    before_position: int,
    planned_at: datetime | None,
) -> bool:
    """True when a WAIT step before `before_position` was changed after `planned_at`."""
    if planned_at is None:
        return False
    return any(
        step["kind"] == "WAIT"
        and step["position"] < before_position
        and step.get("updated_at") is not None
        and step["updated_at"] > planned_at
        for step in steps
    )


def intended_follow_up_due(
    *,
    steps: Sequence[Mapping[str, Any]],
    step_id: str,
    anchor_at: datetime | None,
    planned_due: datetime | None,
    wait_edited: bool,
    timezone: str,
    weekday_set: int,
    window_start_local: time,
    window_end_local: time,
) -> datetime | None:
    if anchor_at is None:
        return planned_due
    wait = wait_before_step(steps, step_id)
    if wait is None:
        return planned_due
    try:
        base = compute_due_at(
            anchor_at,
            wait,
            timezone=timezone,
            weekday_set=weekday_set,
            window_start_local=window_start_local,
            window_end_local=window_end_local,
        )
    except AppError:
        # No sending window: keep the time it was planned with.
        return planned_due
    if planned_due is None or wait_edited:
        return base
    return max(planned_due, base)
