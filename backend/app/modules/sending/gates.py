from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.metrics import record_message_blocked_at_send_due_to_suppression
from app.modules.sending.schemas import LoadedSendContext
from app.modules.suppression.checks import is_address_suppressed, is_platform_suppressed

# Mirrors the pattern in backend/app/modules/mailboxes/service.py's
# _EMAIL_PATTERN (kept as a local, deliberately duplicated constant rather
# than importing a private module-level name across modules, per CLAUDE.md's
# "keep changes focused" -- not a shared utility extraction in this phase).
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MESSAGE_RESOLVED_STATES = frozenset(
    {"SENT", "FAILED", "SKIPPED", "CANCELLED", "UNKNOWN_OUTCOME"}
)
MESSAGE_PROCEED_ELIGIBLE_STATES = frozenset({"QUEUED", "SENDING"})


class SendGateRejected(Exception):
    """Base class for pre-send gate rejections. Each gate raises the most
    specific subclass. Raising (rather than returning a bool) makes a
    forgotten check impossible to silently ignore: SendingService always
    ends up in an except-branch that persists a terminal/skip outcome and
    guarantees provider.send_message was never reached.
    """

    def __init__(self, reason: str, *, terminal_reason: str) -> None:
        self.reason = reason
        # Value persisted to messages.terminal_reason / hold_reason -- kept
        # short and free of PII per the audit/logging requirements.
        self.terminal_reason = terminal_reason
        super().__init__(reason)


class MessageAlreadyResolved(SendGateRejected):
    """Not a failure: the message has already reached a terminal state.
    Duplicate/redelivered task must become a safe no-op, never a resend."""


class StaleDispatchGeneration(SendGateRejected):
    """The task payload's dispatch_generation no longer matches the
    message's current generation -- a newer claim/generation has already
    superseded this task. Safe no-op; the current generation's task (if
    any) owns this message now."""


class MessageStateRejected(SendGateRejected):
    pass


class CampaignStateRejected(SendGateRejected):
    pass


class StaleScheduleGeneration(SendGateRejected):
    """The message was scheduled under an older campaign schedule generation
    (ADR-0020: a resume bumps the campaign's). It was claimed before the pause,
    so its timing and content have not been re-validated by the reconcile pass.
    Temporary: the message is handed back, re-stamped, and sent later."""


class EnrollmentStateRejected(SendGateRejected):
    pass


class RecipientRepliedRejected(SendGateRejected):
    """Refuse send authorization because a qualifying reply has already made
    this recipient ineligible. Campaign safety: the system must never send a
    future campaign email when a reply has already occurred."""


class SuppressionRejected(SendGateRejected):
    pass


class SafetyHoldRejected(SendGateRejected):
    pass


class MailboxStateRejected(SendGateRejected):
    pass


class MailboxOwnershipRejected(SendGateRejected):
    pass


class ProviderMismatchRejected(SendGateRejected):
    pass


class SnapshotIntegrityRejected(SendGateRejected):
    pass


class RecipientValidationRejected(SendGateRejected):
    pass


def check_dispatch_generation(ctx: LoadedSendContext, expected_generation: int) -> None:
    if ctx.dispatch_generation != expected_generation:
        raise StaleDispatchGeneration(
            f"task dispatch_generation={expected_generation} does not match "
            f"current message dispatch_generation={ctx.dispatch_generation}",
            terminal_reason="stale_dispatch_generation",
        )


def check_message_state(ctx: LoadedSendContext) -> None:
    if ctx.message_status in MESSAGE_RESOLVED_STATES:
        raise MessageAlreadyResolved(
            f"message already resolved: status={ctx.message_status}",
            terminal_reason="already_resolved",
        )
    if ctx.message_status not in MESSAGE_PROCEED_ELIGIBLE_STATES:
        raise MessageStateRejected(
            f"message status={ctx.message_status} is not sendable "
            f"(expected one of {sorted(MESSAGE_PROCEED_ELIGIBLE_STATES)})",
            terminal_reason="invalid_message_state",
        )


def check_campaign_state(ctx: LoadedSendContext) -> None:
    if ctx.purpose != "CAMPAIGN":
        return
    if ctx.campaign_status != "RUNNING" or ctx.campaign_planning_status != "READY":
        raise CampaignStateRejected(
            f"campaign status={ctx.campaign_status} planning_status="
            f"{ctx.campaign_planning_status} is not RUNNING/READY",
            terminal_reason="campaign_not_running",
        )


