from __future__ import annotations

import logging
import random
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.scheduler.outbox_repository import OutboxRepository
from app.modules.scheduler.repository import SchedulerRepository
from app.modules.scheduler.schemas import (
    ClaimResult,
    DueMessageCandidate,
    SchedulerIterationSummary,
    SendTaskPayload,
)

logger = logging.getLogger(__name__)


class SchedulerService:
    """Orchestrates due message discovery, fair batching, and atomic claiming."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or Settings.current()
        self.repository = SchedulerRepository(session)

    def start_due_campaigns(self, *, limit: int = 50) -> list[tuple[UUID, UUID, int]]:
        """Start every SCHEDULED campaign whose start time has arrived. One commit
        per campaign, so one bad row cannot undo the others. Returns
        (workspace_id, campaign_id, schedule_generation) for each campaign started.

        A campaign that was paused and resumed before its start was put back to
        SCHEDULED with a bumped generation and no reconcile (it was not RUNNING), so
        its old messages are fenced; the caller dispatches the reconcile pass for
        those (generation > 1) now that they can be sent."""
        started: list[tuple[UUID, UUID, int]] = []
        for workspace_id, campaign_id in self.repository.find_due_scheduled_campaigns(
            limit=limit
        ):
            try:
                generation = self.repository.start_due_campaign(
                    workspace_id=workspace_id, campaign_id=campaign_id
                )
                self.session.commit()
            except Exception as exc:
                self.session.rollback()
                logger.warning(
                    "Error starting due campaign %s: %s", campaign_id, exc,
                    extra={"workspace_id": str(workspace_id)},
                )
                continue
            if generation is None:
                continue  # paused, archived or started by another scheduler first
            logger.info(
                "CAMPAIGN_STARTED workspace=%s campaign=%s generation=%s",
                workspace_id,
                campaign_id,
                generation,
            )
            started.append((workspace_id, campaign_id, generation))
        return started

    def discover_and_claim_due_work(
        self,
        *,
        batch_size: int | None = None,
        max_per_workspace: int | None = None,
        authoritative_now: datetime | None = None,
    ) -> tuple[int, int]:
        """Discover due messages, apply fair allocation, and atomically claim them.

        Returns (discovered_count, claimed_count).
        """
        limit = batch_size or self.settings.scheduler_batch_size

        # Backpressure: a claim lasts scheduler_claim_lease_seconds, so claiming
        # faster than the send workers finish only makes claims expire unused.
        # Claim no more than there is room for.
        room = self.settings.scheduler_max_outstanding_claims - (
            self.repository.count_outstanding_claims()
        )
        if room <= 0:
            logger.info("Scheduler holding back: too many outstanding claims")
            return 0, 0
        limit = min(limit, room)

        candidates = self.repository.find_due_messages(
            limit=limit * 2,  # Fetch slightly wider candidate pool for fairness filtering
            authoritative_now=authoritative_now,
        )

        if not candidates:
            return 0, 0

        # Group candidates by workspace to enforce round-robin fairness
        # and prevent a single workspace with thousands of messages from starving others.
        by_workspace: dict[UUID, list[DueMessageCandidate]] = defaultdict(list)
        for cand in candidates:
            by_workspace[cand.workspace_id].append(cand)

        slice_cap = max_per_workspace or max(1, limit // max(1, len(by_workspace)))
        selected: list[DueMessageCandidate] = []

        # Round-robin selection across workspaces up to batch limit
        workspaces = list(by_workspace.keys())
        idx = 0
        while len(selected) < limit and any(by_workspace.values()):
            ws = workspaces[idx % len(workspaces)]
            if by_workspace[ws]:
                selected.append(by_workspace[ws].pop(0))
            idx += 1

        claimed_count = 0
        lease_seconds = self.settings.scheduler_claim_lease_seconds

        for cand in selected:
            try:
                result = self.repository.claim_due_message(
                    workspace_id=cand.workspace_id,
                    message_id=cand.id,
                    lease_seconds=lease_seconds,
                    authoritative_now=authoritative_now,
                )
                self.session.commit()

                if result.claimed:
                    claimed_count += 1
                    logger.info(
                        "Claimed due message for dispatch",
                        extra={
                            "message_id": str(cand.id),
                            "workspace_id": str(cand.workspace_id),
                            "dispatch_generation": result.dispatch_generation,
                            "purpose": cand.purpose,
                        },
                    )
                else:
                    logger.debug(
                        f"Message {cand.id} not claimed: {result.reason}"
                    )
            except Exception as e:
                self.session.rollback()
                logger.warning(
                    f"Error claiming message {cand.id}: {e}",
                    extra={"message_id": str(cand.id), "workspace_id": str(cand.workspace_id)},
                )

        return len(candidates), claimed_count

    def recover_expired_claims(
        self,
        *,
        batch_size: int = 50,
        authoritative_now: datetime | None = None,
    ) -> int:
        """Find QUEUED messages past their claim_expires_at lease and reset them."""
        expired_rows = self.repository.find_expired_claims(
            limit=batch_size,
            authoritative_now=authoritative_now,
        )

        if not expired_rows:
            return 0

        recovered_count = 0
        for row in expired_rows:
            try:
                mid = row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"]))
                ws_id = (
                    row["workspace_id"]
                    if isinstance(row["workspace_id"], UUID)
                    else UUID(str(row["workspace_id"]))
                )
                recovered = self.repository.recover_expired_claim(
                    workspace_id=ws_id,
                    message_id=mid,
                    dispatch_generation=row["dispatch_generation"],
                )
                self.session.commit()
                if recovered:
                    recovered_count += 1
                    logger.info(
                        "Recovered expired message claim",
                        extra={
                            "message_id": str(mid),
                            "workspace_id": str(ws_id),
                            "dispatch_generation": row["dispatch_generation"],
                        },
                    )
            except Exception as e:
                self.session.rollback()
                logger.warning(f"Error recovering expired claim: {e}")

        return recovered_count

    def get_lag_seconds(self) -> float | None:
        """Get oldest eligible due message lag in seconds."""
        return self.repository.get_oldest_due_lag_seconds()


class OutboxPublisherService:
    """Leases unpublished outbox deliveries and reliably publishes tasks to Celery."""

    def __init__(
        self,
        session: Session,
        celery_client: Any = None,
        settings: Settings | None = None,
        publisher_id: str | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or Settings.current()
        self.repository = OutboxRepository(session)
        self.publisher_id = publisher_id or f"pub-{uuid.uuid4()}"

        if celery_client is not None:
            self._celery = celery_client
        else:
            from workers.celery_app import celery_app
            self._celery = celery_app

    def publish_pending_deliveries(
        self,
        *,
        batch_size: int | None = None,
        consumer: str = "worker-send",
        authoritative_now: datetime | None = None,
    ) -> int:
        """Lease bounded pending deliveries, publish to queue, and record outcomes."""
        limit = batch_size or self.settings.outbox_publish_batch_size
        pending = self.repository.find_pending_deliveries(
            consumer=consumer,
            limit=limit,
            authoritative_now=authoritative_now,
        )

        if not pending:
            return 0

        published_count = 0
        lease_seconds = self.settings.outbox_lease_seconds

        for row in pending:
            delivery_id = (
                row["delivery_id"]
                if isinstance(row["delivery_id"], UUID)
                else UUID(str(row["delivery_id"]))
            )
            workspace_id = (
                row["workspace_id"]
                if isinstance(row["workspace_id"], UUID)
                else UUID(str(row["workspace_id"]))
            )
            work_id = (
                row["work_id"]
                if isinstance(row["work_id"], UUID)
                else UUID(str(row["work_id"]))
            )
            resource_id = (
                row["resource_id"]
                if isinstance(row["resource_id"], UUID)
                else UUID(str(row["resource_id"]))
            )

            # Step 1: Lease the delivery row in a short DB transaction
            try:
                leased = self.repository.lease_delivery(
                    delivery_id=delivery_id,
                    workspace_id=workspace_id,
                    lease_owner=self.publisher_id,
                    lease_seconds=lease_seconds,
                    authoritative_now=authoritative_now,
                )
                self.session.commit()
            except Exception as e:
                self.session.rollback()
                logger.warning(f"Failed to lease delivery {delivery_id}: {e}")
                continue

            if not leased:
                continue

            lease_generation = leased["lease_generation"]

            # Step 2: Build the minimal, strictly validated task payload
            # Extract generation from semantic_key "message_id:gen"
            semantic_key = str(row["semantic_key"])
            gen_str = semantic_key.split(":")[-1] if ":" in semantic_key else "1"
            dispatch_gen = int(gen_str) if gen_str.isdigit() else 1

            payload = SendTaskPayload(
                schema_version=1,
                work_id=str(work_id),
                delivery_id=str(delivery_id),
                message_id=str(resource_id),
                dispatch_id=str(delivery_id),
                resource_id=str(resource_id),
                workspace_id=str(workspace_id),
                dispatch_generation=dispatch_gen,
                correlation_id=str(row["correlation_id"]) if row["correlation_id"] else None,
            )

            # Step 3: Publish to Celery queue OUTSIDE of DB transaction locks
            publish_success = False
            error_message = ""

            try:
                # Use celery_app.send_task to publish to email.send
                self._celery.send_task(
                    "email.send",
                    kwargs={"payload": payload.model_dump()},
                    queue="email.send",
                )
                publish_success = True
            except Exception as e:
                error_message = str(e)
                logger.warning(
                    f"Redis publication failed for delivery {delivery_id}: {e}",
                    extra={
                        "delivery_id": str(delivery_id),
                        "message_id": str(resource_id),
                        "workspace_id": str(workspace_id),
                    },
                )

            # Step 4: Record publication result in DB in a short transaction
            try:
                if publish_success:
                    self.repository.mark_published(
                        delivery_id=delivery_id,
                        workspace_id=workspace_id,
                        lease_owner=self.publisher_id,
                        lease_generation=lease_generation,
                        authoritative_now=authoritative_now,
                    )
                    published_count += 1
                    logger.info(
                        "Published outbox task to email.send",
                        extra={
                            "delivery_id": str(delivery_id),
                            "message_id": str(resource_id),
                            "workspace_id": str(workspace_id),
                            "queue": "email.send",
                        },
                    )
                else:
                    # Exponential backoff with jitter: 2^attempts + jitter, capped at 300s
                    attempts = row["attempts"] + 1
                    backoff = min(300.0, (2.0 ** min(attempts, 8)) + random.uniform(0.5, 2.0))
                    self.repository.mark_retry(
                        delivery_id=delivery_id,
                        workspace_id=workspace_id,
                        lease_owner=self.publisher_id,
                        lease_generation=lease_generation,
                        backoff_seconds=backoff,
                        safe_error=error_message,
                        authoritative_now=authoritative_now,
                    )
                self.session.commit()
            except Exception as e:
                self.session.rollback()
                logger.error(
                    f"Failed to record publication result for delivery {delivery_id}: {e}"
                )

        return published_count

    def recover_stale_leases(self, limit: int = 50) -> int:
        """Reclaim deliveries stuck in LEASED state due to publisher crash."""
        try:
            recovered = self.repository.recover_stale_leases(limit=limit)
            self.session.commit()
            return recovered
        except Exception as e:
            self.session.rollback()
            logger.warning(f"Error recovering stale outbox leases: {e}")
            return 0
