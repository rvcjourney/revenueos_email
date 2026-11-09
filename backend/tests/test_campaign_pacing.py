# ruff: noqa: E501
"""Spreading a campaign's first emails over its sending windows (campaigns/pacing.py)."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.campaigns.mailbox_assignment import assign_mailbox_for_recipient
from app.modules.campaigns.pacing import (
    DuePlanner,
    MailboxPacing,
    daily_equivalent,
    pacing_from_policies,
    per_mailbox_daily,
    planned_due_at,
    queue_position,
)
from app.modules.campaigns.scheduling import WindowCalendar, project_into_window

MON_TO_FRI = 0b0011111
EVERY_DAY = 0b1111111
NINE_TO_FIVE = {"window_start_local": time(9, 0), "window_end_local": time(17, 0)}
DEFAULT = MailboxPacing(daily_cap=50, min_spacing_seconds=60)


def calendar(start: datetime, *, tz: str = "UTC", weekdays: int = MON_TO_FRI) -> WindowCalendar:
    return WindowCalendar(start, timezone=tz, weekday_set=weekdays, **NINE_TO_FIVE)


# 2026-03-02 is a Monday.
MONDAY_MORNING = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
MONDAY_MID_WINDOW = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)


class TestWindowCalendar:
    def test_windows_follow_the_allowed_weekdays(self):
        cal = calendar(MONDAY_MORNING)

        starts = [cal.window(i)[0] for i in range(7)]

        assert [s.strftime("%a") for s in starts] == [
            "Mon", "Tue", "Wed", "Thu", "Fri", "Mon", "Tue",
        ]
        assert all(s.hour == 9 for s in starts)

    def test_starting_mid_window_clips_the_first_window(self):
        cal = calendar(MONDAY_MID_WINDOW)

        start, end = cal.window(0)

        assert start == MONDAY_MID_WINDOW
        assert end == datetime(2026, 3, 2, 17, 0, tzinfo=UTC)
        assert cal.window(1)[0] == datetime(2026, 3, 3, 9, 0, tzinfo=UTC)

    def test_starting_after_todays_window_skips_to_the_next_day(self):
        late = datetime(2026, 3, 2, 18, 0, tzinfo=UTC)

        assert calendar(late).window(0)[0] == datetime(2026, 3, 3, 9, 0, tzinfo=UTC)

    def test_a_weekend_start_waits_for_monday(self):
        saturday = datetime(2026, 3, 7, 10, 0, tzinfo=UTC)

        assert calendar(saturday).window(0)[0] == datetime(2026, 3, 9, 9, 0, tzinfo=UTC)

    def test_daylight_saving_moves_the_utc_time_not_the_local_time(self):
        """New York springs forward on 2026-03-08: 9:00 local is 14:00Z before, 13:00Z after."""
        cal = calendar(datetime(2026, 3, 5, 0, 0, tzinfo=UTC), tz="America/New_York",
                       weekdays=EVERY_DAY)
        ny = ZoneInfo("America/New_York")

        windows = [cal.window(i) for i in range(6)]

        assert [w[0].astimezone(ny).hour for w in windows] == [9] * 6
        assert windows[0][0] == datetime(2026, 3, 5, 14, 0, tzinfo=UTC)  # EST
        assert windows[4][0] == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)  # EDT
        for _, end in windows:
            assert end.astimezone(ny).hour == 17

    def test_it_agrees_with_project_into_window(self):
        """A time the calendar hands out is one project_into_window leaves alone."""
        cal = calendar(MONDAY_MID_WINDOW, tz="Europe/London")
        for i in range(10):
            start, end = cal.window(i)
            mid = start + (end - start) / 2
            assert project_into_window(
                mid, timezone="Europe/London", weekday_set=MON_TO_FRI, **NINE_TO_FIVE
            ) == mid

    def test_asking_past_the_horizon_returns_the_last_window_instead_of_failing(self):
        cal = calendar(MONDAY_MORNING, weekdays=0b0000001)  # Mondays only

        assert cal.window(10**6) == cal.window(10**6 + 5)

    def test_no_allowed_weekday_is_an_error(self):
        with pytest.raises(Exception, match="No eligible sending window"):
            calendar(MONDAY_MORNING, weekdays=0).window(0)

    def test_repeated_lookups_are_stable(self):
        cal = calendar(MONDAY_MORNING)

        assert cal.window(30) == cal.window(30)
        assert cal.window(3) == calendar(MONDAY_MORNING).window(3)


class TestPolicies:
    def test_a_daily_row_is_the_cap(self):
        rows = [{"window_seconds": 86_400, "limit_value": 50, "min_spacing_seconds": 60}]

        assert pacing_from_policies(rows, DEFAULT) == MailboxPacing(50, 60)

    def test_an_hourly_cap_is_converted_to_a_daily_one(self):
        assert daily_equivalent(10, 3_600) == 240
        assert daily_equivalent(100, 7 * 86_400) == 14
        assert daily_equivalent(1, 7 * 86_400) == 1  # never zero

    def test_the_tightest_cap_and_the_widest_spacing_win(self):
        rows = [
            {"window_seconds": 86_400, "limit_value": 80, "min_spacing_seconds": 30},
            {"window_seconds": 3_600, "limit_value": 5, "min_spacing_seconds": 120},
        ]

        # 80/day vs 5/hour (= 120/day): 80 is tighter; spacing takes the larger 120 s.
        assert pacing_from_policies(rows, DEFAULT) == MailboxPacing(80, 120)

    def test_a_short_window_paces_but_is_not_a_cap(self):
        rows = [{"window_seconds": 10, "limit_value": 1, "min_spacing_seconds": 0}]

        # No real cap: spread by the default cap instead, never by "1 per 10 s".
        assert pacing_from_policies(rows, DEFAULT).daily_cap == 50

    def test_a_mailbox_with_no_rows_uses_the_default(self):
        assert pacing_from_policies([], DEFAULT) == DEFAULT


class TestPerMailboxDaily:
    def test_without_a_campaign_limit_the_mailbox_cap_applies(self):
        assert per_mailbox_daily(50, None, 3) == 50
        assert per_mailbox_daily(50, 0, 3) == 50

    def test_the_campaign_limit_is_shared_equally_and_rounded_up(self):
        assert per_mailbox_daily(50, 100, 4) == 25
        assert per_mailbox_daily(50, 10, 3) == 4

    def test_the_mailbox_cap_is_never_exceeded(self):
        assert per_mailbox_daily(50, 10_000, 2) == 50

    def test_always_at_least_one(self):
        assert per_mailbox_daily(0, 1, 5) == 1


class TestQueuePosition:
    @pytest.mark.parametrize("n", [1, 2, 3, 7])
    def test_it_numbers_each_mailboxes_recipients_from_zero(self, n):
        """Matches assign_mailbox_for_recipient: each mailbox sees 0, 1, 2, ..."""
        mailboxes = [uuid.uuid4() for _ in range(n)]
        seen: dict[uuid.UUID, list[int]] = defaultdict(list)

        for ordinal in range(1, 61):
            seen[assign_mailbox_for_recipient(mailboxes, ordinal)].append(
                queue_position(ordinal, n)
            )

        for positions in seen.values():
            assert positions == list(range(len(positions)))


def times_for(n_mailboxes: int, leads: int, *, cap: int = 50, spacing: int = 60,
              start: datetime = MONDAY_MORNING, daily_limit: int | None = None):
    """Plan `leads` first emails the way the render worker does; times per mailbox."""
    mailboxes = [uuid.uuid4() for _ in range(n_mailboxes)]
    planner = DuePlanner(
        calendar(start),
        mailbox_count=n_mailboxes,
        campaign_daily_limit=daily_limit,
        pacing_by_mailbox={m: MailboxPacing(cap, spacing) for m in mailboxes},
        default=DEFAULT,
    )
    planned: dict[uuid.UUID, list[datetime]] = defaultdict(list)
    for ordinal in range(1, leads + 1):
        mailbox = assign_mailbox_for_recipient(mailboxes, ordinal)
        planned[mailbox].append(
            planner.due_at(
                mailbox_id=mailbox, capture_ordinal=ordinal, message_id=uuid.uuid4()
            )
        )
    return planned


def max_in_rolling(times: list[datetime], span: timedelta) -> int:
    ordered = sorted(times)
    best, low = 0, 0
    for high, moment in enumerate(ordered):
        while moment - ordered[low] > span:
            low += 1
        best = max(best, high - low + 1)
    return best


class TestPlanning:
    @pytest.mark.parametrize("n_mailboxes", [1, 3, 7])
    def test_five_thousand_leads_never_exceed_the_daily_cap_per_mailbox(self, n_mailboxes):
        planned = times_for(n_mailboxes, 5_000, cap=50)

        for times in planned.values():
            per_day = Counter(t.date() for t in times)
            assert max(per_day.values()) <= 50
            # Even over a rolling 24 hours the plan stays at the cap (jitter can
            # make it one over at a boundary, which the send-time limiter absorbs).
            assert max_in_rolling(times, timedelta(hours=24)) <= 51

    def test_the_first_message_is_not_sent_before_the_campaign_starts(self):
        planned = times_for(2, 200, start=MONDAY_MID_WINDOW)

        assert min(t for ts in planned.values() for t in ts) >= MONDAY_MID_WINDOW

    def test_every_time_is_inside_a_sending_window(self):
        planned = times_for(3, 1_500)

        for t in (t for ts in planned.values() for t in ts):
            assert t.weekday() < 5
            assert time(9, 0) <= t.time() <= time(17, 0)

    def test_one_mailbox_sends_at_least_the_spacing_apart(self):
        for times in times_for(2, 600, cap=50, spacing=60).values():
            ordered = sorted(times)
            gaps = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:], strict=False)]
            # Across midnight the gap is hours; inside a day it is slot-wide.
            assert min(gaps) >= 60

    def test_a_small_campaign_starts_immediately_and_stays_small(self):
        planned = times_for(1, 5, start=MONDAY_MID_WINDOW)

        times = sorted(next(iter(planned.values())))
        assert times[0] < MONDAY_MID_WINDOW + timedelta(minutes=1)
        assert times[-1] < MONDAY_MID_WINDOW + timedelta(hours=4)

    def test_the_campaign_daily_limit_is_shared_between_mailboxes(self):
        planned = times_for(4, 800, cap=50, daily_limit=40)  # 10 per mailbox per day

        for times in planned.values():
            assert max(Counter(t.date() for t in times).values()) <= 10

    def test_a_mailbox_with_a_lower_cap_gets_fewer_per_day(self):
        low, high = uuid.uuid4(), uuid.uuid4()
        planner = DuePlanner(
            calendar(MONDAY_MORNING),
            mailbox_count=2,
            campaign_daily_limit=None,
            pacing_by_mailbox={low: MailboxPacing(10, 60), high: MailboxPacing(40, 60)},
            default=DEFAULT,
        )
        per_day = {low: Counter(), high: Counter()}
        for ordinal in range(1, 401):
            mailbox = low if ordinal % 2 else high
            when = planner.due_at(
                mailbox_id=mailbox, capture_ordinal=ordinal, message_id=uuid.uuid4()
            )
            per_day[mailbox][when.date()] += 1

        assert max(per_day[low].values()) <= 10
        assert max(per_day[high].values()) <= 40

    def test_it_is_deterministic(self):
        """A retried render chunk (or a second worker) must plan the same times."""
        mailbox, message = uuid.uuid4(), uuid.uuid4()

        def plan():
            return DuePlanner(
                calendar(MONDAY_MORNING),
                mailbox_count=3,
                campaign_daily_limit=None,
                pacing_by_mailbox={mailbox: MailboxPacing(50, 60)},
                default=DEFAULT,
            ).due_at(mailbox_id=mailbox, capture_ordinal=77, message_id=message)

        assert plan() == plan()

    def test_a_window_too_short_for_the_cap_still_stays_inside_it(self):
        """500 per day at 60 s spacing needs 8.3 h; the window is 8 h. The overflow
        piles up at the end of the window rather than spilling outside it."""
        cal = calendar(MONDAY_MORNING)
        start, end = cal.window(0)

        times = [
            planned_due_at(cal, position=p, per_day=500, min_spacing_seconds=60,
                           jitter_key=str(p))
            for p in range(500)
        ]

        assert all(start <= t < end for t in times)

    def test_spacing_is_kept_even_with_jitter(self):
        cal = calendar(MONDAY_MORNING)
        times = [
            planned_due_at(cal, position=p, per_day=100, min_spacing_seconds=240,
                           jitter_key=uuid.uuid4())
            for p in range(100)
        ]

        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
        assert min(gaps) >= 240

    def test_a_campaign_too_long_to_plan_does_not_crash(self):
        cal = calendar(MONDAY_MORNING, weekdays=0b0000001)

        when = planned_due_at(
            cal, position=10**9, per_day=1, min_spacing_seconds=60, jitter_key="x"
        )

        assert when > MONDAY_MORNING
