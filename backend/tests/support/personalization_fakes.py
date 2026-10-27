"""In-memory stand-ins for the personalization worker's database access.

`FakeStore.transaction()` yields a `FakeRepo` with the same surface as
`PersonalizationRepository` and mirrors the semantics that matter for
correctness: lease claims that skip leased rows, attempts charged at claim time,
finalize guarded on `status == 'PLANNED'`, and rollback of everything a failed
transaction did. No database, no network, no email.
"""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from typing import Any

from app.modules.personalization.ports import ResearchOutcome

WS = uuid.uuid4()
ALL_DAYS = 0b1111111

CONFIG: dict[str, Any] = {
    "objective": "Book a demo with SaaS founders",
    "offer": "We help SaaS teams automate manual outbound prospecting",
    "cta": "Would you be open to a quick conversation?",
    "target": "SaaS founders",
    "problem_solved": "Manual outbound work",
    "tone": "friendly",
    "must_mention": [],
    "never_say": ["guaranteed"],
}
REF_SUBJECT = "Quick idea for {{company}}"
REF_BODY = (
    "<p>Hi {{first_name}},</p>"
    "<p>We help SaaS companies improve their outbound process by automating "
    "manual prospecting work.</p>"
    "<p>Would you be open to a quick conversation?</p>"
    "<p>Best,<br>John</p>"
)
FROZEN: dict[str, Any] = {
    "first_name": "Sarah",
    "last_name": "Johnson",
    "company": "Acme",
    "title": "VP Sales",
    "company_industry": "Software",
    "email": "sarah@acme.test",
    "phone": "+1 415 555 0132",
    "linkedin_url": "https://linkedin.com/in/sarah-johnson",
}
NOW = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)  # a Monday, inside the window


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now += timedelta(**kwargs)


class FakeResearch:
    def __init__(self, outcome: ResearchOutcome | None = None) -> None:
        self.outcome = outcome or ResearchOutcome(status="NONE")
        self.calls = 0

    def research(self, *, workspace_id: Any, company_website: Any) -> ResearchOutcome:
        self.calls += 1
        return self.outcome


class FakeStore:
    def __init__(self, clock: Clock, *, max_attempts: int = 3) -> None:
        self.clock = clock
        self.campaign: dict[str, Any] = {
            "status": "RUNNING",
            "planning_status": "READY",
            "campaign_type": "HYPER_PERSONALIZED",
        }
        self.sequence_id = uuid.uuid4()
        self.campaign_id = uuid.uuid4()
        self.enrollment_id = uuid.uuid4()
        self.address_id = uuid.uuid4()
        self.step_id = uuid.uuid4()
        self.step: dict[str, Any] = {
            "position": 1,
            "email_subject": REF_SUBJECT,
            "email_body_html": REF_BODY,
            "email_preheader": None,
            "email_same_thread": False,
        }
        self.config: dict[str, Any] | None = dict(CONFIG)
        self.enrollment: dict[str, Any] = {
            "state": "ACTIVE",
            "next_step_id": self.step_id,
            "frozen_variables": dict(FROZEN),
        }
        self.message_id = uuid.uuid4()
        self.message: dict[str, Any] = {
            "status": "PLANNED",
            "due_at": clock.now + timedelta(minutes=30),
            "anchor_at": clock.now,
            "terminal_reason": None,
            # content_subject of messages.reply_to_message_id (ADR-0021)
            "parent_subject": None,
        }
        self.generation_id = uuid.uuid4()
        self.generation: dict[str, Any] = {
            "state": "PENDING",
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "transient_error_count": 0,
            "next_attempt_at": clock.now - timedelta(minutes=1),
            "lease_owner": None,
            "lease_expires_at": None,
            "last_failure_codes": None,
            "failure_code": None,
            "fallback_used": False,
        }
        self.attempts: list[dict[str, Any]] = []
        self.usage: dict[str, int] = {}
        self.tokens: dict[str, int] = {"in": 0, "out": 0}
        self.previous: dict[str, Any] | None = None
        self.audits: list[dict[str, Any]] = []
        self.fail_finalize_once = False
        self.suppressed = False
        self.previews: list[dict[str, Any]] = []
        self.step_waits: list[dict[str, Any]] = []
        self.sequence_head: dict[str, Any] | None = {
            "status": "DRAFT",
            "campaign_type": "HYPER_PERSONALIZED",
            "sequence_id": self.sequence_id,
            "personalization_config": dict(CONFIG),
        }

    # -- transactions with rollback --------------------------------------
    @contextmanager
    def transaction(self) -> Any:
        snapshot = copy.deepcopy(
            (
                self.generation,
                self.message,
                self.attempts,
                self.usage,
                self.tokens,
                self.previews,
                self.audits,
            )
        )
        try:
            yield FakeRepo(self)
        except BaseException:
            (
                self.generation,
                self.message,
                self.attempts,
                self.usage,
                self.tokens,
                self.previews,
                self.audits,
            ) = snapshot
            raise


