"""Overdue follow-ups are spread, not released together (ADR-0020 follow-up).

After a pause longer than a wait, every enrollment whose email was accepted before
or during the pause has a follow-up whose time (accepted_at + wait) has already
passed. Created as they were, they were all due at the same instant. With pacing the
progression sweep places each into the next free slot of its own mailbox -- within
the mailbox's daily cap and spacing, after what is already planned -- and never
earlier than accepted_at + wait. Pure tests against the same in-memory fake the
progression suite uses; the real-database test is in test_campaign_pause_resume_real_db.
"""

# ruff: noqa: E501
from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest

from app.modules.campaigns.pacing import MailboxOccupancy, MailboxPacing, SpreadPlanner
from app.modules.campaigns.progression import ProgressionService
from app.modules.campaigns.scheduling import WindowCalendar
from tests.test_campaigns_progression import (
    CAMPAIGN,
    WS,
    FakeWorld,
    _three_email_sequence,
)

NOW = datetime(2026, 3, 9, 10, 0, tzinfo=UTC)  # a Monday, inside 09:00-17:00
LONG_AGO = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a week before NOW; wait 2 days
MB1, MB2 = uuid.uuid4(), uuid.uuid4()
PACING = MailboxPacing(daily_cap=2, min_spacing_seconds=60)


class SpreadWorld(FakeWorld):
    """FakeWorld plus the repository surface the spreading uses."""

    def __init__(self, steps, *, mailboxes=(MB1,), policies=None, **ctx) -> None:
        super().__init__(steps, daily_limit=None, **ctx)
        self.mailboxes = list(mailboxes)
        self.policies = policies or {}
        self.locks = 0

    def lock_campaign_for_planning(self, **_):
        self.locks += 1

    def list_eligible_campaign_mailboxes(self, *, workspace_id, campaign_id):
        return [{"mailbox_id": m} for m in self.mailboxes]

    def list_mailbox_limit_policies(self, *, workspace_id, mailbox_ids):
        return self.policies

    def list_mailbox_unsent_hourly(self, *, workspace_id, campaign_id, from_at, **_):
        hourly: Counter = Counter()
        for message in self.messages.values():
            due = message.get("due_at")
            if message["status"] in ("PLANNED", "SCHEDULED") and due and due >= from_at:
                hourly[
                    (
                        message["mailbox_id"],
                        due.replace(minute=0, second=0, microsecond=0),
                    )
                ] += 1
        result: dict = {}
        for (mailbox, hour), count in hourly.items():
            result.setdefault(mailbox, []).append((hour, count))
        return result

    def due_times(self, mailbox=None) -> list[datetime]:
        return sorted(
            m["due_at"]
            for m in self.messages.values()
            if m.get("due_at") and (mailbox is None or m["mailbox_id"] == mailbox)
        )


def service(world: SpreadWorld, **kwargs) -> ProgressionService:
    return ProgressionService(
        world,
        is_suppressed=lambda ws, address_id: address_id in world.suppressed,
        default_pacing=kwargs.pop("default_pacing", PACING),
        **kwargs,
    )


def run(world: SpreadWorld, **kwargs):
    svc = service(
        world,
        **{
            k: kwargs.pop(k) for k in ("default_pacing", "overdue_grace") if k in kwargs
        },
    )
    return svc.advance_batch(
        workspace_id=WS,
        campaign_id=CAMPAIGN,
        limit=kwargs.pop("limit", 100),
        now=NOW,
        **kwargs,
    )


def enroll_accepted(world: SpreadWorld, n: int, *, mailbox=MB1, at=LONG_AGO) -> list:
    ids = []
    for i in range(n):
        eid = world.enroll(name=f"Lead{i}", company="Acme", mailbox=mailbox)
        world.accept(eid, at)
        ids.append(eid)
    return ids


def day(due: datetime) -> int:
    return due.day


