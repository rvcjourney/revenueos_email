from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from celery.exceptions import Retry

from app.core.config import Settings
from app.core.errors import AppError
from app.db.context import set_transaction_context
from app.db.session import SessionLocal
from app.modules.campaigns.mailbox_assignment import assign_mailbox_for_recipient
from app.modules.campaigns.message_rendering import render_step_content
from app.modules.campaigns.pacing import DuePlanner, MailboxPacing, pacing_from_policies
from app.modules.campaigns.progression import ProgressionService
from app.modules.campaigns.reconcile import ReconcileService
from app.modules.campaigns.reopen import ReopenService
from app.modules.campaigns.retime import RetimeService
from app.modules.campaigns.scheduling import WindowCalendar
from app.modules.campaigns.worker_repository import CampaignWorkerRepository
from app.modules.personalization.version import CAMPAIGN_TYPE_HYPER
from app.modules.suppression.checks import is_address_suppressed
from app.modules.templates.rendering import build_lead_render_context
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Matches workers/imports.py's BATCH_SIZE -- same order of magnitude of
# per-transaction work, no measured reason to diverge.
BATCH_SIZE = 500
_LEASE_TTL_SECONDS = 120


def _classify_member(lead: dict[str, Any]) -> tuple[str, str | None]:
    if lead["lead_status"] == "ARCHIVED":
        return "EXCLUDED", "archived_lead"
    if lead["address_id"] is None:
        return "EXCLUDED", "invalid_address"
    if lead["is_suppressed"]:
        return "EXCLUDED", "suppressed"
    if lead.get("is_enrolled"):
        # Supplement capture only (ADR-0020): already in this campaign.
        return "EXCLUDED", "already_enrolled"
    return "ACCEPTED", None


def _build_due_planner(
    repo: CampaignWorkerRepository,
    ctx: Any,
    workspace_id: UUID,
    campaign_id: UUID,
) -> DuePlanner:
    """The planner that gives each first email its own time in the campaign's
    sending windows, within its mailbox's daily cap."""
    settings = Settings.current()
    default = MailboxPacing(
        daily_cap=settings.mailbox_default_daily_cap,
        min_spacing_seconds=settings.mailbox_default_min_spacing_seconds,
    )
    mailbox_ids = [
        UUID(str(r["mailbox_id"]))
        for r in repo.list_eligible_campaign_mailboxes(
            workspace_id=workspace_id, campaign_id=campaign_id
        )
    ]
    policies = repo.list_mailbox_limit_policies(
        workspace_id=workspace_id, mailbox_ids=mailbox_ids
    )
    return DuePlanner(
        WindowCalendar(
            ctx["start_at"],
            timezone=ctx["timezone"],
            weekday_set=ctx["weekday_set"],
            window_start_local=ctx["window_start_local"],
            window_end_local=ctx["window_end_local"],
        ),
        mailbox_count=len(mailbox_ids),
        campaign_daily_limit=ctx["daily_limit"],
        pacing_by_mailbox={
            mailbox_id: pacing_from_policies(policies.get(mailbox_id, []), default)
            for mailbox_id in mailbox_ids
        },
        default=default,
    )


