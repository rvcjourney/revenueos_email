"""Spread a campaign's first emails over time so no mailbox is asked to send
more than its daily cap.

Until this, every message of a campaign got the same due time (the activation
start), so a 5,000-lead campaign tried to send all 5,000 in the first minute and
the send-time limiter refused almost all of them. Here each message gets its own
time, planned once when the campaign is rendered:

    place of the message in its mailbox's own queue
        -> which sending window (day) and which slot inside it
        -> a time inside that window, at least the mailbox's spacing apart

The place comes from the audience member's stable `capture_ordinal`, using the
same round-robin that picks the mailbox, so a retried or concurrent render chunk
always computes the same time for the same message.

Everything here is pure: no database, no clock.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.modules.campaigns.scheduling import WindowCalendar
from app.modules.rate_limit.policy_service import MIN_CAP_WINDOW_SECONDS

_SECONDS_PER_DAY = 86_400
# Jitter keeps many campaigns from landing on the same second. It never exceeds
# this share of a slot or this many seconds, so it cannot break the spacing.
_JITTER_SLOT_SHARE = 0.2
_JITTER_MAX_SECONDS = 15.0


@dataclass(frozen=True)
class MailboxPacing:
    daily_cap: int
    min_spacing_seconds: int


def daily_equivalent(limit_value: int, window_seconds: int) -> int:
    """A cap over any window of an hour or more, as messages per day (rounded down)."""
    return max(1, limit_value * _SECONDS_PER_DAY // window_seconds)


def pacing_from_policies(
    policies: Iterable[Mapping[str, int]], default: MailboxPacing
) -> MailboxPacing:
    """One mailbox's pacing from its MAILBOX policy rows.

    Only windows of an hour or more count as a cap (a short window paces, it does
    not cap). The tightest cap wins; spacing is the largest any row asks for. A
    mailbox with no cap row gets `default` here. At send time such a mailbox is
    refused instead, so this only decides how widely its messages are spread.
    """
    caps: list[int] = []
    spacing = 0
    for row in policies:
        spacing = max(spacing, int(row.get("min_spacing_seconds") or 0))
        window = int(row["window_seconds"])
        if window >= MIN_CAP_WINDOW_SECONDS:
            caps.append(daily_equivalent(int(row["limit_value"]), window))
    if not caps:
        return MailboxPacing(
            default.daily_cap, max(spacing, default.min_spacing_seconds)
        )
    return MailboxPacing(min(caps), spacing)


def per_mailbox_daily(
    mailbox_cap: int, campaign_daily_limit: int | None, mailbox_count: int
) -> int:
    """How many first emails one mailbox should send per day for this campaign.

    The campaign's own `daily_limit` is a total across its mailboxes, so each
    carries an equal share (rounded up). The mailbox's own cap is never exceeded.
    """
    cap = max(1, mailbox_cap)
    if campaign_daily_limit is None or campaign_daily_limit <= 0:
        return cap
    share = -(-campaign_daily_limit // max(1, mailbox_count))
    return max(1, min(cap, share))


def queue_position(capture_ordinal: int, mailbox_count: int) -> int:
    """0-based place of a recipient in its own mailbox's queue.

    Mailbox assignment is `(ordinal - 1) % n` (assign_mailbox_for_recipient), so
    one mailbox's recipients are ordinals m, m+n, m+2n, ... and this is 0, 1, 2,
    ... for them. Ordinals of members that were not accepted leave gaps, which
    only means a few slots stay empty.
    """
    return max(0, capture_ordinal - 1) // max(1, mailbox_count)


def _jitter_seconds(key: str, slot_seconds: float, min_spacing_seconds: int) -> float:
    """A fixed offset in [0, cap) from the key, where `cap` leaves every pair of
    neighbouring slots at least `min_spacing_seconds` apart."""
    cap = min(
        slot_seconds * _JITTER_SLOT_SHARE,
        _JITTER_MAX_SECONDS,
        slot_seconds - min_spacing_seconds,
    )
    if cap <= 0:
        return 0.0
    fraction = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return fraction * cap


def planned_due_at(
    calendar: WindowCalendar,
    *,
    position: int,
    per_day: int,
    min_spacing_seconds: int,
    jitter_key: UUID | str,
) -> datetime:
    """The time for the message at `position` in its mailbox's queue.

    `per_day` messages go in each window, evenly spread over it and never closer
    than `min_spacing_seconds`. A window too short for that many messages at that
    spacing is filled as far as it goes; the rest pile up at its end and the
    send-time limiter spaces them.
    """
    per_day = max(1, per_day)
    start, end = calendar.window(position // per_day)
    length = (end - start).total_seconds()
    slot_in_day = position % per_day
    slot_seconds = max(float(min_spacing_seconds), length / per_day)
    offset = min(slot_in_day * slot_seconds, max(0.0, length - 1.0))
    offset += _jitter_seconds(str(jitter_key), slot_seconds, min_spacing_seconds)
    return min(start + timedelta(seconds=offset), end - timedelta(seconds=1))


class DuePlanner:
    """Picks each first email's due time for one campaign activation.

    Built once per render chunk. The same inputs give the same time for the same
    message, so a retried chunk or a second worker agrees with the first.
    """

    def __init__(
        self,
        calendar: WindowCalendar,
        *,
        mailbox_count: int,
        campaign_daily_limit: int | None,
        pacing_by_mailbox: Mapping[UUID, MailboxPacing],
        default: MailboxPacing,
    ) -> None:
        self._calendar = calendar
        self._mailbox_count = max(1, mailbox_count)
        self._daily_limit = campaign_daily_limit
        self._pacing = pacing_by_mailbox
        self._default = default

    def due_at(
        self, *, mailbox_id: UUID, capture_ordinal: int, message_id: UUID
    ) -> datetime:
        pacing = self._pacing.get(mailbox_id, self._default)
        return planned_due_at(
            self._calendar,
            position=queue_position(capture_ordinal, self._mailbox_count),
            per_day=per_mailbox_daily(
                pacing.daily_cap, self._daily_limit, self._mailbox_count
            ),
            min_spacing_seconds=pacing.min_spacing_seconds,
            jitter_key=message_id,
        )


# How far to look for a free window before piling up in the last one. The calendar
# itself stops at ~10 years; no real backlog needs more than a few weeks of windows.
_MAX_WINDOW_SCAN = 400


class MailboxOccupancy:
    """How many unsent messages one mailbox already has in each sending window.

    Built from per-hour counts of its future unsent messages, so it stays small
    however big the campaign is. `place` hands out the next free slot, so work that
    became overdue lands after what is already planned instead of on top of it.
    Pure: no database, no clock (the calendar carries "now").
    """

    def __init__(
        self, calendar: WindowCalendar, hourly: Iterable[tuple[datetime, int]] = ()
    ) -> None:
        self._calendar = calendar
        self._counts: dict[int, int] = {}
        index = 0
        for hour_start, count in sorted(hourly):
            at = hour_start + timedelta(minutes=30)
            while index < _MAX_WINDOW_SCAN and at >= calendar.window(index)[1]:
                index += 1
            self._counts[index] = self._counts.get(index, 0) + int(count)

    def place(
        self, *, per_day: int, min_spacing_seconds: int, jitter_key: UUID | str
    ) -> datetime:
        per_day = max(1, per_day)
        window = 0
        while self._counts.get(window, 0) >= per_day and window < _MAX_WINDOW_SCAN:
            window += 1
        taken = self._counts.get(window, 0)
        due = planned_due_at(
            self._calendar,
            position=window * per_day + taken,
            per_day=per_day,
            min_spacing_seconds=min_spacing_seconds,
            jitter_key=jitter_key,
        )
        self._counts[window] = taken + 1
        return due


class SpreadPlanner:
    """Places overdue work (a follow-up whose time passed while a campaign was paused,
    a message that was due when it resumed) into the next free slots of its own
    mailbox, within the mailbox's daily cap and spacing and the campaign's daily
    limit. One planner serves one batch; the database is the memory between batches
    (each placed message becomes occupancy for the next), so a replay or a second
    worker needs no shared counter. The jitter key must be stable per message so a
    replay computes the same time."""

    def __init__(
        self,
        calendar: WindowCalendar,
        *,
        mailbox_count: int,
        campaign_daily_limit: int | None,
        pacing_by_mailbox: Mapping[UUID, MailboxPacing],
        default: MailboxPacing,
        hourly_by_mailbox: Mapping[UUID, Iterable[tuple[datetime, int]]],
    ) -> None:
        self._calendar = calendar
        self._mailbox_count = max(1, mailbox_count)
        self._daily_limit = campaign_daily_limit
        self._pacing = pacing_by_mailbox
        self._default = default
        self._hourly = hourly_by_mailbox
        self._occupancy: dict[UUID, MailboxOccupancy] = {}

    def place(self, mailbox_id: UUID, jitter_key: UUID | str) -> datetime:
        occupancy = self._occupancy.get(mailbox_id)
        if occupancy is None:
            occupancy = MailboxOccupancy(
                self._calendar, self._hourly.get(mailbox_id, ())
            )
            self._occupancy[mailbox_id] = occupancy
        pacing = self._pacing.get(mailbox_id, self._default)
        return occupancy.place(
            per_day=per_mailbox_daily(
                pacing.daily_cap, self._daily_limit, self._mailbox_count
            ),
            min_spacing_seconds=pacing.min_spacing_seconds,
            jitter_key=jitter_key,
        )