class TestOverdueFollowUpsAreSpread:
    def test_five_overdue_follow_ups_do_not_all_become_due_together(self) -> None:
        world = SpreadWorld(_three_email_sequence())
        enroll_accepted(world, 5)

        summary = run(world)

        due = world.due_times(MB1)
        assert summary.advanced == 5 and summary.spread == 5
        assert all(d >= NOW for d in due)  # nobody is due in the past any more
        assert all(
            d >= LONG_AGO + timedelta(minutes=2880) for d in due
        )  # never earlier than the wait
        # Daily cap 2 -> today, today, tomorrow, tomorrow, the day after.
        assert [day(d) for d in due] == [9, 9, 10, 10, 11]
        assert len(set(due)) == 5

    def test_slots_in_one_window_respect_the_mailbox_spacing(self) -> None:
        world = SpreadWorld(_three_email_sequence())
        enroll_accepted(world, 2)
        run(world, default_pacing=MailboxPacing(daily_cap=10, min_spacing_seconds=600))

        first, second = world.due_times(MB1)
        assert (second - first).total_seconds() >= 600

    def test_each_mailbox_spreads_independently(self) -> None:
        world = SpreadWorld(_three_email_sequence(), mailboxes=(MB1, MB2))
        enroll_accepted(world, 3, mailbox=MB1)
        enroll_accepted(world, 3, mailbox=MB2)

        run(world)

        assert [day(d) for d in world.due_times(MB1)] == [9, 9, 10]
        assert [day(d) for d in world.due_times(MB2)] == [9, 9, 10]

    def test_the_campaign_daily_limit_is_shared_across_its_mailboxes(self) -> None:
        world = SpreadWorld(_three_email_sequence(), mailboxes=(MB1, MB2))
        world.ctx["daily_limit"] = 2  # two mailboxes -> one per mailbox per day
        enroll_accepted(world, 2, mailbox=MB1)

        run(world, default_pacing=MailboxPacing(daily_cap=50, min_spacing_seconds=60))

        assert [day(d) for d in world.due_times(MB1)] == [9, 10]

    def test_what_is_already_planned_is_not_run_over(self) -> None:
        world = SpreadWorld(_three_email_sequence())
        # Two natural messages already fill today's cap for this mailbox.
        for hour in (12, 13):
            world.messages[(uuid.uuid4(), uuid.uuid4())] = {
                "id": uuid.uuid4(),
                "status": "SCHEDULED",
                "mailbox_id": MB1,
                "due_at": NOW.replace(hour=hour),
            }
        enroll_accepted(world, 1)

        run(world)

        overdue_placed = [
            d
            for d in world.due_times(MB1)
            if d not in (NOW.replace(hour=12), NOW.replace(hour=13))
        ]
        assert [day(d) for d in overdue_placed] == [
            10
        ]  # tomorrow, after today's full window

    def test_chunks_together_equal_one_big_run(self) -> None:
        def build() -> SpreadWorld:
            world = SpreadWorld(_three_email_sequence())
            enroll_accepted(world, 6)
            return world

        whole = build()
        run(whole)

        chunked = build()
        after = None
        for _ in range(10):
            summary = run(chunked, limit=2, after_id=after)
            if not summary.fetched:
                break
            after = summary.last_enrollment_id
        assert [day(d) for d in chunked.due_times(MB1)] == [
            day(d) for d in whole.due_times(MB1)
        ]

    def test_the_campaign_is_locked_once_and_only_when_something_is_overdue(
        self,
    ) -> None:
        world = SpreadWorld(_three_email_sequence())
        enroll_accepted(world, 3)
        run(world)
        assert world.locks == 1

        quiet = SpreadWorld(_three_email_sequence())
        enroll_accepted(
            quiet, 2, at=NOW - timedelta(hours=1)
        )  # wait 2 days -> not overdue
        run(quiet)
        assert quiet.locks == 0