@celery_app.task(
    name="campaigns.capture_audience_chunk",
    queue="campaigns",
    bind=True,
    max_retries=None,
)
def capture_audience_chunk(
    self: Any,
    workspace_id: str,
    campaign_id: str,
    audience_id: str,
    abandon: bool = False,
) -> None:
    """One bounded chunk of audience capture, claimed via a lease so a lost/
    duplicate delivery is safe to redeliver.

    Transactions are demarcated with explicit session.commit()/rollback()
    rather than `with session.begin():` -- SQLAlchemy 2.0's Session
    autobegins a transaction on the very first statement (the lease claim
    below), and a later `session.begin()` on top of that raises
    "A transaction is already begun on this Session" rather than nesting.
    Each commit() closes out the current (auto-begun) transaction so the
    next statement can autobegin a fresh one -- but set_transaction_context()
    uses SET LOCAL (transaction-scoped GUCs, see app/db/context.py), so
    app.workspace_id is wiped out by every commit()/rollback() too and must
    be re-applied before the next statement that needs RLS context.
    """
    Settings.current()
    lease_owner = self.request.id
    ws_uuid = UUID(workspace_id)

    with SessionLocal() as session:

        def _ctx() -> None:
            set_transaction_context(session, workspace_id=ws_uuid)

        _ctx()
        repo = CampaignWorkerRepository(session)
        job: dict[str, Any] | None = None

        try:
            job = repo.claim_capture_job(
                workspace_id=UUID(workspace_id),
                audience_id=UUID(audience_id),
                lease_owner=lease_owner,
                ttl_seconds=_LEASE_TTL_SECONDS,
            )
            if job is None:
                logger.info(
                    f"Capture job for audience {audience_id} not claimable "
                    "or already terminal/claimed."
                )
                session.rollback()
                return
            session.commit()  # persist the lease claim durably before any further work
            _ctx()

            if abandon:
                repo.abandon_capture(
                    workspace_id=UUID(workspace_id),
                    campaign_id=UUID(campaign_id),
                    audience_id=UUID(audience_id),
                    job_id=UUID(str(job["id"])),
                )
                session.commit()
                return

            audience = repo.get_audience(
                workspace_id=UUID(workspace_id), audience_id=UUID(audience_id)
            )
            if audience is None:
                raise AppError(
                    "not_found", "Audience revision not found", status_code=404
                )

            manifest = audience["selection_manifest"] or {}
            cursor = json.loads(job["cursor_data"]) if job["cursor_data"] else None
            if cursor is None:
                candidate_ids = repo.resolve_candidate_lead_ids(
                    workspace_id=UUID(workspace_id),
                    list_ids=manifest.get("lists", []),
                    lead_ids=manifest.get("leads", []),
                )
                cursor = {"candidate_ids": candidate_ids, "offset": 0}

            candidate_ids: list[str] = cursor["candidate_ids"]
            offset: int = cursor["offset"]
            batch_ids = candidate_ids[offset : offset + BATCH_SIZE]

            if batch_ids:
                lead_rows = repo.fetch_lead_capture_rows(
                    workspace_id=UUID(workspace_id),
                    lead_ids=batch_ids,
                    campaign_id=(
                        UUID(campaign_id)
                        if audience.get("kind") == "SUPPLEMENT"
                        else None
                    ),
                )
                lead_by_id = {str(row["lead_id"]): dict(row) for row in lead_rows}
                members: list[dict[str, Any]] = []
                for i, lead_id in enumerate(batch_ids):
                    lead = lead_by_id.get(lead_id)
                    ordinal = offset + i + 1
                    if lead is None:
                        # Referenced by the manifest but not resolvable under
                        # this workspace's RLS scope any more (e.g. deleted
                        # between selection and capture).
                        members.append(
                            {
                                "lead_id": lead_id,
                                "address_id": None,
                                "capture_ordinal": ordinal,
                                "contact_revision": 1,
                                "frozen_variables": {},
                                "eligibility_status": "EXCLUDED",
                                "exclusion_reason": "invalid_address",
                            }
                        )
                        continue
                    eligibility, reason = _classify_member(lead)
                    frozen_variables = (
                        build_lead_render_context(lead)
                        if eligibility == "ACCEPTED"
                        else {}
                    )
                    members.append(
                        {
                            "lead_id": lead_id,
                            "address_id": (
                                str(lead["address_id"]) if lead["address_id"] else None
                            ),
                            "capture_ordinal": ordinal,
                            "contact_revision": lead["contact_revision"],
                            "frozen_variables": frozen_variables,
                            "eligibility_status": eligibility,
                            "exclusion_reason": reason,
                        }
                    )
                # A member without a resolvable address cannot be inserted
                # (address_id is NOT NULL) -- skip it entirely; it is
                # already excluded from send eligibility by construction and
                # is reflected only in the processed/total counters.
                insertable = [m for m in members if m["address_id"] is not None]
                repo.insert_audience_members(
                    workspace_id=UUID(workspace_id),
                    campaign_id=UUID(campaign_id),
                    audience_id=UUID(audience_id),
                    members=insertable,
                )

            new_offset = offset + len(batch_ids)
            finished = len(batch_ids) == 0 or new_offset >= len(candidate_ids)

            if finished:
                digest = hashlib.sha256(
                    json.dumps(manifest, sort_keys=True).encode("utf-8")
                ).hexdigest()
                repo.complete_capture(
                    workspace_id=UUID(workspace_id),
                    campaign_id=UUID(campaign_id),
                    audience_id=UUID(audience_id),
                    job_id=UUID(str(job["id"])),
                    processed_count=new_offset,
                    source_manifest_digest=digest,
                )
            else:
                repo.update_job_progress(
                    workspace_id=UUID(workspace_id),
                    job_id=UUID(str(job["id"])),
                    cursor_data=json.dumps(
                        {"candidate_ids": candidate_ids, "offset": new_offset}
                    ),
                    processed_count=new_offset,
                    lease_owner=lease_owner,
                    ttl_seconds=_LEASE_TTL_SECONDS,
                )
            session.commit()

            if not finished:
                logger.info(
                    f"Audience {audience_id} capture chunk processed "
                    f"({new_offset}/{len(candidate_ids)}). Re-queuing."
                )
                self.retry(countdown=1, max_retries=self.request.retries + 1)
            else:
                logger.info(f"Audience {audience_id} capture completed.")

        except Retry:
            # Celery's self.retry() raises this to schedule redelivery -- it
            # must propagate untouched, never be treated as a capture failure.
            raise
        except AppError as exc:
            logger.error(f"App error capturing audience {audience_id}: {exc.message}")
            session.rollback()
            _ctx()
            if job is not None:
                repo.fail_capture(
                    workspace_id=UUID(workspace_id),
                    campaign_id=UUID(campaign_id),
                    audience_id=UUID(audience_id),
                    job_id=UUID(str(job["id"])),
                    error_reason=exc.message,
                )
                session.commit()
        except Exception:
            logger.exception(f"Unexpected error capturing audience {audience_id}")
            session.rollback()
            _ctx()
            if job is not None:
                repo.fail_capture(
                    workspace_id=UUID(workspace_id),
                    campaign_id=UUID(campaign_id),
                    audience_id=UUID(audience_id),
                    job_id=UUID(str(job["id"])),
                    error_reason="An unexpected error occurred during audience capture",
                )
                session.commit()


