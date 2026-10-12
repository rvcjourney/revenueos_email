"""The shared timing rule for planned personalized follow-ups (ADR-0020 amendment 2)
and the service that applies it after a resume. Pure functions and an in-memory
repository: no database, no network."""

# ruff: noqa: E501
from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any

from app.modules.campaigns.follow_up_timing import (
    intended_follow_up_due,
    wait_edited_after,
)
from app.modules.campaigns.retime import RetimeService

ANCHOR = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a Monday, inside the window
WINDOW = dict(
    timezone="UTC",
    weekday_set=0b1111111,
    window_start_local=time(9, 0),
    window_end_local=time(17, 0),
)
E1, W, E2 = (str(uuid.uuid4()) for _ in range(3))


def steps(wait_minutes: int, *, wait_updated: datetime | None = None):
    old = ANCHOR - timedelta(days=2)
    return [
        {
            "id": E1,
            "position": 1,
            "kind": "EMAIL",
            "wait_duration_minutes": None,
            "updated_at": old,
        },
        {
            "id": W,
            "position": 2,
            "kind": "WAIT",
            "wait_duration_minutes": wait_minutes,
            "updated_at": wait_updated or old,
        },
        {
            "id": E2,
            "position": 3,
            "kind": "EMAIL",
            "wait_duration_minutes": None,
            "updated_at": old,
        },
    ]


def due(*, wait_minutes, planned_due, edited, anchor=ANCHOR, step_id=E2):
    return intended_follow_up_due(
        steps=steps(wait_minutes),
        step_id=step_id,
        anchor_at=anchor,
        planned_due=planned_due,
        wait_edited=edited,
        **WINDOW,
    )


class TestRule:
    def test_an_unedited_wait_keeps_the_planned_time(self) -> None:
        planned = ANCHOR + timedelta(hours=1)
        assert due(wait_minutes=60, planned_due=planned, edited=False) == planned

    def test_an_edited_shorter_wait_brings_it_forward(self) -> None:
        assert due(
            wait_minutes=30, planned_due=ANCHOR + timedelta(hours=5), edited=True
        ) == ANCHOR + timedelta(minutes=30)

    def test_an_edited_longer_wait_pushes_it_back(self) -> None:
        assert due(
            wait_minutes=240, planned_due=ANCHOR + timedelta(hours=1), edited=True
        ) == ANCHOR + timedelta(hours=4)

    def test_a_spread_out_time_is_kept_when_nothing_was_edited(self) -> None:
        planned = ANCHOR + timedelta(hours=5)  # later than the wait alone implies
        assert due(wait_minutes=60, planned_due=planned, edited=False) == planned

    def test_the_planned_time_is_never_earlier_than_the_wait_implies(self) -> None:
        assert due(
            wait_minutes=240, planned_due=ANCHOR + timedelta(hours=1), edited=False
        ) == ANCHOR + timedelta(hours=4)

    def test_a_first_email_is_left_alone(self) -> None:
        planned = ANCHOR + timedelta(hours=1)
        assert (
            due(wait_minutes=5000, planned_due=planned, edited=True, anchor=None)
            == planned
        )
        assert (
            due(wait_minutes=5000, planned_due=planned, edited=True, step_id=E1)
            == planned
        )

    def test_no_sending_window_keeps_the_planned_time(self) -> None:
        planned = ANCHOR + timedelta(hours=3)
        result = intended_follow_up_due(
            steps=steps(60),
            step_id=E2,
            anchor_at=ANCHOR,
            planned_due=planned,
            wait_edited=True,
            timezone="UTC",
            weekday_set=0,  # no sending day at all
            window_start_local=time(9, 0),
            window_end_local=time(17, 0),
        )
        assert result == planned


class TestWaitEditedAfter:
    def test_only_a_wait_before_the_step_counts(self) -> None:
        planned_at = ANCHOR - timedelta(days=1)
        edited = steps(60, wait_updated=ANCHOR)
        assert wait_edited_after(edited, 3, planned_at) is True
        assert (
            wait_edited_after(edited, 2, planned_at) is False
        )  # the wait is not before position 2

    def test_a_wait_edited_before_planning_does_not_count(self) -> None:
        planned_at = ANCHOR
        assert (
            wait_edited_after(
                steps(60, wait_updated=ANCHOR - timedelta(hours=1)), 3, planned_at
            )
            is False
        )

    def test_no_planning_time_means_not_edited(self) -> None:
        assert wait_edited_after(steps(60, wait_updated=ANCHOR), 3, None) is False


