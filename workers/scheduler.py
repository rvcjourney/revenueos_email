from __future__ import annotations

import logging
import signal
import time
from threading import Event
from uuid import UUID

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.session import session_scope
from app.modules.scheduler.service import OutboxPublisherService, SchedulerService

logger = logging.getLogger(__name__)

# A resume dispatches its own reconcile task; this sweep only repairs a lost one,
# so a slow cadence is enough and no setting is needed.
_RECONCILE_SWEEP_INTERVAL_SECONDS = 60.0
_RECONCILE_CAMPAIGNS_PER_RUN = 20
_PLANNING_RECOVERY_INTERVAL_SECONDS = 60.0
_PLANNING_RECOVERY_PER_RUN = 50


class SchedulerRuntime:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.current()
        self.stopped = Event()
        # Follow-up progression is swept on its own, slower cadence, walking the
        # RUNNING campaigns in id order across runs.
        self._progression_last_run = float("-inf")
        self._progression_cursor: UUID | None = None
        # Hyper-personalized generation (ADR-0011) has its own cadence and cursor.
        self._personalization_last_run = float("-inf")
        self._personalization_cursor: UUID | None = None
        # Post-resume reconcile repair sweep (ADR-0020).
        self._reconcile_last_run = float("-inf")
        self._reconcile_cursor: UUID | None = None
        # Lost planning-task repair (ADR-0020 follow-up).
        self._planning_recovery_last_run = float("-inf")

    def stop(self, signum: int | None = None, frame: object | None = None) -> None:
        logger.info("Scheduler shutdown requested")
        self.stopped.set()

    def run_once(self) -> None:
        logger.info("Scheduler heartbeat")

        # 0. Start campaigns whose scheduled start has arrived, so work that became
        # claimable with them is picked up in this same pass.
        try:
            self._start_due_campaigns()
        except Exception:
            logger.exception("Error starting due campaigns")

        # 1. Discover due work and claim
        try:
            with session_scope(self.settings) as session:
                service = SchedulerService(session, self.settings)
                disc, claimed = service.discover_and_claim_due_work()
                if claimed > 0:
                    logger.info(f"Scheduler claimed {claimed}/{disc} due messages")
        except Exception:
            logger.exception("Error during scheduler due discovery and claim")

        # 2. Publish pending outbox deliveries to Celery queue
        try:
            with session_scope(self.settings) as session:
                publisher = OutboxPublisherService(session, settings=self.settings)
                published = publisher.publish_pending_deliveries()
                if published > 0:
                    logger.info(f"Outbox publisher published {published} deliveries to email.send")
        except Exception:
            logger.exception("Error during outbox publication")

        # 3. Recover expired message claims
        try:
            with session_scope(self.settings) as session:
                service = SchedulerService(session, self.settings)
                recovered = service.recover_expired_claims()
                if recovered > 0:
                    logger.info(f"Scheduler recovered {recovered} expired message claims")
        except Exception:
            logger.exception("Error during expired claim recovery")

        # 4. Recover stale outbox leases
        try:
            with session_scope(self.settings) as session:
                publisher = OutboxPublisherService(session, settings=self.settings)
                recovered_leases = publisher.recover_stale_leases()
                if recovered_leases > 0:
                    logger.info(f"Outbox publisher recovered {recovered_leases} stale leases")
        except Exception:
            logger.exception("Error during stale outbox lease recovery")

        # 5. Recover imports
        try:
            self._recover_imports()
        except Exception:
            logger.exception("Error during import recovery")

        # 6. Recover stale in-flight executions (workers crashed/stuck in SENDING)
        try:
            with session_scope(self.settings) as session:
                from app.modules.sending.recovery_service import RecoveryService

                recovery_service = RecoveryService(session)
                recovered_stale = recovery_service.recover_stale_executions()
                if recovered_stale > 0:
                    logger.info(
                        f"Scheduler recovered {recovered_stale} stale SENDING messages to UNKNOWN_OUTCOME"
                    )
        except Exception:
            logger.exception("Error during stale execution recovery")

        # 7. Reconcile ambiguous message outcomes
        try:
            with session_scope(self.settings) as session:
                from app.modules.sending.reconciliation_service import (
                    ReconciliationService,
                )

                reconciliation_service = ReconciliationService(session)
                reconciled = reconciliation_service.reconcile_unknown_messages()
                if reconciled.get("reconciled_sent", 0) > 0:
                    logger.info(
                        f"Scheduler reconciled {reconciled['reconciled_sent']} messages to SENT"
                    )
        except Exception:
            logger.exception("Error during message outcome reconciliation")

        # 7b. Plan follow-up emails for enrollments whose email was accepted
        try:
            queued = self._dispatch_progression()
            if queued > 0:
                logger.info(f"Scheduler queued progression for {queued} campaigns")
        except Exception:
            logger.exception("Error during follow-up progression dispatch")

        # 7c. Queue just-in-time generation for hyper-personalized messages
        try:
            queued = self._dispatch_personalization()
            if queued > 0:
                logger.info(f"Scheduler queued personalization for {queued} campaigns")
        except Exception:
            logger.exception("Error during personalization dispatch")

        # 7d. Re-publish the post-resume reconcile for campaigns whose pass was
        # lost (ADR-0020). Until it runs their old messages are unclaimable.
        try:
            queued = self._dispatch_reconcile()
            if queued > 0:
                logger.info(f"Scheduler queued resume reconcile for {queued} campaigns")
        except Exception:
            logger.exception("Error during resume reconcile dispatch")

        # 7e. Re-dispatch planning jobs whose task message was lost
        try:
            redispatched = self._dispatch_planning_recovery()
            if redispatched > 0:
                logger.info(f"Scheduler re-dispatched {redispatched} stuck planning job(s)")
        except Exception:
            logger.exception("Error during planning-job recovery")

        # 8. Discover and enqueue due mailbox reply syncs (Phase 13)
        if self.settings.reply_sync_enabled:
            # Nothing is ever dispatched for a mailbox without a sync state, so
            # make sure every connected mailbox has one (idempotent).
            try:
                with session_scope(self.settings) as session:
                    from app.modules.replies.service import ReplySyncService

                    created = ReplySyncService(session).ensure_sync_states()
                    if created > 0:
                        logger.info("Scheduler ensured %d mailbox sync states", created)
            except Exception:
                logger.exception("Error ensuring mailbox sync states")

            try:
                with session_scope(self.settings) as session:
                    from datetime import UTC, datetime

                    from app.modules.replies.repository import ReplyRepository

                    from workers.celery_app import celery_app

                    repo = ReplyRepository(session)
                    now = datetime.now(UTC)
                    claimed_syncs = repo.claim_due_sync_states(
                        lease_owner="scheduler-sync-dispatcher",
                        now=now,
                        limit=20,
                        lease_duration_seconds=self.settings.reply_sync_lease_seconds,
                    )
                    session.commit()

                    if claimed_syncs:
                        logger.info("Scheduler dispatched %d due mailbox sync tasks", len(claimed_syncs))
                        for sync in claimed_syncs:
                            celery_app.send_task(
                                "mailbox.sync",
                                kwargs={
                                    "workspace_id": str(sync["workspace_id"]),
                                    "mailbox_id": str(sync["mailbox_id"]),
                                    # Unique per-claim owner minted by
                                    # claim_due_sync_states; the task takes
                                    # over exactly this lease.
                                    "lease_owner": sync["lease_owner"],
                                },
                                queue="mailbox.sync",
                            )
            except Exception:
                logger.exception("Error during mailbox sync due discovery")

            # 9. Recover stale reply sync leases
            try:
                with session_scope(self.settings) as session:
                    from app.modules.replies.service import ReplySyncService

                    service = ReplySyncService(session)
                    recovered_sync_leases = service.recover_stale_leases()
                    if recovered_sync_leases > 0:
                        logger.info("Scheduler recovered %d stale sync leases", recovered_sync_leases)
            except Exception:
                logger.exception("Error during stale sync lease recovery")

    def _dispatch_progression(self) -> int:
        """Queue a follow-up progression task for each RUNNING campaign (a bounded
        slice per interval). The scheduler role cannot read enrollments across
        tenants, so it only names campaigns; the task does the per-workspace
        work. Returns the number of tasks queued."""
        if not self.settings.sequence_progression_enabled:
            return 0
        now = time.monotonic()
        if now - self._progression_last_run < (
            self.settings.sequence_progression_interval_seconds
        ):
            return 0
        self._progression_last_run = now

        from app.modules.scheduler.repository import SchedulerRepository

        from workers.celery_app import celery_app

        limit = self.settings.sequence_progression_campaigns_per_run
        with session_scope(self.settings) as session:
            campaigns = SchedulerRepository(session).find_running_campaigns(
                after_id=self._progression_cursor, limit=limit
            )
        # Wrap around once the end is reached so every campaign is visited.
        self._progression_cursor = campaigns[-1][1] if len(campaigns) >= limit else None
        for workspace_id, campaign_id in campaigns:
            celery_app.send_task(
                "campaigns.advance_enrollments_chunk",
                kwargs={
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                },
                queue="campaigns",
            )
        return len(campaigns)

    def _dispatch_personalization(self) -> int:
        """Queue a generation task for each RUNNING campaign that has due
        hyper-personalized messages (a bounded slice per interval, wrapping
        around). Read-only: it never generates and never claims messages. The
        task claims work under a lease, so duplicates are harmless."""
        if not self.settings.personalization_enabled:
            return 0
        now = time.monotonic()
        if now - self._personalization_last_run < (
            self.settings.personalization_dispatch_interval_seconds
        ):
            return 0
        self._personalization_last_run = now

        from app.modules.scheduler.repository import SchedulerRepository

        from workers.celery_app import celery_app

        limit = self.settings.personalization_campaigns_per_run
        with session_scope(self.settings) as session:
            campaigns = SchedulerRepository(session).find_campaigns_with_due_generation(
                after_id=self._personalization_cursor, limit=limit
            )
        self._personalization_cursor = (
            campaigns[-1][1] if len(campaigns) >= limit else None
        )
        for workspace_id, campaign_id in campaigns:
            celery_app.send_task(
                "personalization.generate_chunk",
                kwargs={
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                },
                queue="personalization",
            )
        return len(campaigns)

    def _dispatch_planning_recovery(self) -> int:
        """Planning (enrolling an audience, rendering its first emails) is published
        as Celery tasks after the request commits; a lost publish or retry message
        would leave a job PENDING/PROCESSING for ever. Offer every such job to a worker
        again. Safe to repeat: a task claims its job under a lease, and the chunks are
        idempotent. Slow cadence; nothing here is urgent to the second."""
        now = time.monotonic()
        if now - self._planning_recovery_last_run < _PLANNING_RECOVERY_INTERVAL_SECONDS:
            return 0
        self._planning_recovery_last_run = now

        from app.modules.scheduler.repository import SchedulerRepository

        from workers.celery_app import celery_app

        with session_scope(self.settings) as session:
            jobs = SchedulerRepository(session).find_stuck_planning_jobs(
                limit=_PLANNING_RECOVERY_PER_RUN
            )
        for workspace_id, campaign_id, activation_id, audience_id, phase in jobs:
            kwargs = {
                "workspace_id": str(workspace_id),
                "campaign_id": str(campaign_id),
                "activation_id": str(activation_id),
            }
            if audience_id is not None:
                kwargs["audience_id"] = str(audience_id)
            celery_app.send_task(
                "campaigns.enroll_activation_chunk"
                if phase == "ENROLL"
                else "campaigns.render_messages_chunk",
                kwargs=kwargs,
                queue="campaigns",
            )
        return len(jobs)

    def _start_due_campaigns(self) -> int:
        """SCHEDULED -> RUNNING at start_at (campaign state machine "Due start")."""
        with session_scope(self.settings) as session:
            started = SchedulerService(session, self.settings).start_due_campaigns()
        if not started:
            return 0
        from workers.celery_app import celery_app

        for workspace_id, campaign_id, generation in started:
            if generation <= 1:
                continue  # never resumed: nothing is fenced
            # Resumed into SCHEDULED earlier: its pre-pause messages are fenced until
            # the reconcile pass re-validates them, and it did not run (not RUNNING).
            celery_app.send_task(
                "campaigns.reconcile_resume_chunk",
                kwargs={
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "generation": generation,
                },
                queue="campaigns",
            )
        logger.info("Scheduler started %d scheduled campaign(s)", len(started))
        return len(started)

    def _dispatch_reconcile(self) -> int:
        """Queue the reconcile task for each RUNNING campaign that still has
        unsent messages below its schedule generation (a bounded slice, wrapping
        around). Normally a resume dispatches the task itself; this repairs a lost
        publish or a crashed pass. Duplicates are harmless: the task is idempotent
        and a stamped message is never selected again."""
        now = time.monotonic()
        if now - self._reconcile_last_run < _RECONCILE_SWEEP_INTERVAL_SECONDS:
            return 0
        self._reconcile_last_run = now

        from app.modules.scheduler.repository import SchedulerRepository

        from workers.celery_app import celery_app

        limit = _RECONCILE_CAMPAIGNS_PER_RUN
        with session_scope(self.settings) as session:
            campaigns = SchedulerRepository(session).find_campaigns_with_stale_generation(
                after_id=self._reconcile_cursor, limit=limit
            )
        self._reconcile_cursor = campaigns[-1][1] if len(campaigns) >= limit else None
        for workspace_id, campaign_id, generation in campaigns:
            celery_app.send_task(
                "campaigns.reconcile_resume_chunk",
                kwargs={
                    "workspace_id": str(workspace_id),
                    "campaign_id": str(campaign_id),
                    "generation": generation,
                },
                queue="campaigns",
            )
        return len(campaigns)

    def _recover_imports(self) -> None:
        from app.db.session import session_scope
        from sqlalchemy import text

        from workers.celery_app import celery_app

        with session_scope(self.settings) as session:
            session.execute(text("SET ROLE app_scheduler"))
            rows = session.execute(
                text(
                    """
                    SELECT workspace_id, id FROM import_jobs
                    WHERE (status = 'PENDING' AND next_due_at <= pg_catalog.transaction_timestamp())
                       OR (status = 'PROCESSING' AND lease_expires_at <= pg_catalog.transaction_timestamp())
                    """
                )
            ).all()

            if rows:
                logger.info(f"Discovered {len(rows)} due/stuck import(s). Dispatching recovery tasks.")
                for workspace_id, import_id in rows:
                    celery_app.send_task(
                        "imports.process_chunk",
                        kwargs={"workspace_id": str(workspace_id), "import_id": str(import_id)},
                        queue="imports",
                    )

    def run_forever(self) -> None:
        configure_logging(self.settings, service_name="scheduler")
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        logger.info("Scheduler startup")
        while not self.stopped.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Error in scheduler run_once")
            self.stopped.wait(self.settings.scheduler_poll_seconds)
        logger.info("Scheduler shutdown complete")


def main() -> None:
    SchedulerRuntime().run_forever()


if __name__ == "__main__":
    main()