def _dispatch_render_task(
    *,
    workspace_id: str,
    campaign_id: str,
    activation_id: str,
    audience_id: str | None = None,
) -> None:
    kwargs = {
        "workspace_id": workspace_id,
        "campaign_id": campaign_id,
        "activation_id": activation_id,
    }
    if audience_id is not None:
        kwargs["audience_id"] = audience_id
    celery_app.send_task(
        "campaigns.render_messages_chunk", kwargs=kwargs, queue="campaigns"
    )


@celery_app.task(
    name="campaigns.enroll_activation_chunk",
    queue="campaigns",
    bind=True,
    max_retries=None,
)
def enroll_activation_chunk(
    self: Any,
    workspace_id: str,
    campaign_id: str,
    activation_id: str,
    audience_id: str | None = None,
) -> None:
    """One bounded chunk of enrollment creation for a campaign activation.

    With `audience_id` it enrolls a SUPPLEMENT audience of an already-running
    activation instead (ADR-0020): same chunking, suppression re-check and unique
    keys, but the campaign's planning_status is left alone and mailbox assignment
    continues after the leads already enrolled.

    Converts campaign_audience_members (accepted, not currently suppressed)
    into campaign_enrollments plus their first PLANNED message, using a
    keyset cursor on capture_ordinal. See capture_audience_chunk's docstring
    for the transaction/GUC discipline this mirrors.
    """
    Settings.current()
    lease_owner = self.request.id
    ws_uuid = UUID(workspace_id)
    campaign_uuid = UUID(campaign_id)
    activation_uuid = UUID(activation_id)
    supplement_uuid = UUID(audience_id) if audience_id else None

    with SessionLocal() as session:

        def _ctx() -> None:
            set_transaction_context(session, workspace_id=ws_uuid)

        _ctx()
        repo = CampaignWorkerRepository(session)
        job: dict[str, Any] | None = None

        try:
            job = repo.claim_planning_job(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                activation_id=activation_uuid,
                phase="ENROLL",
                lease_owner=lease_owner,
                ttl_seconds=_LEASE_TTL_SECONDS,
                audience_id=supplement_uuid,
            )
            if job is None:
                logger.info(
                    f"ENROLL job for campaign {campaign_id} activation "
                    f"{activation_id} not claimable or already terminal/claimed."
                )
                session.rollback()
                return
            session.commit()  # persist the lease claim durably before any further work
            _ctx()

            ctx = repo.get_activation_context(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                activation_id=activation_uuid,
            )
            if ctx is None:
                logger.info(
                    f"Campaign {campaign_id}'s current activation no longer "
                    f"matches {activation_id}; abandoning superseded ENROLL work."
                )
                session.rollback()
                return

            member_audience_id = supplement_uuid or UUID(
                str(ctx["activated_audience_id"])
            )
            sequence_id = UUID(str(ctx["activated_sequence_id"]))
            first_step_id = UUID(str(ctx["first_step_id"]))

            eligible_mailbox_ids = [
                UUID(str(row["mailbox_id"]))
                for row in repo.list_eligible_campaign_mailboxes(
                    workspace_id=ws_uuid, campaign_id=campaign_uuid
                )
            ]

            cursor = (
                json.loads(job["cursor_data"])
                if job["cursor_data"]
                else {"after_ordinal": 0}
            )
            if supplement_uuid is not None and "ordinal_offset" not in cursor:
                # Continue the round-robin after the leads already enrolled, and
                # remember it so a retried chunk assigns the same mailboxes.
                cursor["ordinal_offset"] = repo.count_campaign_enrollments(
                    workspace_id=ws_uuid, campaign_id=campaign_uuid
                )
            ordinal_offset = int(cursor.get("ordinal_offset", 0))
            members = repo.fetch_accepted_audience_members_chunk(
                workspace_id=ws_uuid,
                audience_id=member_audience_id,
                after_ordinal=cursor["after_ordinal"],
                limit=BATCH_SIZE,
            )

            enrollable_rows: list[dict[str, Any]] = []
            for member in members:
                address_id = UUID(str(member["address_id"]))
                if is_address_suppressed(session, ws_uuid, address_id):
                    # Re-checked here because suppression can change between
                    # audience capture and activation; this is a courtesy
                    # skip, not the authoritative gate -- the future send
                    # worker re-checks suppression again at send time
                    # regardless of what happens here.
                    continue
                mailbox_id = assign_mailbox_for_recipient(
                    eligible_mailbox_ids,
                    int(member["capture_ordinal"]) + ordinal_offset,
                )
                enrollable_rows.append(
                    {
                        "audience_member_id": str(member["audience_member_id"]),
                        "lead_id": str(member["lead_id"]),
                        "address_id": str(address_id),
                        "frozen_destination": member["canonical_address"],
                        "frozen_variables": member["frozen_variables"],
                        "assigned_mailbox_id": str(mailbox_id),
                    }
                )

            repo.insert_enrollments(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                audience_id=member_audience_id,
                sequence_id=sequence_id,
                first_step_id=first_step_id,
                rows=enrollable_rows,
            )

            if enrollable_rows:
                lead_ids = [row["lead_id"] for row in enrollable_rows]
                enrollments = repo.get_enrollments_for_leads(
                    workspace_id=ws_uuid, campaign_id=campaign_uuid, lead_ids=lead_ids
                )
                message_rows = [
                    {
                        "enrollment_id": str(enrollment["id"]),
                        "mailbox_id": str(enrollment["assigned_mailbox_id"]),
                        "address_id": str(enrollment["address_id"]),
                    }
                    for enrollment in enrollments
                ]
                repo.insert_planned_first_messages(
                    workspace_id=ws_uuid,
                    campaign_id=campaign_uuid,
                    sequence_id=sequence_id,
                    step_id=first_step_id,
                    rows=message_rows,
                )

            new_after_ordinal = (
                max(int(m["capture_ordinal"]) for m in members)
                if members
                else cursor["after_ordinal"]
            )
            finished = len(members) < BATCH_SIZE

            if finished:
                repo.complete_enroll_job(
                    workspace_id=ws_uuid,
                    campaign_id=campaign_uuid,
                    job_id=UUID(str(job["id"])),
                    processed_count=new_after_ordinal,
                    mark_planning_ready=supplement_uuid is None,
                )
            else:
                repo.update_job_progress(
                    workspace_id=ws_uuid,
                    job_id=UUID(str(job["id"])),
                    cursor_data=json.dumps(
                        {
                            "after_ordinal": new_after_ordinal,
                            **(
                                {"ordinal_offset": ordinal_offset}
                                if supplement_uuid is not None
                                else {}
                            ),
                        }
                    ),
                    processed_count=new_after_ordinal,
                    lease_owner=lease_owner,
                    ttl_seconds=_LEASE_TTL_SECONDS,
                )
            session.commit()

            if not finished:
                logger.info(
                    f"Campaign {campaign_id} activation {activation_id} ENROLL "
                    f"chunk processed (cursor={new_after_ordinal}). Re-queuing."
                )
                self.retry(countdown=1, max_retries=self.request.retries + 1)
            else:
                logger.info(
                    f"Campaign {campaign_id} activation {activation_id} ENROLL "
                    "completed."
                )
                _dispatch_render_task(
                    workspace_id=workspace_id,
                    campaign_id=campaign_id,
                    activation_id=activation_id,
                    audience_id=audience_id,
                )

        except Retry:
            raise
        except AppError as exc:
            logger.error(
                f"App error enrolling campaign {campaign_id} activation "
                f"{activation_id}: {exc.message}"
            )
            session.rollback()
            _ctx()
            if job is not None:
                repo.fail_planning_job(
                    workspace_id=ws_uuid,
                    job_id=UUID(str(job["id"])),
                    error_reason=exc.message,
                )
                session.commit()
        except Exception:
            logger.exception(
                f"Unexpected error enrolling campaign {campaign_id} activation "
                f"{activation_id}"
            )
            session.rollback()
            _ctx()
            if job is not None:
                repo.fail_planning_job(
                    workspace_id=ws_uuid,
                    job_id=UUID(str(job["id"])),
                    error_reason="An unexpected error occurred during enrollment",
                )
                session.commit()