class FakeRepo:
    """The slice of CampaignWorkerRepository the service uses."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        campaign_type: str = "HYPER_PERSONALIZED",
        active: bool = True,
    ) -> None:
        self.rows = rows
        self.applied: list[dict[str, Any]] = []
        self.ctx = (
            {
                "campaign_type": campaign_type,
                "activated_sequence_id": uuid.uuid4(),
                **WINDOW,
            }
            if active
            else None
        )
        self.waits = steps(30)  # the wait was shortened to 30 minutes

    def get_reconcile_context(self, **_: Any):
        return self.ctx

    def list_sequence_steps(self, **_: Any):
        return self.waits

    def fetch_unretimed_planned(self, *, limit: int, after_id=None, **_: Any):
        rows = [
            r for r in self.rows if after_id is None or r["generation_id"] > after_id
        ]
        return rows[:limit]

    def apply_retime(self, **kw: Any) -> bool:
        self.applied.append(kw)
        return True


def planned_row(planned_due: datetime) -> dict[str, Any]:
    return {
        "generation_id": uuid.uuid4(),
        "message_id": uuid.uuid4(),
        "step_id": E2,
        "due_at": planned_due,
        "anchor_at": ANCHOR,
    }


class TestRetimeService:
    def service(self, repo: FakeRepo) -> RetimeService:
        return RetimeService(repo, lead_seconds=3600)

    def run(self, repo: FakeRepo, **kw: Any):
        return self.service(repo).retime_batch(
            workspace_id=uuid.uuid4(),
            campaign_id=uuid.uuid4(),
            generation=2,
            limit=10,
            **kw,
        )

    def test_a_shortened_wait_moves_the_message_and_its_job_earlier(self) -> None:
        row = planned_row(ANCHOR + timedelta(hours=5))
        repo = FakeRepo([row])
        summary = self.run(repo)
        assert (summary.fetched, summary.retimed, summary.unchanged) == (1, 1, 0)
        applied = repo.applied[0]
        assert applied["due_at"] == ANCHOR + timedelta(minutes=30)
        # Looked at again one lead time before it is due (an hour before: already past).
        assert applied["next_attempt_at"] == ANCHOR + timedelta(minutes=30) - timedelta(
            hours=1
        )
        assert applied["generation_id"] == row["generation_id"]

    def test_an_unchanged_time_still_marks_the_job_as_done(self) -> None:
        repo = FakeRepo([planned_row(ANCHOR + timedelta(minutes=30))])
        summary = self.run(repo)
        assert (summary.retimed, summary.unchanged) == (0, 1)
        assert len(repo.applied) == 1  # touched, so it stops matching

    def test_a_standard_campaign_is_never_touched(self) -> None:
        repo = FakeRepo([planned_row(ANCHOR)], campaign_type="STANDARD")
        summary = self.run(repo)
        assert summary.active is False and repo.applied == []

    def test_a_superseded_task_does_nothing(self) -> None:
        repo = FakeRepo([planned_row(ANCHOR)], active=False)
        summary = self.run(repo)
        assert summary.active is False and repo.applied == []

    def test_nothing_to_do_returns_quietly(self) -> None:
        summary = self.run(FakeRepo([]))
        assert (
            summary.active is True and summary.fetched == 0 and summary.last_id is None
        )

    def test_the_last_job_is_reported_so_a_full_batch_can_continue(self) -> None:
        rows = [planned_row(ANCHOR + timedelta(hours=5)) for _ in range(3)]
        rows.sort(key=lambda r: r["generation_id"])
        summary = RetimeService(FakeRepo(rows), lead_seconds=0).retime_batch(
            workspace_id=uuid.uuid4(), campaign_id=uuid.uuid4(), generation=2, limit=2
        )
        assert summary.fetched == 2 and summary.last_id == rows[1]["generation_id"]