def check_schedule_generation(ctx: LoadedSendContext) -> None:
    """Scheduler discovery already requires the match; this closes the window
    for a message claimed (QUEUED) before a pause/resume bumped the campaign's
    generation, whose content/time the reconcile pass has not yet verified."""
    if ctx.purpose != "CAMPAIGN":
        return
    message_generation = ctx.message_schedule_generation
    campaign_generation = ctx.campaign_schedule_generation
    if message_generation is None or campaign_generation is None:
        return
    if message_generation != campaign_generation:
        raise StaleScheduleGeneration(
            f"message schedule_generation={message_generation} does not match "
            f"campaign schedule_generation={campaign_generation}",
            terminal_reason="stale_schedule_generation",
        )


def check_enrollment_state(ctx: LoadedSendContext) -> None:
    if ctx.purpose != "CAMPAIGN":
        return
    if ctx.enrollment_state != "ACTIVE":
        raise EnrollmentStateRejected(
            f"enrollment state={ctx.enrollment_state} is not ACTIVE",
            terminal_reason="enrollment_not_active",
        )


def check_reply_outcome(session: Session, ctx: LoadedSendContext) -> None:
    """Pre-send gate check: verifies that recipient_outcomes does not contain a
    REPLIED outcome for this enrollment, closing any race between background
    reply synchronization and send dispatch.
    """
    if ctx.purpose != "CAMPAIGN" or not ctx.enrollment_id:
        return
    stmt = text(
        "SELECT 1 FROM public.recipient_outcomes "
        "WHERE workspace_id = :ws AND enrollment_id = :eid AND kind = 'REPLIED' LIMIT 1"
    )
    res = session.execute(
        stmt,
        {"ws": str(ctx.workspace_id), "eid": str(ctx.enrollment_id)},
    ).first()
    if res:
        raise RecipientRepliedRejected(
            f"enrollment {ctx.enrollment_id} already has a recorded reply outcome",
            terminal_reason="recipient_replied",
        )


def check_suppression(session: Session, ctx: LoadedSendContext) -> None:
    """Authoritative suppression check against BOTH the workspace-scoped
    `suppressions` table and the global `platform_suppressions` table --
    neither alone is authoritative (see SUPPRESSION.md's two suppression
    dimensions). Must run immediately before authorization, never from a
    cache, and must be re-run on every retry (a recipient can unsubscribe
    between attempts).
    """
    if is_address_suppressed(session, ctx.workspace_id, ctx.address_id):
        record_message_blocked_at_send_due_to_suppression()
        raise SuppressionRejected(
            "recipient address is suppressed (workspace scope)",
            terminal_reason="suppressed",
        )
    if is_platform_suppressed(session, ctx.canonical_address):
        record_message_blocked_at_send_due_to_suppression()
        raise SuppressionRejected(
            "recipient address is suppressed (platform scope)",
            terminal_reason="suppressed",
        )


def check_safety_holds(ctx: LoadedSendContext) -> None:
    """Refuse send authorization while any pending-safety counter/hold is active
    on this mailbox or workspace, closing the race between acknowledging safety
    events and asynchronous domain processing (EVENT_SYSTEM.md §37).
    """
    if ctx.mailbox_pending_safety_count > 0:
        raise SafetyHoldRejected(
            f"mailbox has {ctx.mailbox_pending_safety_count} active pending-safety hold(s)",
            terminal_reason="safety_hold_active",
        )