class TestWhatIsLeftAlone:
    def test_a_follow_up_that_is_not_yet_due_keeps_its_exact_time(self) -> None:
        world = SpreadWorld(_three_email_sequence())
        [eid] = enroll_accepted(world, 1, at=NOW - timedelta(hours=1))
        run(world)
        [due] = world.due_times(MB1)
        assert due == NOW - timedelta(hours=1) + timedelta(minutes=2880)

    def test_a_follow_up_that_is_only_a_little_late_keeps_its_time(self) -> None:
        """Normal sweep lag must not turn into rescheduling."""
        world = SpreadWorld(_three_email_sequence(wait1=60))
        enroll_accepted(world, 1, at=NOW - timedelta(minutes=61))  # due 1 minute ago
        run(world, overdue_grace=timedelta(minutes=2))
        [due] = world.due_times(MB1)
        assert due == NOW - timedelta(minutes=1)

    def test_without_pacing_behaviour_is_exactly_as_before(self) -> None:
        world = SpreadWorld(_three_email_sequence())
        enroll_accepted(world, 3)
        svc = ProgressionService(
            world, is_suppressed=lambda ws, a: False
        )  # default_pacing=None
        svc.advance_batch(workspace_id=WS, campaign_id=CAMPAIGN, limit=100, now=NOW)
        assert {d for d in world.due_times(MB1)} == {LONG_AGO + timedelta(minutes=2880)}
        assert world.locks == 0

    def test_the_anchor_is_never_rewritten(self) -> None:
        world = SpreadWorld(_three_email_sequence())
        enroll_accepted(world, 1)
        run(world)
        [message] = [m for m in world.messages.values() if m.get("due_at")]
        assert (
            message["anchor_at"] == LONG_AGO
        )  # history: when the previous email was accepted


class TestPlannerPrimitives:
    calendar = WindowCalendar(
        NOW,
        timezone="UTC",
        weekday_set=0b1111111,
        window_start_local=time(9, 0),
        window_end_local=time(17, 0),
    )

    def test_the_same_key_always_gets_the_same_time(self) -> None:
        a = MailboxOccupancy(self.calendar).place(
            per_day=5, min_spacing_seconds=60, jitter_key="e1:s2"
        )
        b = MailboxOccupancy(self.calendar).place(
            per_day=5, min_spacing_seconds=60, jitter_key="e1:s2"
        )
        assert a == b

    def test_a_full_window_pushes_the_next_message_to_the_next_one(self) -> None:
        occupancy = MailboxOccupancy(self.calendar, [(NOW.replace(hour=11), 3)])
        placed = occupancy.place(per_day=3, min_spacing_seconds=60, jitter_key="k")
        assert placed.day == 10  # today (window 0) already holds its 3

    def test_times_before_the_window_opens_count_towards_the_next_window(self) -> None:
        early = [(NOW.replace(hour=20), 2)]  # evening of the 9th: between windows
        placed = MailboxOccupancy(self.calendar, early).place(
            per_day=2, min_spacing_seconds=60, jitter_key="k"
        )
        assert (
            placed.day == 9
        )  # window 0 (today) is still empty; the evening belongs to tomorrow

    def test_planner_uses_a_mailbox_default_when_it_has_no_policy(self) -> None:
        planner = SpreadPlanner(
            self.calendar,
            mailbox_count=1,
            campaign_daily_limit=None,
            pacing_by_mailbox={},
            default=MailboxPacing(1, 60),
            hourly_by_mailbox={},
        )
        first = planner.place(MB1, "a")
        second = planner.place(MB1, "b")
        assert first.day == 9 and second.day == 10  # default cap of 1 per day

    @pytest.mark.parametrize("count", [1, 7])
    def test_placing_never_goes_backwards(self, count: int) -> None:
        planner = SpreadPlanner(
            self.calendar,
            mailbox_count=1,
            campaign_daily_limit=None,
            pacing_by_mailbox={},
            default=PACING,
            hourly_by_mailbox={},
        )
        times: list[Any] = [planner.place(MB1, f"k{i}") for i in range(count)]
        assert times == sorted(times)
        assert all(t >= NOW for t in times)