@celery_app.task(
    name="campaigns.render_messages_chunk",
    queue="campaigns",
    bind=True,
    max_retries=None,
)
def render_messages_chunk(
    self: Any,
    workspace_id: str,
    campaign_id: str,
    activation_id: str,
    audience_id: str | None = None,
) -> None:
    """One bounded chunk of first-message content rendering + schedule
    calculation for a campaign activation. Rows leave PLANNED as they are
    rendered (no separate cursor needed), and FOR UPDATE ... SKIP LOCKED lets
    concurrent invocations safely split the remaining work.
    """
    Settings.current()
    lease_owner = self.request.id
    ws_uuid = UUID(workspace_id)
    campaign_uuid = UUID(campaign_id)
    activation_uuid = UUID(activation_id)
    supplement_uuid = UUID(audience_id) if audience_id else None

    with SessionLocal() as session:

        def _ctx() -> None:
            set_transaction_context(session, workspace_id=ws_uuid)

        _ctx()
        repo = CampaignWorkerRepository(session)
        job: dict[str, Any] | None = None

        try:
            if not repo.enroll_job_is_ready(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                activation_id=activation_uuid,
                audience_id=supplement_uuid,
            ):
                # RENDER renders every PLANNED message of the campaign: running it
                # before ENROLL has created them all would finish early and strand the
                # rest. ENROLL dispatches RENDER itself when it completes.
                logger.info(
                    f"RENDER for campaign {campaign_id} waits for its ENROLL job; "
                    "not claiming."
                )
                session.rollback()
                return
            job = repo.claim_planning_job(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                activation_id=activation_uuid,
                phase="RENDER",
                lease_owner=lease_owner,
                ttl_seconds=_LEASE_TTL_SECONDS,
                audience_id=supplement_uuid,
            )
            if job is None:
                logger.info(
                    f"RENDER job for campaign {campaign_id} activation "
                    f"{activation_id} not claimable or already terminal/claimed."
                )
                session.rollback()
                return
            session.commit()
            _ctx()

            ctx = repo.get_activation_context(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                activation_id=activation_uuid,
            )
            if ctx is None:
                logger.info(
                    f"Campaign {campaign_id}'s current activation no longer "
                    f"matches {activation_id}; abandoning superseded RENDER work."
                )
                session.rollback()
                return

            if supplement_uuid is not None:
                # Late joiners are scheduled from now, not from a start in the
                # past; the post-resume reconcile spreads whatever is overdue.
                ctx = {**ctx, "start_at": max(ctx["start_at"], datetime.now(UTC))}
            hyper = ctx["campaign_type"] == CAMPAIGN_TYPE_HYPER
            rows = repo.fetch_planned_messages_chunk(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                limit=BATCH_SIZE,
                hyper=hyper,
            )
            personalization = Settings.current() if hyper else None
            planner = _build_due_planner(repo, ctx, ws_uuid, campaign_uuid)

            for row in rows:
                # Each message gets its own time, so a mailbox is never asked
                # for more than its daily cap (see campaigns/pacing.py).
                due_at = planner.due_at(
                    mailbox_id=UUID(str(row["mailbox_id"])),
                    capture_ordinal=int(row["capture_ordinal"]),
                    message_id=UUID(str(row["id"])),
                )
                if personalization is not None:
                    # Hyper-personalized (ADR-0011): the message stays PLANNED
                    # with its intended time; the personalization worker writes
                    # the content snapshot one lead time before it is due.
                    repo.mark_generation_pending(
                        workspace_id=ws_uuid,
                        campaign_id=campaign_uuid,
                        sequence_id=UUID(str(row["sequence_id"])),
                        step_id=UUID(str(row["step_id"])),
                        enrollment_id=UUID(str(row["enrollment_id"])),
                        message_id=UUID(str(row["id"])),
                        due_at=due_at,
                        anchor_at=ctx["start_at"],
                        next_attempt_at=due_at
                        - timedelta(
                            seconds=personalization.personalization_lead_time_seconds
                        ),
                        max_attempts=personalization.personalization_max_attempts,
                    )
                    continue
                rendered = render_step_content(
                    subject=row["email_subject"],
                    body_html=row["email_body_html"],
                    preheader=row["email_preheader"],
                    frozen_variables=row["frozen_variables"] or {},
                    renderer_version=1,
                )
                repo.render_message(
                    workspace_id=ws_uuid,
                    message_id=UUID(str(row["id"])),
                    content_subject=rendered.subject,
                    content_body_html=rendered.body_html,
                    content_digest=rendered.content_digest,
                    frozen_destination=row["frozen_destination"],
                    frozen_sender_address=row["sender_address"],
                    frozen_sender_name=row["sender_name"],
                    due_at=due_at,
                    anchor_at=ctx["start_at"],
                )

            # processed_count and the row status flips above commit in the
            # same transaction, so a blind += is safe here (unlike a
            # naively-retried counter): a crash before commit replays this
            # exact chunk unchanged, and FOR UPDATE ... SKIP LOCKED already
            # guarantees no two concurrent invocations touch the same row.
            finished = len(rows) < BATCH_SIZE
            new_processed_count = int(job["processed_count"]) + len(rows)

            if finished:
                repo.complete_render_job(
                    workspace_id=ws_uuid,
                    job_id=UUID(str(job["id"])),
                    processed_count=new_processed_count,
                )
            else:
                repo.update_job_progress(
                    workspace_id=ws_uuid,
                    job_id=UUID(str(job["id"])),
                    cursor_data=job["cursor_data"] or "{}",
                    processed_count=new_processed_count,
                    lease_owner=lease_owner,
                    ttl_seconds=_LEASE_TTL_SECONDS,
                )
            session.commit()

            if not finished:
                logger.info(
                    f"Campaign {campaign_id} activation {activation_id} RENDER "
                    f"chunk processed ({new_processed_count}). Re-queuing."
                )
                self.retry(countdown=1, max_retries=self.request.retries + 1)
            else:
                logger.info(
                    f"Campaign {campaign_id} activation {activation_id} RENDER "
                    "completed."
                )

        except Retry:
            raise
        except AppError as exc:
            logger.error(
                f"App error rendering campaign {campaign_id} activation "
                f"{activation_id}: {exc.message}"
            )
            session.rollback()
            _ctx()
            if job is not None:
                repo.fail_planning_job(
                    workspace_id=ws_uuid,
                    job_id=UUID(str(job["id"])),
                    error_reason=exc.message,
                )
                session.commit()
        except Exception:
            logger.exception(
                f"Unexpected error rendering campaign {campaign_id} activation "
                f"{activation_id}"
            )
            session.rollback()
            _ctx()
            if job is not None:
                repo.fail_planning_job(
                    workspace_id=ws_uuid,
                    job_id=UUID(str(job["id"])),
                    error_reason="An unexpected error occurred during rendering",
                )
                session.commit()


