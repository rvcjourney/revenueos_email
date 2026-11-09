"""Follow-up progression: pure timing rules plus the service run against an
in-memory fake that mirrors the SQL semantics of CampaignWorkerRepository
(ACTIVE + current message SENT/FAILED, unique (enrollment, step) message key,
guarded pointer updates). No database, no email."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from app.modules.campaigns.progression import (
    ProgressionService,
    compute_due_at,
    plan_next_email,
)

ALL_DAYS = 0b1111111
WEEKDAYS = 0b0011111  # Mon..Fri (bit 0 = Monday)
WS = uuid.uuid4()
CAMPAIGN = uuid.uuid4()
SEQUENCE = uuid.uuid4()


def _step(
    position: int, kind: str, *, wait: int | None = None, subject=None, body=None
):
    return {
        "id": uuid.uuid4(),
        "position": position,
        "kind": kind,
        "wait_duration_minutes": wait,
        "email_subject": subject,
        "email_body_html": body,
        "email_preheader": None,
    }


def _three_email_sequence(wait1: int = 2880, wait2: int = 4320):
    return [
        _step(1, "EMAIL", subject="One {{first_name|there}}", body="<p>Body 1</p>"),
        _step(2, "WAIT", wait=wait1),
        _step(
            3,
            "EMAIL",
            subject="Two for {{company|you}}",
            body="<p>Hi {{first_name|there}}, follow-up 2</p>",
        ),
        _step(4, "WAIT", wait=wait2),
        _step(5, "EMAIL", subject="Three", body="<p>Last one, {{first_name}}</p>"),
    ]


class TestPlanNextEmail:
    def test_sums_the_waits_before_the_next_email(self) -> None:
        steps = _three_email_sequence()
        plan = plan_next_email(steps, 1)
        assert plan is not None
        assert plan.step["position"] == 3
        assert plan.wait_minutes == 2880
        plan = plan_next_email(steps, 3)
        assert plan is not None and plan.step["position"] == 5
        assert plan.wait_minutes == 4320

    def test_no_email_after_the_last_step(self) -> None:
        assert plan_next_email(_three_email_sequence(), 5) is None

    def test_trailing_wait_without_an_email_finishes_the_sequence(self) -> None:
        steps = _three_email_sequence()[:4]  # ends with a WAIT
        assert plan_next_email(steps, 3) is None

    def test_consecutive_waits_add_up(self) -> None:
        steps = [
            _step(1, "EMAIL", subject="a", body="b"),
            _step(2, "WAIT", wait=60),
            _step(3, "WAIT", wait=120),
            _step(4, "EMAIL", subject="c", body="d"),
        ]
        plan = plan_next_email(steps, 1)
        assert plan is not None and plan.wait_minutes == 180

    def test_input_order_does_not_matter(self) -> None:
        steps = list(reversed(_three_email_sequence()))
        plan = plan_next_email(steps, 1)
        assert plan is not None and plan.step["position"] == 3

    def test_single_email_sequence_has_no_follow_up(self) -> None:
        assert plan_next_email([_step(1, "EMAIL", subject="a", body="b")], 1) is None


class TestComputeDueAt:
    def _due(self, accepted: datetime, minutes: int, **overrides: Any) -> datetime:
        params: dict[str, Any] = {
            "timezone": "UTC",
            "weekday_set": ALL_DAYS,
            "window_start_local": time(9, 0),
            "window_end_local": time(17, 0),
        }
        params.update(overrides)
        return compute_due_at(accepted, minutes, **params)

    def test_days_wait_lands_at_the_same_time_two_days_later(self) -> None:
        accepted = datetime(2026, 3, 2, 10, 15, tzinfo=UTC)  # Monday
        assert self._due(accepted, 2880) == datetime(2026, 3, 4, 10, 15, tzinfo=UTC)

    def test_hours_wait(self) -> None:
        accepted = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
        assert self._due(accepted, 180) == datetime(2026, 3, 2, 13, 0, tzinfo=UTC)

    def test_minutes_wait(self) -> None:
        accepted = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
        assert self._due(accepted, 45) == datetime(2026, 3, 2, 10, 45, tzinfo=UTC)

    def test_after_hours_moves_to_the_next_window_start(self) -> None:
        accepted = datetime(2026, 3, 2, 16, 30, tzinfo=UTC)
        assert self._due(accepted, 120) == datetime(2026, 3, 3, 9, 0, tzinfo=UTC)

    def test_before_hours_moves_to_window_start(self) -> None:
        accepted = datetime(2026, 3, 2, 2, 0, tzinfo=UTC)
        assert self._due(accepted, 60) == datetime(2026, 3, 2, 9, 0, tzinfo=UTC)

    def test_weekend_is_skipped(self) -> None:
        friday_late = datetime(2026, 3, 6, 16, 30, tzinfo=UTC)
        due = self._due(friday_late, 120, weekday_set=WEEKDAYS)
        assert due == datetime(2026, 3, 9, 9, 0, tzinfo=UTC)  # Monday

    def test_two_day_wait_from_thursday_skips_to_monday(self) -> None:
        thursday = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
        due = self._due(thursday, 2880, weekday_set=WEEKDAYS)
        assert due == datetime(2026, 3, 9, 9, 0, tzinfo=UTC)

    def test_uses_the_campaign_timezone(self) -> None:
        # 15:00 UTC is 10:00 in New York (EST): inside a 09:00-17:00 local window.
        accepted = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
        due = self._due(accepted, 60, timezone="America/New_York")
        assert due == datetime(2026, 1, 5, 16, 0, tzinfo=UTC)
        # 23:00 UTC is 18:00 local: pushed to 09:00 local next day = 14:00 UTC.
        late = datetime(2026, 1, 5, 22, 0, tzinfo=UTC)
        assert self._due(late, 60, timezone="America/New_York") == datetime(
            2026, 1, 6, 14, 0, tzinfo=UTC
        )


class FakeWorld:
    """In-memory stand-in for CampaignWorkerRepository."""

    def __init__(self, steps, *, running: bool = True, **ctx_overrides: Any) -> None:
        self.steps = steps
        self.running = running
        self.ctx = {
            "activated_sequence_id": SEQUENCE,
            "schedule_generation": 1,
            "timezone": "UTC",
            "weekday_set": ALL_DAYS,
            "window_start_local": time(9, 0),
            "window_end_local": time(17, 0),
        }
        self.ctx.update(ctx_overrides)
        self.enrollments: dict[uuid.UUID, dict[str, Any]] = {}
        # (enrollment_id, step_id) -> message
        self.messages: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] = {}
        self.suppressed: set[uuid.UUID] = set()
        self.calls: list[str] = []

    # --- setup helpers -------------------------------------------------
    def enroll(self, *, name: str, company: str, mailbox: uuid.UUID | None = None):
        eid = uuid.uuid4()
        first = self.steps[0]
        self.enrollments[eid] = {
            "id": eid,
            "address_id": uuid.uuid4(),
            "assigned_mailbox_id": mailbox or uuid.uuid4(),
            "frozen_variables": {"first_name": name, "company": company},
            "frozen_destination": f"{name.lower()}@example.com",
            "state": "ACTIVE",
            "next_step_id": first["id"],
            "next_sequence_position": 1,
            "first_acceptance_at": None,
            "last_acceptance_at": None,
            "sender_address": "sender@acme.test",
            "sender_name": "Sam",
        }
        self.messages[(eid, first["id"])] = {
            "id": uuid.uuid4(),
            "status": "SCHEDULED",
            "accepted_at": None,
            "mailbox_id": self.enrollments[eid]["assigned_mailbox_id"],
            "subject": f"One {name}",
        }
        return eid

    def accept(self, eid: uuid.UUID, at: datetime) -> None:
        current = self.enrollments[eid]["next_step_id"]
        self.messages[(eid, current)].update(status="SENT", accepted_at=at)

    def fail(self, eid: uuid.UUID) -> None:
        current = self.enrollments[eid]["next_step_id"]
        self.messages[(eid, current)].update(status="FAILED", accepted_at=None)

    # --- repository surface -------------------------------------------
    def get_progression_context(self, *, workspace_id, campaign_id):
        return self.ctx if self.running else None

    def list_sequence_steps(self, *, workspace_id, sequence_id):
        return self.steps

    def fetch_progressable_enrollments(
        self, *, workspace_id, campaign_id, limit, after_id=None
    ):
        rows = []
        for eid in sorted(self.enrollments):
            e = self.enrollments[eid]
            if after_id is not None and eid <= after_id:
                continue
            if e["state"] != "ACTIVE" or e["next_step_id"] is None:
                continue
            m = self.messages.get((eid, e["next_step_id"]))
            if m is None or m["status"] not in ("SENT", "FAILED"):
                continue
            rows.append(
                {
                    "enrollment_id": eid,
                    "address_id": e["address_id"],
                    "assigned_mailbox_id": e["assigned_mailbox_id"],
                    "frozen_variables": e["frozen_variables"],
                    "frozen_destination": e["frozen_destination"],
                    "next_step_id": e["next_step_id"],
                    "message_status": m["status"],
                    "accepted_at": m["accepted_at"],
                    "message_id": m["id"],
                    "message_subject": m.get("subject"),
                    "message_mailbox_id": m.get("mailbox_id"),
                    "sender_address": e["sender_address"],
                    "sender_name": e["sender_name"],
                }
            )
        return rows[:limit]

    def insert_followup_message(self, **kw):
        key = (kw["enrollment_id"], kw["step_id"])
        if key in self.messages:
            return None
        message_id = uuid.uuid4()
        self.messages[key] = {
            "id": message_id,
            "status": "PLANNED",
            "accepted_at": None,
            "mailbox_id": kw["mailbox_id"],
            "schedule_generation": kw["schedule_generation"],
            "campaign_id": kw["campaign_id"],
            "sequence_id": kw["sequence_id"],
            "reply_to_message_id": kw.get("reply_to_message_id"),
        }
        self.calls.append("insert")
        return message_id

    def render_message(self, **kw):
        for message in self.messages.values():
            if message["id"] == kw["message_id"] and message["status"] == "PLANNED":
                message.update(
                    status="SCHEDULED",
                    subject=kw["content_subject"],
                    body=kw["content_body_html"],
                    digest=kw["content_digest"],
                    destination=kw["frozen_destination"],
                    sender=kw["frozen_sender_address"],
                    due_at=kw["due_at"],
                    anchor_at=kw["anchor_at"],
                )
        self.calls.append("render")

    def _guarded(self, enrollment_id, expected_step_id):
        e = self.enrollments[enrollment_id]
        if e["state"] != "ACTIVE" or e["next_step_id"] != expected_step_id:
            return None
        return e

    def advance_enrollment(
        self,
        *,
        enrollment_id,
        expected_step_id,
        next_step_id,
        next_position,
        accepted_at,
        **_,
    ):
        e = self._guarded(enrollment_id, expected_step_id)
        if e is None:
            return False
        e.update(
            next_step_id=next_step_id,
            next_sequence_position=next_position,
            first_acceptance_at=e["first_acceptance_at"] or accepted_at,
            last_acceptance_at=accepted_at,
        )
        return True

    def complete_enrollment(self, *, enrollment_id, expected_step_id, accepted_at, **_):
        e = self._guarded(enrollment_id, expected_step_id)
        if e is None:
            return False
        e.update(
            state="COMPLETED",
            next_step_id=None,
            next_sequence_position=None,
            first_acceptance_at=e["first_acceptance_at"] or accepted_at,
            last_acceptance_at=accepted_at,
        )
        return True

    def fail_enrollment(self, *, enrollment_id, expected_step_id, **_):
        e = self._guarded(enrollment_id, expected_step_id)
        if e is None:
            return False
        e.update(state="FAILED", next_step_id=None, next_sequence_position=None)
        return True


def _service(world: FakeWorld) -> ProgressionService:
    return ProgressionService(
        world, is_suppressed=lambda ws, address_id: address_id in world.suppressed
    )


def _run(world: FakeWorld, **kwargs: Any):
    return _service(world).advance_batch(
        workspace_id=WS, campaign_id=CAMPAIGN, limit=kwargs.pop("limit", 100), **kwargs
    )


T = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a Monday, inside the window


class TestAdvanceBatch:
    def test_nothing_happens_until_the_first_email_is_accepted(self) -> None:
        world = FakeWorld(_three_email_sequence())
        world.enroll(name="Ada", company="Engine")
        summary = _run(world)
        assert summary.fetched == 0
        assert world.calls == []

    def test_schedules_the_second_email_after_acceptance_plus_wait(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)

        summary = _run(world)

        assert (summary.advanced, summary.completed) == (1, 0)
        second = world.messages[(eid, steps[2]["id"])]
        assert second["status"] == "SCHEDULED"
        assert second["due_at"] == T + timedelta(days=2)
        assert second["anchor_at"] == T
        assert second["schedule_generation"] == 1
        enrollment = world.enrollments[eid]
        assert enrollment["next_step_id"] == steps[2]["id"]
        assert enrollment["next_sequence_position"] == 3
        assert enrollment["first_acceptance_at"] == T
        assert enrollment["last_acceptance_at"] == T
        assert enrollment["state"] == "ACTIVE"

    def test_second_email_is_rendered_for_that_prospect_and_mailbox(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        mailbox = uuid.uuid4()
        eid = world.enroll(name="Ada", company="Engine Co", mailbox=mailbox)
        world.accept(eid, T)
        _run(world)

        second = world.messages[(eid, steps[2]["id"])]
        assert second["subject"] == "Two for Engine Co"
        assert "Hi Ada, follow-up 2" in second["body"]
        assert second["destination"] == "ada@example.com"
        assert second["sender"] == "sender@acme.test"
        assert second["mailbox_id"] == mailbox

    def test_running_twice_creates_exactly_one_message(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        _run(world)
        again = _run(world)

        assert again.fetched == 0  # the pointer moved, so nothing is progressable
        assert world.calls.count("insert") == 1
        assert len([k for k in world.messages if k[0] == eid]) == 2

    def test_a_message_that_already_exists_does_not_duplicate_or_block_the_pointer(
        self,
    ) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        # A previous run got as far as inserting the message.
        world.messages[(eid, steps[2]["id"])] = {
            "id": uuid.uuid4(),
            "status": "SCHEDULED",
            "accepted_at": None,
        }
        summary = _run(world)
        assert summary.advanced == 1
        assert "insert" not in world.calls and "render" not in world.calls
        assert world.enrollments[eid]["next_step_id"] == steps[2]["id"]

    def test_walks_the_whole_sequence_and_completes(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")

        world.accept(eid, T)
        _run(world)
        second_due = world.messages[(eid, steps[2]["id"])]["due_at"]
        assert second_due == T + timedelta(days=2)

        # The second email is accepted a little late; the third is anchored on
        # ITS acceptance time, not on the planned time.
        second_accepted = second_due + timedelta(minutes=7)
        world.accept(eid, second_accepted)
        _run(world)
        third = world.messages[(eid, steps[4]["id"])]
        assert third["due_at"] == second_accepted + timedelta(days=3)
        assert third["anchor_at"] == second_accepted
        assert third["subject"] == "Three"

        third_accepted = third["due_at"] + timedelta(minutes=1)
        world.accept(eid, third_accepted)
        summary = _run(world)
        assert summary.completed == 1
        enrollment = world.enrollments[eid]
        assert enrollment["state"] == "COMPLETED"
        assert enrollment["next_step_id"] is None
        assert enrollment["next_sequence_position"] is None
        assert enrollment["first_acceptance_at"] == T
        assert enrollment["last_acceptance_at"] == third_accepted
        # Nothing further is ever planned.
        assert _run(world).fetched == 0
        assert len([k for k in world.messages if k[0] == eid]) == 3

    def test_each_prospect_progresses_on_their_own_clock_with_their_own_data(
        self,
    ) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        ada = world.enroll(name="Ada", company="Engine")
        grace = world.enroll(name="Grace", company="Navy")
        linus = world.enroll(name="Linus", company="Kernel")

        world.accept(ada, T)
        world.accept(grace, T + timedelta(hours=3))
        # Linus's first email has not been accepted yet.
        summary = _run(world)

        assert summary.advanced == 2
        ada_2 = world.messages[(ada, steps[2]["id"])]
        grace_2 = world.messages[(grace, steps[2]["id"])]
        assert ada_2["due_at"] == T + timedelta(days=2)
        assert grace_2["due_at"] == T + timedelta(days=2, hours=3)
        assert ada_2["subject"] == "Two for Engine"
        assert grace_2["subject"] == "Two for Navy"
        assert ada_2["destination"] == "ada@example.com"
        assert grace_2["destination"] == "grace@example.com"
        assert (linus, steps[2]["id"]) not in world.messages
        assert world.enrollments[linus]["next_step_id"] == steps[0]["id"]

    def test_hours_based_wait_is_honoured(self) -> None:
        steps = _three_email_sequence(wait1=180)
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        _run(world)
        assert world.messages[(eid, steps[2]["id"])]["due_at"] == T + timedelta(hours=3)

    def test_follow_up_is_pushed_into_the_sending_window(self) -> None:
        steps = _three_email_sequence(wait1=120)
        world = FakeWorld(steps, weekday_set=WEEKDAYS)
        eid = world.enroll(name="Ada", company="Engine")
        friday_late = datetime(2026, 3, 6, 16, 30, tzinfo=UTC)
        world.accept(eid, friday_late)
        _run(world)
        due = world.messages[(eid, steps[2]["id"])]["due_at"]
        assert due == datetime(2026, 3, 9, 9, 0, tzinfo=UTC)
        assert world.messages[(eid, steps[2]["id"])]["anchor_at"] == friday_late

    def test_stamps_the_campaigns_current_schedule_generation(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps, schedule_generation=4)
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        _run(world)
        assert world.messages[(eid, steps[2]["id"])]["schedule_generation"] == 4

    def test_paused_or_not_running_campaign_plans_nothing(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps, running=False)
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        summary = _run(world)
        assert summary.fetched == 0
        assert world.calls == []
        # Resumed: the follow-up is planned then, from the ORIGINAL acceptance time.
        world.running = True
        _run(world)
        assert world.messages[(eid, steps[2]["id"])]["due_at"] == T + timedelta(days=2)

    @pytest.mark.parametrize("state", ["STOPPED", "COMPLETED", "FAILED"])
    def test_stopped_enrollments_are_left_alone(self, state: str) -> None:
        # e.g. a reply stopped the enrollment: no follow-up must be created.
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        world.enrollments[eid]["state"] = state
        summary = _run(world)
        assert summary.fetched == 0
        assert (eid, steps[2]["id"]) not in world.messages

    def test_suppressed_recipient_gets_no_follow_up(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.suppressed.add(world.enrollments[eid]["address_id"])
        world.accept(eid, T)
        summary = _run(world)
        assert summary.skipped == 1 and summary.reasons == {"suppressed": 1}
        assert (eid, steps[2]["id"]) not in world.messages
        assert world.enrollments[eid]["next_step_id"] == steps[0]["id"]

    def test_a_suppressed_recipient_on_the_last_step_still_completes(self) -> None:
        steps = _three_email_sequence()[:1]
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.suppressed.add(world.enrollments[eid]["address_id"])
        world.accept(eid, T)
        assert _run(world).completed == 1

    def test_permanently_failed_email_fails_the_enrollment(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.fail(eid)
        summary = _run(world)
        assert summary.failed == 1
        assert world.enrollments[eid]["state"] == "FAILED"
        assert world.enrollments[eid]["next_step_id"] is None
        assert (eid, steps[2]["id"]) not in world.messages

    def test_missing_sender_is_skipped_not_guessed(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.enrollments[eid]["assigned_mailbox_id"] = None
        world.accept(eid, T)
        summary = _run(world)
        assert summary.reasons == {"no_sender": 1}
        assert (eid, steps[2]["id"]) not in world.messages

    def test_one_bad_row_does_not_stop_the_rest_of_the_batch(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        bad = world.enroll(name="Bad", company="X")
        good = world.enroll(name="Good", company="Y")
        world.enrollments[bad]["assigned_mailbox_id"] = None
        world.accept(bad, T)
        world.accept(good, T)
        summary = _run(world)
        assert (summary.advanced, summary.skipped) == (1, 1)
        assert (good, steps[2]["id"]) in world.messages

    def test_no_sending_window_is_skipped_and_reported(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(
            steps,
            window_start_local=time(9, 0),
            window_end_local=time(9, 0),  # empty window
        )
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        summary = _run(world)
        assert summary.reasons == {"no_sending_window": 1}
        assert (eid, steps[2]["id"]) not in world.messages

    def test_lost_race_on_the_pointer_is_reported_not_double_applied(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        original = world.advance_enrollment

        def stolen(**kw):
            world.enrollments[eid]["state"] = "STOPPED"  # a reply landed first
            return original(**kw)

        world.advance_enrollment = stolen  # type: ignore[method-assign]
        summary = _run(world)
        assert summary.reasons == {"concurrent_change": 1}
        assert world.enrollments[eid]["state"] == "STOPPED"

    def test_chunking_resumes_after_the_last_row_seen(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        ids = [world.enroll(name=f"P{i}", company="C") for i in range(5)]
        for eid in ids:
            world.accept(eid, T)
        world.suppressed.add(world.enrollments[min(ids)]["address_id"])

        first = _run(world, limit=2)
        assert first.fetched == 2 and first.last_enrollment_id is not None
        second = _run(world, limit=2, after_id=first.last_enrollment_id)
        third = _run(world, limit=2, after_id=second.last_enrollment_id)
        # The permanently skipped (suppressed) row never starves the others.
        assert first.advanced + second.advanced + third.advanced == 4
        assert third.fetched == 1


class HyperWorld(FakeWorld):
    """A hyper-personalized campaign: follow-ups are not rendered here; a
    generation job is created instead (ADR-0011)."""

    def __init__(self, steps, **kw: Any) -> None:
        super().__init__(steps, campaign_type="HYPER_PERSONALIZED", **kw)
        self.pending: list[dict[str, Any]] = []

    def mark_generation_pending(self, **kw):
        for message in self.messages.values():
            if message["id"] == kw["message_id"]:
                message.update(due_at=kw["due_at"], anchor_at=kw["anchor_at"])
        self.pending.append(kw)
        self.calls.append("pending")
        return True


class TestHyperPersonalizedProgression:
    def test_follow_up_stays_planned_and_gets_a_generation_job(self) -> None:
        world = HyperWorld(_three_email_sequence())
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)

        summary = _run(world)

        assert summary.advanced == 1
        step3 = world.steps[2]["id"]
        message = world.messages[(eid, step3)]
        assert message["status"] == "PLANNED"  # no content snapshot, not sendable
        assert "subject" not in message and "digest" not in message
        assert "render" not in world.calls and world.calls == ["insert", "pending"]
        # The pointer still moves, exactly as for standard campaigns.
        assert world.enrollments[eid]["next_step_id"] == step3

    def test_generation_is_scheduled_one_lead_time_before_the_due_time(self) -> None:
        world = HyperWorld(_three_email_sequence(wait1=2880))
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        ProgressionService(
            world,
            is_suppressed=lambda ws, a: False,
            personalization_lead_seconds=1800,
            personalization_max_attempts=4,
        ).advance_batch(workspace_id=WS, campaign_id=CAMPAIGN, limit=10)

        (job,) = world.pending
        due = T + timedelta(days=2)
        assert job["due_at"] == due and job["anchor_at"] == T
        assert job["next_attempt_at"] == due - timedelta(seconds=1800)
        assert job["max_attempts"] == 4
        assert job["step_id"] == world.steps[2]["id"]
        assert job["enrollment_id"] == eid

    def test_the_intended_time_is_still_projected_into_the_sending_window(self) -> None:
        world = HyperWorld(_three_email_sequence(wait1=120))
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, datetime(2026, 3, 2, 16, 30, tzinfo=UTC))  # near close
        _run(world)
        (job,) = world.pending
        assert job["due_at"] == datetime(2026, 3, 3, 9, 0, tzinfo=UTC)

    def test_a_replayed_run_does_not_create_a_second_job(self) -> None:
        world = HyperWorld(_three_email_sequence())
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        _run(world)
        # Simulate the pointer not having moved (crash after insert): the unique
        # (enrollment, step) key returns no new message, so no second job.
        world.enrollments[eid]["next_step_id"] = world.steps[0]["id"]
        _run(world)
        assert len(world.pending) == 1

    def test_a_failed_email_still_fails_the_enrollment_without_a_job(self) -> None:
        world = HyperWorld(_three_email_sequence())
        eid = world.enroll(name="Ada", company="Engine")
        world.fail(eid)
        summary = _run(world)
        assert summary.failed == 1 and world.pending == []
        assert world.enrollments[eid]["state"] == "FAILED"

    def test_standard_campaigns_are_unchanged(self) -> None:
        world = FakeWorld(_three_email_sequence())
        eid = world.enroll(name="Ada", company="Engine")
        world.accept(eid, T)
        _run(world)
        assert world.calls == ["insert", "render"]
        assert world.messages[(eid, world.steps[2]["id"])]["status"] == "SCHEDULED"


class TestSameThreadFollowUp:
    """ADR-0021: a threaded follow-up replies to the email the lead was just sent."""

    def _threaded_sequence(self):
        steps = _three_email_sequence()
        steps[2]["email_same_thread"] = True
        steps[2]["email_subject"] = None
        return steps

    def test_parent_and_re_subject_are_frozen_with_the_follow_up(self) -> None:
        from app.modules.campaigns.message_rendering import compute_content_digest

        steps = self._threaded_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine Co")
        world.accept(eid, T)
        _run(world)

        parent = world.messages[(eid, steps[0]["id"])]
        second = world.messages[(eid, steps[2]["id"])]
        assert second["reply_to_message_id"] == parent["id"]
        assert second["subject"] == "Re: One Ada"
        assert "Hi Ada, follow-up 2" in second["body"]
        assert second["digest"] == compute_content_digest(
            "Re: One Ada", second["body"], 1
        )

    def test_an_unthreaded_follow_up_has_no_parent(self) -> None:
        steps = _three_email_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine Co")
        world.accept(eid, T)
        _run(world)
        assert world.messages[(eid, steps[2]["id"])]["reply_to_message_id"] is None

    def test_a_parent_sent_from_another_mailbox_is_not_linked(self) -> None:
        steps = self._threaded_sequence()
        world = FakeWorld(steps)
        eid = world.enroll(name="Ada", company="Engine Co")
        world.messages[(eid, steps[0]["id"])]["mailbox_id"] = uuid.uuid4()
        world.accept(eid, T)
        _run(world)
        second = world.messages[(eid, steps[2]["id"])]
        assert second["reply_to_message_id"] is None
        # Still a follow-up of the same conversation.
        assert second["subject"] == "Re: One Ada"

    def test_a_personalized_follow_up_gets_its_parent_at_insert(self) -> None:
        steps = self._threaded_sequence()
        world = HyperWorld(steps)
        eid = world.enroll(name="Ada", company="Engine Co")
        world.accept(eid, T)
        _run(world)
        parent = world.messages[(eid, steps[0]["id"])]
        second = world.messages[(eid, steps[2]["id"])]
        assert second["reply_to_message_id"] == parent["id"]
        assert second["status"] == "PLANNED"  # written later by the JIT writer

    def test_a_personalized_follow_up_from_another_mailbox_has_no_parent(self) -> None:
        steps = self._threaded_sequence()
        world = HyperWorld(steps)
        eid = world.enroll(name="Ada", company="Engine Co")
        world.messages[(eid, steps[0]["id"])]["mailbox_id"] = uuid.uuid4()
        world.accept(eid, T)
        _run(world)
        assert world.messages[(eid, steps[2]["id"])]["reply_to_message_id"] is None