class FakeRepo:
    """Same surface as PersonalizationRepository."""

    def __init__(self, store: FakeStore) -> None:
        self.s = store

    # -- budget -----------------------------------------------------------
    def reserve_usage(
        self, *, workspace_id: Any, day: Any, kind: str, cap: int
    ) -> bool:
        used = self.s.usage.get(kind, 0)
        if used >= cap:
            return False
        self.s.usage[kind] = used + 1
        return True

    def refund_usage(self, *, workspace_id: Any, day: Any, kind: str) -> None:
        self.s.usage[kind] = max(0, self.s.usage.get(kind, 0) - 1)

    def add_tokens(self, *, input_tokens: int, output_tokens: int, **_: Any) -> None:
        self.s.tokens["in"] += input_tokens
        self.s.tokens["out"] += output_tokens

    def purge_expired(self, **_: Any) -> int:
        return 0

    def is_suppressed(self, **_: Any) -> bool:
        return self.s.suppressed

    # -- generation jobs --------------------------------------------------
    def _lease_free(self, g: dict[str, Any]) -> bool:
        return g["lease_owner"] is None or g["lease_expires_at"] < self.s.clock.now

    def fail_exhausted(self, *, limit: int, **_: Any) -> list[dict[str, Any]]:
        g = self.s.generation
        if (
            g["state"] == "PENDING"
            and g["attempt_count"] >= g["max_attempts"]
            and self._lease_free(g)
        ):
            self.abandon_open_attempts(
                workspace_id=WS, generation_id=self.s.generation_id
            )
            self.fail_generation(
                workspace_id=WS,
                generation_id=self.s.generation_id,
                message_id=self.s.message_id,
                failure_code="attempts_exhausted",
                failure_codes=g["last_failure_codes"],
            )
            return [{"id": self.s.generation_id}]
        return []

    def claim_due(
        self, *, lease_owner: str, ttl_seconds: int, limit: int, **_: Any
    ) -> list[dict[str, Any]]:
        g = self.s.generation
        c = self.s.campaign
        if (
            g["state"] == "PENDING"
            and g["next_attempt_at"] <= self.s.clock.now
            and g["attempt_count"] < g["max_attempts"]
            and self._lease_free(g)
            and self.s.message["status"] == "PLANNED"
            and c["status"] == "RUNNING"
            and c["planning_status"] == "READY"
        ):
            g["lease_owner"] = lease_owner
            g["lease_expires_at"] = self.s.clock.now + timedelta(seconds=ttl_seconds)
            g["attempt_count"] += 1
            return [
                {
                    "id": self.s.generation_id,
                    "message_id": self.s.message_id,
                    "attempt_count": g["attempt_count"],
                    "max_attempts": g["max_attempts"],
                    "transient_error_count": g["transient_error_count"],
                    "last_failure_codes": g["last_failure_codes"],
                }
            ]
        return []

    def abandon_open_attempts(self, **_: Any) -> None:
        for attempt in self.s.attempts:
            if attempt["outcome"] == "RESERVED":
                attempt["outcome"] = "ABANDONED"

    def insert_attempt(self, **_: Any) -> int:
        number = len(self.s.attempts) + 1
        self.s.attempts.append({"attempt_no": number, "outcome": "RESERVED"})
        return number

    def finish_attempt(
        self, *, attempt_no: int, outcome: str, failure_codes: Any = None, **kw: Any
    ) -> None:
        for attempt in self.s.attempts:
            if attempt["attempt_no"] == attempt_no and attempt["outcome"] == "RESERVED":
                attempt.update(outcome=outcome, failure_codes=failure_codes, **kw)

    def release_claim(
        self,
        *,
        delay_seconds: float,
        refund_attempt: bool,
        transient_error: bool = False,
        failure_codes: Any = None,
        **_: Any,
    ) -> None:
        g = self.s.generation
        g["lease_owner"] = None
        g["lease_expires_at"] = None
        if refund_attempt and g["attempt_count"] > 0:
            g["attempt_count"] -= 1
        if transient_error:
            g["transient_error_count"] += 1
        g["next_attempt_at"] = self.s.clock.now + timedelta(seconds=delay_seconds)
        if failure_codes:
            g["last_failure_codes"] = list(failure_codes)

    def load_work_item(self, **_: Any) -> dict[str, Any]:
        s = self.s
        return {
            "generation_id": s.generation_id,
            "message_id": s.message_id,
            "campaign_id": s.campaign_id,
            "sequence_id": s.sequence_id,
            "step_id": s.step_id,
            "enrollment_id": s.enrollment_id,
            "message_status": s.message["status"],
            "due_at": s.message["due_at"],
            "anchor_at": s.message["anchor_at"],
            "message_created_at": s.message.get("created_at"),
            "campaign_type": s.campaign["campaign_type"],
            "campaign_status": s.campaign["status"],
            "planning_status": s.campaign["planning_status"],
            "enrollment_state": s.enrollment["state"],
            "next_step_id": s.enrollment["next_step_id"],
            "frozen_variables": s.enrollment["frozen_variables"],
            "address_id": s.address_id,
            "email_subject": s.step["email_subject"],
            "email_body_html": s.step["email_body_html"],
            "email_preheader": s.step["email_preheader"],
            "email_same_thread": s.step.get("email_same_thread", False),
            "parent_subject": s.message.get("parent_subject"),
            "step_position": s.step["position"],
            "step_version": s.step.get("version", 1),
            "personalization_config": s.config,
            "timezone": "UTC",
            "weekday_set": ALL_DAYS,
            "window_start_local": time(9, 0),
            "window_end_local": time(17, 0),
        }

    def load_previous_email(self, **_: Any) -> dict[str, Any] | None:
        return self.s.previous

    def record_same_thread_unavailable(
        self, *, message_id: Any, reason: str, **_: Any
    ) -> None:
        self.s.audits.append({"message_id": message_id, "reason": reason})

    def retime_planned_message(self, *, due_at: datetime, **_: Any) -> bool:
        m = self.s.message
        if m["status"] != "PLANNED":
            return False
        m["due_at"] = due_at
        return True

    def lock_for_finalize(self, **_: Any) -> dict[str, Any]:
        s = self.s
        return {
            "generation_state": s.generation["state"],
            "message_id": s.message_id,
            "step_id": s.step_id,
            "enrollment_id": s.enrollment_id,
            "message_status": s.message["status"],
            "due_at": s.message["due_at"],
            "anchor_at": s.message["anchor_at"],
            "address_id": s.address_id,
            "campaign_status": s.campaign["status"],
            "planning_status": s.campaign["planning_status"],
            "enrollment_state": s.enrollment["state"],
            "next_step_id": s.enrollment["next_step_id"],
            "step_version": s.step.get("version", 1),
        }

    def finalize_message(
        self,
        *,
        subject: str,
        body_html: str,
        content_digest: str,
        renderer_version: int,
        due_at: datetime,
        **_: Any,
    ) -> bool:
        if self.s.fail_finalize_once:
            self.s.fail_finalize_once = False
            raise RuntimeError("simulated crash before commit")
        m = self.s.message
        if m["status"] != "PLANNED":
            return False
        m.update(
            status="SCHEDULED",
            subject=subject,
            body=body_html,
            digest=content_digest,
            renderer_version=renderer_version,
            due_at=due_at,
        )
        return True

    def complete_generation(self, *, state: str, **kw: Any) -> None:
        g = self.s.generation
        g.update(state=state, lease_owner=None, lease_expires_at=None, **kw)

    def fail_generation(
        self,
        *,
        failure_code: str,
        failure_codes: Any = None,
        **_: Any,
    ) -> None:
        if self.s.message["status"] == "PLANNED":
            self.s.message.update(
                status="FAILED",
                terminal_reason=f"personalization_failed:{failure_code}",
            )
        g = self.s.generation
        g.update(
            state="FAILED",
            failure_code=failure_code,
            lease_owner=None,
            lease_expires_at=None,
        )
        if failure_codes:
            g["last_failure_codes"] = list(failure_codes)

    def supersede_generation(self, **_: Any) -> None:
        g = self.s.generation
        g.update(state="SUPERSEDED", lease_owner=None, lease_expires_at=None)

    # -- previews ---------------------------------------------------------
    def load_sequence_config(self, **_: Any) -> dict[str, Any] | None:
        return self.s.sequence_head

    def claim_preview_rows(self, **_: Any) -> list[dict[str, Any]]:
        return [p for p in self.s.previews if p["state"] == "PENDING"]

    def list_step_waits(self, **_: Any) -> list[dict[str, Any]]:
        return self.s.step_waits

    def finish_preview(self, *, preview_id: Any, **kw: Any) -> None:
        for preview in self.s.previews:
            if preview["id"] == preview_id and preview["state"] == "PENDING":
                preview.update(kw)