@celery_app.task(
    name="campaigns.advance_enrollments_chunk",
    queue="campaigns",
    bind=True,
    max_retries=None,
)
def advance_enrollments_chunk(
    self: Any,
    workspace_id: str,
    campaign_id: str,
    after_enrollment_id: str | None = None,
) -> None:
    """One bounded chunk of follow-up progression for a RUNNING campaign.

    Builds the next email for every enrollment whose current email was accepted
    by the provider (see app.modules.campaigns.progression). Safe to repeat:
    the whole chunk is one transaction, so a crash replays it unchanged, and the
    unique (enrollment, step) message key stops two workers double-planning.
    A full chunk re-queues itself from the last enrollment it looked at.
    """
    settings = Settings.current()
    if not settings.sequence_progression_enabled:
        return
    ws_uuid = UUID(workspace_id)
    campaign_uuid = UUID(campaign_id)
    limit = settings.sequence_progression_batch_size

    with SessionLocal() as session:
        set_transaction_context(session, workspace_id=ws_uuid)
        repo = CampaignWorkerRepository(session)
        service = ProgressionService(
            repo,
            is_suppressed=lambda ws, address_id: is_address_suppressed(
                session, ws, address_id
            ),
            personalization_lead_seconds=settings.personalization_lead_time_seconds,
            personalization_max_attempts=settings.personalization_max_attempts,
            default_pacing=MailboxPacing(
                daily_cap=settings.mailbox_default_daily_cap,
                min_spacing_seconds=settings.mailbox_default_min_spacing_seconds,
            ),
            overdue_grace=timedelta(
                seconds=2 * settings.sequence_progression_interval_seconds
            ),
        )
        try:
            summary = service.advance_batch(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                limit=limit,
                after_id=UUID(after_enrollment_id) if after_enrollment_id else None,
            )
            session.commit()
        except Exception:
            logger.exception(
                f"Unexpected error advancing enrollments for campaign {campaign_id}"
            )
            session.rollback()
            return

        if summary.fetched:
            # Counts and reason codes only: never recipient data or content.
            logger.info(
                f"Campaign {campaign_id} progression: fetched={summary.fetched} "
                f"advanced={summary.advanced} completed={summary.completed} "
                f"failed={summary.failed} skipped={summary.skipped} "
                f"reasons={summary.reasons}"
            )
        if summary.fetched >= limit and summary.last_enrollment_id is not None:
            self.retry(
                kwargs={
                    "workspace_id": workspace_id,
                    "campaign_id": campaign_id,
                    "after_enrollment_id": str(summary.last_enrollment_id),
                },
                countdown=1,
                max_retries=self.request.retries + 1,
            )


