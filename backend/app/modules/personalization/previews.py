"""Sample-preview generation (worker side; ADR-0011).

Same service and validator as the just-in-time path. Previews are interactive:
a bounded number of attempts happen inline, failures are reported on the row
(the user can regenerate) instead of being retried durably, and nothing here
touches messages. Steps are generated in order for each sample lead, so a
follow-up preview is written against the step before it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.modules.campaigns.message_rendering import reply_subject
from app.modules.personalization.budget import RpmLimiter
from app.modules.personalization.config_schema import parse_config
from app.modules.personalization.ports import ModelError, PreviousEmail
from app.modules.personalization.service import (
    AttemptOutcome,
    GenerationInputs,
    PersonalizationService,
    facts_payload,
)
from app.modules.personalization.text_utils import html_to_text

logger = logging.getLogger(__name__)

_EXCERPT_CHARS = 500
_PREVIOUS_BODY_CHARS = 4000


@dataclass
class PreviewSummary:
    ok: int = 0
    failed: int = 0
    codes: dict[str, int] = field(default_factory=dict)


class PreviewRunner:
    def __init__(
        self,
        *,
        db: Any,
        service: PersonalizationService,
        rpm: RpmLimiter,
        max_attempts: int,
        daily_preview_cap: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._db = db
        self._service = service
        self._rpm = rpm
        self._max_attempts = max_attempts
        self._cap = daily_preview_cap
        self._clock = clock

    def run_batch(
        self, *, workspace_id: UUID, campaign_id: UUID, batch_id: UUID
    ) -> PreviewSummary:
        summary = PreviewSummary()
        with self._db.transaction() as repo:
            head = repo.load_sequence_config(
                workspace_id=workspace_id, campaign_id=campaign_id
            )
            rows = repo.claim_preview_rows(
                workspace_id=workspace_id, campaign_id=campaign_id, batch_id=batch_id
            )
            waits = (
                repo.list_step_waits(
                    workspace_id=workspace_id,
                    sequence_id=UUID(str(head["sequence_id"])),
                )
                if head is not None and head["sequence_id"] is not None
                else []
            )
        if head is None or head["status"] != "DRAFT":
            for row in rows:
                self._finish_failed(workspace_id, row, ["campaign_not_draft"], summary)
            return summary
        try:
            objective = parse_config(head["personalization_config"])
        except ValidationError:
            objective = None
        if objective is None:
            for row in rows:
                self._finish_failed(workspace_id, row, ["objective_invalid"], summary)
            return summary

        chain: dict[str, PreviousEmail] = {}
        chain_pos: dict[str, int] = {}
        failed_members: set[str] = set()
        for row in rows:  # ordered by member then step position
            member = str(row["audience_member_id"])
            if member in failed_members:
                self._finish_failed(
                    workspace_id, row, ["previous_step_failed"], summary
                )
                continue
            previous = chain.get(member)
            if previous is not None:
                previous = PreviousEmail(
                    subject=previous.subject,
                    body_text=previous.body_text,
                    angle=previous.angle,
                    fact_ids_used=previous.fact_ids_used,
                    facts_used=previous.facts_used,
                    days_since_sent=_days_between(
                        waits, chain_pos[member], int(row["step_position"])
                    ),
                )
            outcome = self._generate(workspace_id, row, objective, previous, summary)
            if outcome is None:
                failed_members.add(member)
                continue
            chain[member] = PreviousEmail(
                subject=outcome.subject,
                body_text=html_to_text(outcome.body_html)[:_PREVIOUS_BODY_CHARS],
                angle=outcome.angle,
                fact_ids_used=tuple(f.id for f in outcome.facts_used),
                facts_used=tuple(f.text for f in outcome.facts_used),
                days_since_sent=0,
            )
            chain_pos[member] = int(row["step_position"])
        return summary

    # ------------------------------------------------------------------

    def _generate(
        self,
        workspace_id: UUID,
        row: Mapping[str, Any],
        objective: Any,
        previous: PreviousEmail | None,
        summary: PreviewSummary,
    ) -> AttemptOutcome | None:
        retry_codes: tuple[str, ...] = ()
        last_codes: list[str] = []
        for _ in range(self._max_attempts):
            if not self._rpm.allow():
                self._finish_failed(workspace_id, row, ["rate_limited"], summary)
                return None
            with self._db.transaction() as repo:
                reserved = repo.reserve_usage(
                    workspace_id=workspace_id,
                    day=self._clock().date(),
                    kind="PREVIEW",
                    cap=self._cap,
                )
            if not reserved:
                self._finish_failed(
                    workspace_id, row, ["preview_budget_exhausted"], summary
                )
                return None
            inputs = GenerationInputs(
                workspace_id=workspace_id,
                frozen_variables=row["frozen_variables"] or {},
                objective=objective,
                reference_subject=row["email_subject"] or "",
                reference_body_html=row["email_body_html"],
                reference_preheader=row["email_preheader"],
                step_position=int(row["step_position"]),
                previous=previous,
                retry_codes=retry_codes,
                # A same-thread step replies to this lead's previous sample
                # (ADR-0021). A batch holds every email step in order and a failed
                # step fails the ones after it, so that sample always exists here.
                thread_subject=(
                    reply_subject(previous.subject)
                    if row.get("email_same_thread") and previous is not None
                    else None
                ),
            )
            try:
                outcome = self._service.attempt(inputs)
            except ModelError as exc:
                last_codes = [exc.code]
                if exc.permanent or exc.transient:
                    break  # interactive: report and let the user retry
                retry_codes = (exc.code,)
                continue
            if outcome.kind == "REJECTED":
                last_codes = list(outcome.failure_codes)
                retry_codes = outcome.failure_codes
                continue
            self._finish_ok(workspace_id, row, outcome, summary)
            return outcome
        self._finish_failed(
            workspace_id, row, last_codes or ["validation_failed"], summary
        )
        return None

    def _finish_ok(
        self,
        workspace_id: UUID,
        row: Mapping[str, Any],
        outcome: AttemptOutcome,
        summary: PreviewSummary,
    ) -> None:
        with self._db.transaction() as repo:
            repo.finish_preview(
                workspace_id=workspace_id,
                preview_id=UUID(str(row["id"])),
                state="OK",
                subject=outcome.subject,
                body_html=outcome.body_html,
                facts=facts_payload(outcome.facts_used),
                research_summary={
                    "status": outcome.research_status,
                    "source_url": (outcome.research_url or "")[:300] or None,
                    "excerpt": outcome.research_excerpt[:_EXCERPT_CHARS],
                },
                fallback_used=outcome.kind == "FALLBACK",
                failure_codes=None,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
            )
            repo.add_tokens(
                workspace_id=workspace_id,
                day=self._clock().date(),
                kind="PREVIEW",
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
            )
        summary.ok += 1

    def fail_unfinished(
        self,
        *,
        workspace_id: UUID,
        campaign_id: UUID,
        batch_id: UUID,
        codes: list[str],
    ) -> int:
        """Give up on a batch: every sample not yet written is marked FAILED with
        `codes` (the user can generate new ones)."""
        with self._db.transaction() as repo:
            return repo.fail_pending_previews(
                workspace_id=workspace_id,
                campaign_id=campaign_id,
                batch_id=batch_id,
                failure_codes=codes,
            )

    def _finish_failed(
        self,
        workspace_id: UUID,
        row: Mapping[str, Any],
        codes: list[str],
        summary: PreviewSummary,
    ) -> None:
        with self._db.transaction() as repo:
            repo.finish_preview(
                workspace_id=workspace_id,
                preview_id=UUID(str(row["id"])),
                state="FAILED",
                subject=None,
                body_html=None,
                facts=None,
                research_summary=None,
                fallback_used=False,
                failure_codes=codes,
                input_tokens=None,
                output_tokens=None,
            )
        summary.failed += 1
        for code in codes:
            summary.codes[code] = summary.codes.get(code, 0) + 1


def _days_between(
    waits: list[Mapping[str, Any]], from_position: int, to_position: int
) -> int:
    minutes = sum(
        int(w["wait_duration_minutes"] or 0)
        for w in waits
        if w["kind"] == "WAIT" and from_position < int(w["position"]) < to_position
    )
    return max(0, minutes // 1440)