def check_mailbox_state(ctx: LoadedSendContext, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    if ctx.mailbox_connection_state != "CONNECTED":
        raise MailboxStateRejected(
            f"mailbox connection_state={ctx.mailbox_connection_state} is not CONNECTED",
            terminal_reason="mailbox_disconnected",
        )
    if ctx.mailbox_policy_state != "ENABLED":
        raise MailboxStateRejected(
            f"mailbox policy_state={ctx.mailbox_policy_state}: "
            f"{ctx.mailbox_policy_reason or 'restricted'}",
            terminal_reason="mailbox_restricted",
        )
    if ctx.mailbox_health_state == "DEGRADED":
        raise MailboxStateRejected(
            "mailbox health_state=DEGRADED", terminal_reason="mailbox_degraded"
        )
    if ctx.mailbox_circuit_state == "OPEN":
        raise MailboxStateRejected(
            "mailbox circuit breaker is OPEN", terminal_reason="mailbox_circuit_open"
        )
    blocked_until = ctx.mailbox_blocked_until
    if blocked_until is not None:
        if blocked_until.tzinfo is None:
            blocked_until = blocked_until.replace(tzinfo=UTC)
        if blocked_until > now:
            raise MailboxStateRejected(
                f"mailbox is blocked until {blocked_until.isoformat()}",
                terminal_reason="mailbox_blocked",
            )


def check_mailbox_ownership(ctx: LoadedSendContext) -> None:
    """Defense in depth: the loading query already joins mailbox to message
    on workspace_id, so this can only fail if a future query change breaks
    that join -- but a cross-tenant provider send is severe enough to assert
    explicitly rather than rely solely on SQL construction."""
    if ctx.mailbox_workspace_id != ctx.workspace_id:
        raise MailboxOwnershipRejected(
            "mailbox workspace_id does not match message workspace_id",
            terminal_reason="cross_tenant_mailbox",
        )


_VALID_PROVIDERS = frozenset({"GMAIL", "MICROSOFT", "SMTP"})


def check_provider_match(ctx: LoadedSendContext) -> None:
    if ctx.mailbox_provider not in _VALID_PROVIDERS:
        raise ProviderMismatchRejected(
            f"mailbox provider={ctx.mailbox_provider!r} is not a recognized provider",
            terminal_reason="unsupported_provider",
        )


def _expected_content_digest(ctx: LoadedSendContext) -> str:
    """Two different digest formulas are authoritative depending on
    purpose -- do not unify them, they are independently owned:

    - CAMPAIGN: app.modules.campaigns.message_rendering.render_step_content,
      f"{subject}\\x00{body}\\x00{renderer_version}" (renderer-version
      sensitive, matches campaign_sequences' frozen-content digest scheme).
    - CONTROLLED_TEST: app.modules.mailboxes.service.send_controlled_test_email,
      f"{subject}\\n{body}" (no renderer_version -- test sends aren't
      rendered from a template/renderer at all).
    """
    if ctx.purpose == "CAMPAIGN":
        digest_input = (
            f"{ctx.content_subject}\x00{ctx.content_body_html}\x00"
            f"{ctx.renderer_version}"
        )
    else:
        digest_input = f"{ctx.content_subject}\n{ctx.content_body_html}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def check_snapshot_integrity(ctx: LoadedSendContext) -> None:
    """The rendered content snapshot must be present and internally
    consistent (messages_render_shape_check requires all of these together
    or none). Recomputing the digest catches silent corruption between
    render time and send time -- never send from a snapshot that doesn't
    match its own recorded digest."""
    if (
        ctx.rendered_at is None
        or not ctx.content_subject
        or not ctx.content_body_html
        or not ctx.content_digest
        or not ctx.frozen_destination
        or not ctx.frozen_sender_address
    ):
        raise SnapshotIntegrityRejected(
            "message has no valid rendered content snapshot",
            terminal_reason="missing_snapshot",
        )
    if _expected_content_digest(ctx) != ctx.content_digest:
        raise SnapshotIntegrityRejected(
            "content snapshot digest mismatch (possible corruption)",
            terminal_reason="snapshot_digest_mismatch",
        )


def check_recipient(ctx: LoadedSendContext) -> None:
    destination = ctx.frozen_destination or ""
    if not _EMAIL_PATTERN.match(destination) or any(
        c in destination for c in ("\r", "\n")
    ):
        raise RecipientValidationRejected(
            "frozen_destination is not a valid recipient address",
            terminal_reason="invalid_recipient",
        )


def run_pre_authorization_gates(
    session: Session, ctx: LoadedSendContext, *, expected_dispatch_generation: int
) -> None:
    """Runs every gate that does not require rate-limit/DB-write access, in
    the documented order. Any rejection raises before a rate reservation or
    Postgres authorization transaction is ever attempted.
    """
    check_dispatch_generation(ctx, expected_dispatch_generation)
    check_message_state(ctx)
    check_campaign_state(ctx)
    check_schedule_generation(ctx)
    check_enrollment_state(ctx)
    check_suppression(session, ctx)
    check_reply_outcome(session, ctx)
    check_safety_holds(ctx)
    check_mailbox_state(ctx)
    check_mailbox_ownership(ctx)
    check_provider_match(ctx)
    check_snapshot_integrity(ctx)
    check_recipient(ctx)