@celery_app.task(
    name="campaigns.reopen_completed_enrollments_chunk",
    queue="campaigns",
    bind=True,
    max_retries=None,
)
def reopen_completed_enrollments_chunk(
    self: Any,
    workspace_id: str,
    campaign_id: str,
    generation: int,
    after_enrollment_id: str | None = None,
) -> None:
    """One bounded chunk of the opt-in reopen of finished leads (ADR-0020).

    Bound to the resume `generation` that asked for it; does nothing once the campaign
    is no longer RUNNING at that generation. Safe to repeat. A full chunk re-queues
    itself from the last enrollment it looked at; the progression sweep then plans the
    appended email for each reopened lead."""
    settings = Settings.current()
    ws_uuid = UUID(workspace_id)
    campaign_uuid = UUID(campaign_id)
    limit = settings.sequence_progression_batch_size

    with SessionLocal() as session:
        set_transaction_context(session, workspace_id=ws_uuid)
        repo = CampaignWorkerRepository(session)
        service = ReopenService(
            repo,
            is_suppressed=lambda ws, address_id: is_address_suppressed(
                session, ws, address_id
            ),
        )
        try:
            summary = service.reopen_batch(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                generation=generation,
                limit=limit,
                after_id=UUID(after_enrollment_id) if after_enrollment_id else None,
            )
            session.commit()
        except Exception:
            logger.exception(
                f"Unexpected error reopening finished leads for campaign {campaign_id}"
            )
            session.rollback()
            return

        if summary.campaign_not_eligible:
            logger.info(
                f"Reopen for campaign {campaign_id} generation {generation} skipped: "
                "campaign is not running at that generation."
            )
            return
        # Counts only: never recipient data.
        logger.info(
            "CAMPAIGN_REOPEN_COMPLETED workspace=%s campaign=%s fetched=%d "
            "reopened=%d suppressed=%d",
            workspace_id,
            campaign_id,
            summary.fetched,
            summary.reopened,
            summary.skipped_suppressed,
        )
        if summary.fetched >= limit and summary.last_enrollment_id is not None:
            self.retry(
                kwargs={
                    "workspace_id": workspace_id,
                    "campaign_id": campaign_id,
                    "generation": generation,
                    "after_enrollment_id": str(summary.last_enrollment_id),
                },
                countdown=1,
                max_retries=self.request.retries + 1,
            )


def _retime_phase(
    task: Any,
    *,
    workspace_id: str,
    campaign_id: str,
    generation: int,
    after_id: str | None,
) -> None:
    """Second phase of the post-resume pass (ADR-0020 amendment 2): move the
    personalized follow-ups that are planned but not yet written to the time their
    waits imply now. A no-op for a standard campaign and for a task that has been
    superseded. A full batch re-queues itself from the last job it looked at."""
    settings = Settings.current()
    ws_uuid = UUID(workspace_id)
    limit = BATCH_SIZE
    with SessionLocal() as session:
        set_transaction_context(session, workspace_id=ws_uuid)
        service = RetimeService(
            CampaignWorkerRepository(session),
            lead_seconds=settings.personalization_lead_time_seconds,
        )
        try:
            summary = service.retime_batch(
                workspace_id=ws_uuid,
                campaign_id=UUID(campaign_id),
                generation=generation,
                limit=limit,
                after_id=UUID(after_id) if after_id else None,
            )
            session.commit()
        except Exception:
            logger.exception(
                "CAMPAIGN_RETIME_FAILED workspace=%s campaign=%s generation=%s",
                workspace_id,
                campaign_id,
                generation,
            )
            session.rollback()
            return
    if not summary.active or not summary.fetched:
        return
    # Counts only: never recipient data or content.
    logger.info(
        "CAMPAIGN_FOLLOWUPS_RETIMED workspace=%s campaign=%s generation=%s "
        "fetched=%d retimed=%d unchanged=%d",
        workspace_id,
        campaign_id,
        generation,
        summary.fetched,
        summary.retimed,
        summary.unchanged,
    )
    if summary.fetched >= limit and summary.last_id is not None:
        task.retry(
            kwargs={
                "workspace_id": workspace_id,
                "campaign_id": campaign_id,
                "generation": generation,
                "phase": "RETIME",
                "retime_after_id": str(summary.last_id),
            },
            countdown=1,
            max_retries=task.request.retries + 1,
        )


@celery_app.task(
    name="campaigns.reconcile_resume_chunk",
    queue="campaigns",
    bind=True,
    max_retries=None,
)
def reconcile_resume_chunk(
    self: Any,
    workspace_id: str,
    campaign_id: str,
    generation: int,
    after_due_at: str | None = None,
    after_id: str | None = None,
    offsets: dict[str, int] | None = None,
    phase: str = "RECONCILE",
    retime_after_id: str | None = None,
) -> None:
    """One bounded chunk of the post-resume reconcile pass (ADR-0020).

    Re-times and re-stamps the campaign's unsent messages that carry an older
    schedule generation, so the scheduler can claim them again. Safe to repeat
    or run twice: a message at the current generation is never selected, and the
    task does nothing once the campaign is no longer RUNNING at `generation`
    (a later pause or resume supersedes it). A full chunk re-queues itself from
    the last message it looked at, carrying the per-mailbox slot counters.

    When the reconcile of scheduled messages is finished, a personalized campaign's
    planned follow-ups are re-timed from the waits as they stand now (`phase="RETIME"`,
    see `_retime_phase`); tasks queued before that phase existed default to RECONCILE.
    """
    if phase == "RETIME":
        _retime_phase(
            self,
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            generation=generation,
            after_id=retime_after_id,
        )
        return
    settings = Settings.current()
    ws_uuid = UUID(workspace_id)
    campaign_uuid = UUID(campaign_id)
    limit = BATCH_SIZE
    start_retime = False

    with SessionLocal() as session:
        set_transaction_context(session, workspace_id=ws_uuid)
        repo = CampaignWorkerRepository(session)
        service = ReconcileService(
            repo,
            default_pacing=MailboxPacing(
                daily_cap=settings.mailbox_default_daily_cap,
                min_spacing_seconds=settings.mailbox_default_min_spacing_seconds,
            ),
        )
        try:
            summary = service.reconcile_batch(
                workspace_id=ws_uuid,
                campaign_id=campaign_uuid,
                generation=generation,
                limit=limit,
                after_due_at=(
                    datetime.fromisoformat(after_due_at) if after_due_at else None
                ),
                after_id=UUID(after_id) if after_id else None,
                offsets=offsets,
            )
            session.commit()
        except Exception:
            logger.exception(
                "CAMPAIGN_RESUME_FAILED workspace=%s campaign=%s generation=%s "
                "reason=reconcile_error",
                workspace_id,
                campaign_id,
                generation,
            )
            session.rollback()
            return

        if not summary.active:
            logger.info(
                "Reconcile for campaign %s generation %s superseded; nothing to do",
                campaign_id,
                generation,
            )
            return
        if summary.fetched:
            # Counts and reason codes only: never recipient data or content.
            logger.info(
                "CAMPAIGN_STEP_RESUMED workspace=%s campaign=%s generation=%s "
                "fetched=%d restamped=%d rerendered=%d spread=%d kept_future=%d "
                "skipped=%d reasons=%s",
                workspace_id,
                campaign_id,
                generation,
                summary.fetched,
                summary.restamped,
                summary.rerendered,
                summary.spread,
                summary.kept_future,
                summary.skipped,
                summary.reasons,
            )
        if summary.fetched >= limit and summary.last_id is not None:
            self.retry(
                kwargs={
                    "workspace_id": workspace_id,
                    "campaign_id": campaign_id,
                    "generation": generation,
                    "after_due_at": (
                        summary.last_due_at.isoformat() if summary.last_due_at else None
                    ),
                    "after_id": str(summary.last_id),
                    "offsets": summary.offsets,
                },
                countdown=1,
                max_retries=self.request.retries + 1,
            )
        else:
            logger.info(
                "CAMPAIGN_STEP_COMPLETED workspace=%s campaign=%s generation=%s",
                workspace_id,
                campaign_id,
                generation,
            )
            start_retime = True
    if start_retime:
        _retime_phase(
            self,
            workspace_id=workspace_id,
            campaign_id=campaign_id,
            generation=generation,
            after_id=None,
        )


@celery_app.task(name="campaigns.retry_planning_job", queue="campaigns")
def retry_planning_job(
    workspace_id: str,
    campaign_id: str,
    activation_id: str,
    audience_id: str | None = None,
) -> None:
    """Put a FAILED ENROLL/RENDER pair back to PENDING (cursor kept) and run it again.

    The API cannot do this itself (it has no UPDATE on the planning tables), so the
    command dispatches this task, like abandon_audience does. Idempotent: a repeat
    finds nothing FAILED and does nothing; the chunks it re-runs are idempotent.
    """
    ws_uuid = UUID(workspace_id)
    campaign_uuid = UUID(campaign_id)
    activation_uuid = UUID(activation_id)
    supplement_uuid = UUID(audience_id) if audience_id else None

    with SessionLocal() as session:
        set_transaction_context(session, workspace_id=ws_uuid)
        repo = CampaignWorkerRepository(session)
        phases = repo.reset_failed_planning_jobs(
            workspace_id=ws_uuid,
            campaign_id=campaign_uuid,
            activation_id=activation_uuid,
            audience_id=supplement_uuid,
        )
        session.commit()
        if not phases:
            logger.info(
                f"retry_planning_job: nothing FAILED for campaign {campaign_id}; no-op"
            )
            return

    logger.info(
        "CAMPAIGN_PLANNING_RETRY workspace=%s campaign=%s phases=%s",
        workspace_id,
        campaign_id,
        ",".join(phases),
    )
    kwargs = {
        "workspace_id": workspace_id,
        "campaign_id": campaign_id,
        "activation_id": activation_id,
    }
    if audience_id is not None:
        kwargs["audience_id"] = audience_id
    # ENROLL first; it dispatches RENDER when it completes. If only RENDER failed,
    # its ENROLL is READY and RENDER can run now.
    celery_app.send_task(
        "campaigns.enroll_activation_chunk"
        if "ENROLL" in phases
        else "campaigns.render_messages_chunk",
        kwargs=kwargs,
        queue="campaigns",
    )
