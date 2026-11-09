from __future__ import annotations

import hashlib
import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import decrypt_credentials, encrypt_credentials
from app.core.errors import AppError
from app.core.metrics import (
    MetricsRegistry,
    record_ambiguous_outcome,
    record_auth_failure,
    record_duplicate_task_noop,
    record_provider_throttle,
    record_retry_executed,
    record_retry_exhausted,
    record_retry_scheduled,
    record_terminal_failure,
)
from app.modules.imports.storage import (
    StorageError,
    StorageObjectMissingError,
    StorageUnavailableError,
)
from app.modules.mailboxes.providers.base import (
    EmailProvider,
    EnvelopeAttachment,
    OutboundMessageEnvelope,
    ProviderCapability,
    ProviderSendResult,
)
from app.modules.mailboxes.providers.message_builder import generate_message_id
from app.modules.mailboxes.providers.registry import ProviderRegistry
from app.modules.rate_limit.limiter import RedisRateLimiter
from app.modules.rate_limit.policy_service import has_mailbox_volume_cap
from app.modules.rate_limit.repository import RatePolicyRepository
from app.modules.rate_limit.schemas import (
    RateLimitDenied,
    RateLimitGenerationStale,
    RateLimitUnavailable,
    RateReservationRequest,
)
from app.modules.scheduler.schemas import SendTaskPayload
from app.modules.sending import gates
from app.modules.sending.repository import (
    FINALIZE_LATE,
    FINALIZE_LATE_ACCEPTED,
    AttemptAlreadyClaimed,
    MessageLockedByAnotherWorker,
    SendingRepository,
)
from app.modules.sending.retry_policy import classify_and_decide
from app.modules.sending.schemas import LoadedSendContext, SendOutcome
from app.modules.tracking.pixel import (
    inject_open_pixel,
    open_pixel_url,
    open_tracking_ready,
)
from app.modules.unsubscribe.compliance import (
    ComplianceInfo,
    build_footer_html,
    build_footer_text,
    html_to_text,
    inject_footer,
    read_compliance,
    unsubscribe_headers,
    unsubscribe_ready,
    unsubscribe_url,
)

logger = logging.getLogger(__name__)

CAPACITY_UNIT = "MESSAGE"
AUTHORIZATION_WINDOW_SECONDS = 30
CREDENTIAL_REFRESH_SAFETY_MARGIN = timedelta(minutes=5)
# A message that cannot go yet is handed back to the scheduler for a time inside
# this range, plus up to 10% jitter so a burst does not come back in one tick.
MIN_DEFERRAL = timedelta(seconds=30)
MAX_DEFERRAL = timedelta(hours=24)
SAFETY_HOLD_RETRY = timedelta(minutes=15)
# Short: the reconcile pass (or its sweep) re-stamps the message within seconds
# to minutes, and until then it is unclaimable anyway.
STALE_GENERATION_RETRY = timedelta(minutes=1)
DEFERRAL_JITTER_FRACTION = 0.1


def deferral_delay(retry_after_seconds: float | None) -> timedelta:
    """How long to wait before offering a deferred message to the scheduler again.

    `retry_after_seconds` is the limiter's estimate of when capacity frees up (None
    when it has none): clamped to 30 s..24 h, then spread by up to 10%.
    """
    base = MIN_DEFERRAL
    if retry_after_seconds is not None:
        base = timedelta(seconds=retry_after_seconds)
    base = min(max(base, MIN_DEFERRAL), MAX_DEFERRAL)
    jitter = base.total_seconds() * DEFERRAL_JITTER_FRACTION * random.random()
    return base + timedelta(seconds=jitter)


class SendingService:
    """Orchestrates one `email.send` task end to end. The worker MUST NEVER
    make a send decision from the task payload alone -- every method here
    reloads authoritative PostgreSQL state before deciding anything, and the
    provider is invoked only after a committed Postgres authorization
    transaction with no open lock or transaction held (see module-level
    flow in the Phase 10 plan / docs/architecture/*).
    """

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        rate_limiter: RedisRateLimiter | None = None,
        storage_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.session = session
        self._storage_factory = storage_factory
        self.settings = settings or Settings.current()
        self.repository = SendingRepository(session)
        self.rate_policy_repository = RatePolicyRepository(session)
        self.rate_limiter = rate_limiter or RedisRateLimiter(settings=self.settings)

    def execute(self, payload: SendTaskPayload) -> SendOutcome:
        workspace_id = UUID(payload.workspace_id)
        message_id = UUID(payload.message_id)

        try:
            ctx = self.repository.load_message_for_send(
                workspace_id=workspace_id, message_id=message_id
            )
        except MessageLockedByAnotherWorker:
            self.session.rollback()
            record_duplicate_task_noop("locked")
            logger.info(
                "Message locked by a concurrent worker; safe no-op",
                extra={"message_id": str(message_id)},
            )
            return SendOutcome(message_id=message_id, outcome="NOOP", reason="locked")

        if ctx is None:
            self.session.rollback()
            record_duplicate_task_noop("message_not_found")
            logger.warning(
                "email.send task references a message that does not exist; "
                "terminal, non-retryable",
                extra={"message_id": str(message_id)},
            )
            return SendOutcome(
                message_id=message_id, outcome="NOOP", reason="message_not_found"
            )

        try:
            gates.run_pre_authorization_gates(
                self.session,
                ctx,
                expected_dispatch_generation=payload.dispatch_generation,
            )
        except (gates.MessageAlreadyResolved, gates.StaleDispatchGeneration) as exc:
            self.session.rollback()
            record_duplicate_task_noop(exc.terminal_reason)
            return SendOutcome(
                message_id=message_id, outcome="NOOP", reason=exc.terminal_reason
            )
        except (
            gates.CampaignStateRejected,
            gates.StaleScheduleGeneration,
            gates.EnrollmentStateRejected,
            gates.MailboxStateRejected,
            gates.SafetyHoldRejected,
        ) as exc:
            # Temporarily ineligible, may become eligible again later (e.g.
            # campaign resumes). Leave the message QUEUED and untouched --
            # Phase 9's existing claim-lease recovery sweep will revert it
            # to SCHEDULED/RETRY_SCHEDULED once claim_expires_at passes, and
            # the scheduler will reconsider it naturally. No destructive
            # state change here.
            self._audit(ctx, action="message.send_deferred", reason=exc.terminal_reason)
            if isinstance(exc, gates.SafetyHoldRejected):
                # A held mailbox stays held until a person releases it: retry
                # in 15 minutes instead of churning through claim leases.
                self._hand_back(ctx, SAFETY_HOLD_RETRY)
            elif isinstance(exc, gates.MailboxStateRejected):
                # Disconnected, restricted, degraded, breaker open or in cooldown.
                # Leaving it QUEUED would have the lease expire (5 minutes) and the
                # scheduler claim it again at once -- a loop. Hand it back; the
                # scheduler also stops claiming for a mailbox that cannot send.
                self._hand_back(ctx, self._mailbox_retry_delay(ctx, exc))
            elif isinstance(exc, gates.StaleScheduleGeneration):
                # Claimed before a pause/resume: release it now (with a
                # dispatch-generation bump) so the reconcile pass can re-time
                # and re-stamp it, instead of waiting out the claim lease.
                self._hand_back(ctx, STALE_GENERATION_RETRY)
            self.session.commit()
            logger.info(
                "Send deferred (temporary gate rejection)",
                extra={"message_id": str(message_id), "reason": exc.terminal_reason},
            )
            if (
                isinstance(exc, gates.CampaignStateRejected)
                and ctx.campaign_status == "PAUSED"
            ):
                logger.info(
                    "CAMPAIGN_EXECUTION_SKIPPED_PAUSED campaign=%s message=%s",
                    ctx.campaign_id,
                    message_id,
                )
                MetricsRegistry.get().increment("campaign_execution_skipped_paused_total")
            return SendOutcome(
                message_id=message_id, outcome="DEFERRED", reason=exc.terminal_reason
            )
        except gates.SendGateRejected as exc:
            # Permanent for this message as constructed (suppressed,
            # invalid recipient, corrupted snapshot, cross-tenant/provider
            # mismatch) -- terminal SKIPPED, never sent.
            return self._skip(ctx, exc.terminal_reason)

        compliance: ComplianceInfo | None = None
        if ctx.purpose == "CAMPAIGN":
            # Checked before any capacity is reserved: an email without a working
            # opt-out is never sent, and never costs a reservation either.
            if not unsubscribe_ready(self.settings):
                self._audit(
                    ctx,
                    action="message.send_deferred",
                    reason="unsubscribe_not_configured",
                )
                self._hand_back(ctx, SAFETY_HOLD_RETRY)
                self.session.commit()
                logger.error(
                    "Campaign send deferred: UNSUBSCRIBE_BASE_URL and "
                    "UNSUBSCRIBE_SIGNING_KEY must both be set",
                    extra={"message_id": str(message_id)},
                )
                return SendOutcome(
                    message_id=message_id,
                    outcome="DEFERRED",
                    reason="unsubscribe_not_configured",
                )
            compliance = read_compliance(
                self.repository.get_workspace_defaults(workspace_id=ctx.workspace_id)
            )

        return self._authorize_and_send(ctx, payload, compliance)

    # -------------------------------------------------------------------------
    # Rate reservation + authorization transaction
    # -------------------------------------------------------------------------

    def _authorize_and_send(
        self,
        ctx: LoadedSendContext,
        payload: SendTaskPayload,
        compliance: ComplianceInfo | None = None,
    ) -> SendOutcome:
        policies = self.rate_policy_repository.resolve_applicable_policies(
            workspace_id=ctx.workspace_id,
            campaign_id=ctx.campaign_id,
            mailbox_id=ctx.mailbox_id,
            provider=ctx.mailbox_provider,
            provider_account_id=ctx.mailbox_provider_account_id,
            unit=CAPACITY_UNIT,
        )

        if ctx.purpose == "CAMPAIGN" and not has_mailbox_volume_cap(policies):
            # Fail closed: a mailbox without a daily cap must never send
            # unlimited. A person has to give it one (Mailboxes > limits).
            self._audit(
                ctx, action="message.send_deferred", reason="mailbox_limits_missing"
            )
            self._hand_back(ctx, SAFETY_HOLD_RETRY)
            self.session.commit()
            logger.error(
                "Campaign send deferred: mailbox has no daily sending limit",
                extra={
                    "message_id": str(ctx.message_id),
                    "mailbox_id": str(ctx.mailbox_id),
                },
            )
            return SendOutcome(
                message_id=ctx.message_id,
                outcome="DEFERRED",
                reason="mailbox_limits_missing",
            )

        try:
            expected_generation = self.rate_limiter.current_generation()
            if expected_generation is None:
                raise RateLimitGenerationStale("no generation set")
            reservation = self.rate_limiter.reserve(
                RateReservationRequest(
                    message_id=str(ctx.message_id), quantity=1, policies=tuple(policies)
                ),
                expected_generation=expected_generation,
            )
        except RateLimitUnavailable as exc:
            # Fail closed: no provider call without a confirmed-healthy
            # limiter. RATE_LIMITING.md: "A missing key never means an
            # unused full budget."
            self.session.rollback()
            self._audit(ctx, action="message.rate_limit_unavailable", reason=str(exc))
            self.session.commit()
            logger.warning(
                "Rate limiter unavailable; deferring send (fail closed)",
                extra={"message_id": str(ctx.message_id), "error": str(exc)},
            )
            return SendOutcome(
                message_id=ctx.message_id,
                outcome="DEFERRED",
                reason="rate_limit_unavailable",
            )
        except RateLimitDenied as exc:
            self.session.rollback()
            # Back to the scheduler for when capacity frees up, instead of
            # sitting QUEUED until the claim lease expires and being claimed again.
            self._hand_back(ctx, deferral_delay(exc.denial.retry_after_seconds))
            self.session.commit()
            logger.info(
                "Rate limit capacity denied; deferring send",
                extra={
                    "message_id": str(ctx.message_id),
                    "reason": exc.denial.reason,
                    "failing_kind": exc.denial.failing_kind,
                },
            )
            return SendOutcome(
                message_id=ctx.message_id,
                outcome="DEFERRED",
                reason="rate_limit_denied",
            )

        try:
            outcome = self._run_authorization_transaction(
                ctx, payload, policies, reservation, expected_generation
            )
        except _AuthorizationAborted as exc:
            self.rate_limiter.release(reservation.reservation_id)
            self.session.rollback()
            return SendOutcome(
                message_id=ctx.message_id, outcome="DEFERRED", reason=exc.reason
            )
        except AttemptAlreadyClaimed:
            self.rate_limiter.release(reservation.reservation_id)
            self.session.rollback()
            record_duplicate_task_noop("already_claimed")
            logger.info(
                "Attempt already claimed by a concurrent worker; safe no-op",
                extra={"message_id": str(ctx.message_id)},
            )
            return SendOutcome(
                message_id=ctx.message_id, outcome="NOOP", reason="already_claimed"
            )

        # Authorization transaction committed: message is now SENDING, a
        # PREPARED attempt exists, and capacity has been durably debited.
        # From here on, capacity is NEVER released/refunded regardless of
        # outcome (RATE_LIMITING.md: no refunds for uncertain provider
        # usage) -- only the pre-commit abort paths above call release().
        attempt_id, fresh_ctx, cred_gen, deadline = outcome
        if fresh_ctx.retry_count > 0:
            record_retry_executed(fresh_ctx.mailbox_provider)
        return self._invoke_provider_and_finalize(
            attempt_id, fresh_ctx, cred_gen, compliance, authorization_deadline=deadline
        )

    def _run_authorization_transaction(
        self,
        ctx: LoadedSendContext,
        payload: SendTaskPayload,
        policies: list[Any],
        reservation: Any,
        expected_generation: int,
    ) -> tuple[UUID, LoadedSendContext, int, datetime]:
        """Re-verify everything under a fresh, lock-holding reload, then
        claim the attempt and commit SENDING atomically. Returns
        (attempt_id, fresh_ctx, credential_generation, authorization_deadline)
        for the caller to use outside this (now-committed) transaction."""
        rate_status = self.repository.get_rate_control_status()
        if (
            rate_status is None
            or rate_status["status"] != "READY"
            or int(rate_status["generation"]) != reservation.generation
        ):
            raise _AuthorizationAborted("rate_control_generation_mismatch")

        try:
            fresh_ctx = self.repository.load_message_for_send(
                workspace_id=ctx.workspace_id, message_id=ctx.message_id
            )
        except MessageLockedByAnotherWorker:
            raise _AuthorizationAborted("locked_by_concurrent_worker") from None
        if fresh_ctx is None:
            raise _AuthorizationAborted("message_not_found")

        try:
            gates.run_pre_authorization_gates(
                self.session,
                fresh_ctx,
                expected_dispatch_generation=payload.dispatch_generation,
            )
        except gates.SendGateRejected as exc:
            raise _AuthorizationAborted(exc.terminal_reason) from exc

        attempt_id = uuid4()
        ordinal = self.repository.next_attempt_ordinal(
            workspace_id=fresh_ctx.workspace_id, message_id=fresh_ctx.message_id
        )
        now = datetime.now(UTC)
        deadline = now + timedelta(seconds=AUTHORIZATION_WINDOW_SECONDS)
        self.repository.insert_prepared_attempt(
            attempt_id=attempt_id,
            workspace_id=fresh_ctx.workspace_id,
            message_id=fresh_ctx.message_id,
            mailbox_id=fresh_ctx.mailbox_id,
            ordinal=ordinal,
            invocation_owner=f"worker-send:{payload.work_id}",
            credential_generation=fresh_ctx.mailbox_current_connection_generation,
            authorization_deadline=deadline,
            dispatch_generation=fresh_ctx.dispatch_generation,
        )

        scope_rows = [
            {
                "rate_scope_id": (
                    p.source_id
                    if p.kind.value in ("PLATFORM", "PROVIDER", "PROVIDER_ACCOUNT")
                    else None
                ),
                "tenant_rate_policy_id": (
                    p.source_id
                    if p.kind.value in ("WORKSPACE", "CAMPAIGN", "MAILBOX")
                    else None
                ),
                "policy_version": p.policy_version,
                "policy_snapshot": {
                    "kind": p.kind.value,
                    "scope_key": p.scope_key,
                    "window_seconds": p.window_seconds,
                    "limit_value": p.limit_value,
                },
                "window_kind": p.window_kind,
            }
            for p in policies
        ]
        self.repository.insert_capacity_debit(
            workspace_id=fresh_ctx.workspace_id,
            attempt_id=attempt_id,
            reservation_id=reservation.reservation_id,
            unit=CAPACITY_UNIT,
            quantity=1,
            scopes=scope_rows,
        )

        transitioned = self.repository.transition_message_to_sending(
            workspace_id=fresh_ctx.workspace_id,
            message_id=fresh_ctx.message_id,
            expected_version=fresh_ctx.message_version,
        )
        if not transitioned:
            raise _AuthorizationAborted("message_version_conflict")

        self.session.commit()
        return (
            attempt_id,
            fresh_ctx,
            fresh_ctx.mailbox_current_connection_generation,
            deadline,
        )

    # -------------------------------------------------------------------------
    # Provider invocation (no open transaction/lock) + result persistence
    # -------------------------------------------------------------------------

    def _invoke_provider_and_finalize(
        self,
        attempt_id: UUID,
        ctx: LoadedSendContext,
        credential_generation: int,
        compliance: ComplianceInfo | None = None,
        *,
        authorization_deadline: datetime | None = None,
    ) -> SendOutcome:
        provider = ProviderRegistry.get(ctx.mailbox_provider)

        try:
            credential, credential_generation = self._load_and_refresh_credential(
                ctx, provider, credential_generation
            )
        except _CredentialFailure as exc:
            applied = self._finalize_not_invoked(ctx, attempt_id, exc)
            self.session.commit()
            return SendOutcome(
                message_id=ctx.message_id,
                outcome=exc.message_status if applied else "NOOP",
                reason=exc.error_category if applied else "late_provider_result",
                attempt_id=attempt_id,
            )

        # Files are fetched now, outside any DB transaction, and verified against
        # the digests recorded when they were uploaded. A problem here means the
        # provider was never invoked, so the message can be failed or retried
        # without any ambiguity about whether it was sent.
        try:
            attachments = self._load_attachments(ctx)
        except _CredentialFailure as exc:
            applied = self._finalize_not_invoked(ctx, attempt_id, exc)
            self.session.commit()
            return SendOutcome(
                message_id=ctx.message_id,
                outcome=exc.message_status if applied else "NOOP",
                reason=exc.error_category if applied else "late_provider_result",
                attempt_id=attempt_id,
            )

        from_address = ctx.frozen_sender_address or ctx.mailbox_original_address
        # Replies and bounces quote the Message-ID, so every campaign message
        # must carry one we know. It is stored only together with the result
        # (finalize): the commit-retry loop below reuses this value, and a later
        # attempt reuses it only if an earlier result was stored with it (e.g.
        # UNKNOWN_OUTCOME). Before the provider call it is not persisted
        # (PROVIDER_ARCHITECTURE.md asks for that; not done yet).
        rfc_message_id = ctx.rfc_message_id or generate_message_id(from_address)
        body_html, body_text, extra_headers = self._compose_outbound(ctx, compliance)
        reply_headers, provider_thread_id, _ = self._thread_context(ctx)
        envelope = OutboundMessageEnvelope(
            to_address=ctx.frozen_destination or "",
            from_address=from_address,
            from_name=ctx.frozen_sender_name or ctx.mailbox_sender_display_name,
            subject=ctx.content_subject or "",
            body_html=body_html,
            body_text=body_text,
            rfc_message_id=rfc_message_id,
            attachments=attachments,
            extra_headers=extra_headers + reply_headers,
            provider_thread_id=provider_thread_id,
            in_reply_to=ctx.thread_in_reply_to if reply_headers else None,
        )

        try:
            # The deadline lets an adapter refuse to start a send that could still
            # be in flight when recovery declares the attempt abandoned.
            send_result = provider.send_message(
                credential, envelope, authorization_deadline=authorization_deadline
            )
        except Exception as exc:  # noqa: BLE001 -- see comment below
            # Any exception escaping the adapter is treated as ambiguous,
            # never as "definitely not sent": the adapters are documented to
            # return ProviderSendResult(status="UNKNOWN"/"DEFINITIVELY_REJECTED")
            # for every case they can classify, so an escaping exception
            # means this worker cannot determine what happened. Per
            # MESSAGE_STATE_MACHINE.md / RATE_LIMITING.md: never blindly
            # treat an unclassified failure as safe-to-retry.
            logger.error(
                "Unexpected exception from provider adapter; treating as "
                "ambiguous outcome",
                extra={
                    "message_id": str(ctx.message_id),
                    "provider": ctx.mailbox_provider,
                },
            )
            send_result = ProviderSendResult(
                status="UNKNOWN",
                error_category="NETWORK_ERROR",
                error_code=type(exc).__name__,
            )

        outcome = self._finalize_send_result(
            ctx, attempt_id, send_result, rfc_message_id
        )
        # Commit with retry on transient DB error: if provider accepted but
        # DB finalization commit experiences a transient glitch, retry commit.
        # If DB fails permanently, rollback and raise without re-calling provider;
        # stale execution recovery + reconciliation will safely recover the send.
        for commit_attempt in range(3):
            try:
                self.session.commit()
                break
            except Exception as commit_err:
                self.session.rollback()
                if commit_attempt == 2:
                    logger.error(
                        "Failed to commit attempt finalization after provider call; "
                        "rolling back to allow recovery/reconciliation safely",
                        extra={
                            "message_id": str(ctx.message_id),
                            "error": str(commit_err),
                        },
                    )
                    raise
                self._finalize_send_result(
                    ctx, attempt_id, send_result, rfc_message_id
                )
        return outcome

    def _load_attachments(
        self, ctx: LoadedSendContext
    ) -> tuple[EnvelopeAttachment, ...]:
        step_id = ctx.raw.get("step_id")
        if ctx.purpose != "CAMPAIGN" or not step_id:
            return ()
        rows = self.repository.list_step_attachments(
            workspace_id=ctx.workspace_id, step_id=UUID(str(step_id))
        )
        # End the read transaction before any network I/O.
        self.session.commit()
        if not rows:
            return ()

        if self._storage_factory is not None:
            storage = self._storage_factory()
        else:
            from app.modules.campaigns.attachment_service import (
                default_storage_client,
            )

            storage = default_storage_client()

        loaded: list[EnvelopeAttachment] = []
        for row in rows:
            try:
                data = storage.download_object(row["storage_key"])
            except StorageObjectMissingError:
                raise _CredentialFailure(
                    error_category="CONFIGURATION_FAILURE",
                    error_code="attachment_missing",
                    message_status="FAILED",
                ) from None
            except StorageUnavailableError:
                raise _CredentialFailure(
                    error_category="TRANSIENT_FAILURE",
                    error_code="attachment_storage_unavailable",
                    message_status="RETRY_SCHEDULED",
                    hold_reason="attachment_storage_unavailable",
                ) from None
            except StorageError:
                raise _CredentialFailure(
                    error_category="CONFIGURATION_FAILURE",
                    error_code="attachment_unreadable",
                    message_status="FAILED",
                ) from None
            if (
                len(data) != row["size_bytes"]
                or hashlib.sha256(data).hexdigest() != row["sha256"]
            ):
                raise _CredentialFailure(
                    error_category="CONFIGURATION_FAILURE",
                    error_code="attachment_corrupt",
                    message_status="FAILED",
                )
            loaded.append(
                EnvelopeAttachment(
                    filename=row["filename"],
                    content_type=row["content_type"],
                    data=data,
                    content_id=(
                        row["content_id"] if row["disposition"] == "INLINE" else None
                    ),
                )
            )
        return tuple(loaded)

    def _load_and_refresh_credential(
        self,
        ctx: LoadedSendContext,
        provider: EmailProvider,
        credential_generation: int,
    ) -> tuple[dict[str, Any], int]:
        conn = self.repository.get_mailbox_connection(
            workspace_id=ctx.workspace_id,
            mailbox_id=ctx.mailbox_id,
            generation=credential_generation,
        )
        if not conn or not conn["credential_ciphertext"]:
            raise _CredentialFailure(
                error_category="CONFIGURATION_FAILURE",
                error_code="credentials_missing",
                message_status="FAILED",
            )

        try:
            creds = decrypt_credentials(
                conn["credential_ciphertext"],
                conn["nonce"],
                ctx.workspace_id,
                ctx.mailbox_id,
                ctx.mailbox_provider,
                conn["encryption_key_id"],
            )
        except AppError:
            raise _CredentialFailure(
                error_category="CONFIGURATION_FAILURE",
                error_code="credential_decrypt_failed",
                message_status="FAILED",
            ) from None

        credential: dict[str, Any] = dict(creds)
        if conn.get("protected_config"):
            credential.update(conn["protected_config"])

        expires_at = conn["expires_at"]
        now = datetime.now(UTC)
        if (
            ProviderCapability.CREDENTIAL_REFRESH in provider.capabilities
            and expires_at
            and (now + CREDENTIAL_REFRESH_SAFETY_MARGIN >= expires_at)
        ):
            refresh_token = creds.get("refresh_token")
            if not refresh_token:
                self.repository.update_mailbox_connection_state(
                    workspace_id=ctx.workspace_id,
                    mailbox_id=ctx.mailbox_id,
                    connection_state="RECONNECT_REQUIRED",
                    health_state="DEGRADED",
                    connected_generation=None,
                    current_connection_generation=credential_generation,
                )
                record_auth_failure(ctx.mailbox_provider)
                raise _CredentialFailure(
                    error_category="AUTH_FAILURE",
                    error_code="refresh_token_unavailable",
                    message_status="RETRY_SCHEDULED",
                    hold_reason="mailbox_reauth_required",
                )
            try:
                refresh_res = provider.refresh_token(refresh_token)
            except AppError:
                self.repository.update_mailbox_connection_state(
                    workspace_id=ctx.workspace_id,
                    mailbox_id=ctx.mailbox_id,
                    connection_state="RECONNECT_REQUIRED",
                    health_state="DEGRADED",
                    connected_generation=None,
                    current_connection_generation=credential_generation,
                )
                record_auth_failure(ctx.mailbox_provider)
                raise _CredentialFailure(
                    error_category="AUTH_FAILURE",
                    error_code="refresh_failed",
                    message_status="RETRY_SCHEDULED",
                    hold_reason="mailbox_reauth_required",
                ) from None

            new_refresh_token = refresh_res.refresh_token or refresh_token
            new_ct, new_key_id, new_nonce = encrypt_credentials(
                {
                    "access_token": refresh_res.access_token,
                    "refresh_token": new_refresh_token,
                    "token_type": refresh_res.token_type,
                },
                ctx.workspace_id,
                ctx.mailbox_id,
                ctx.mailbox_provider,
            )
            new_generation = credential_generation + 1
            self.repository.insert_mailbox_connection(
                connection_id=uuid4(),
                workspace_id=ctx.workspace_id,
                mailbox_id=ctx.mailbox_id,
                generation=new_generation,
                credential_ciphertext=new_ct,
                encryption_key_id=new_key_id,
                nonce=new_nonce,
                auth_mechanism="OAUTH",
                granted_scopes=refresh_res.granted_scopes,
                expires_at=now + timedelta(seconds=refresh_res.expires_in),
            )
            self.repository.update_mailbox_connection_state(
                workspace_id=ctx.workspace_id,
                mailbox_id=ctx.mailbox_id,
                connection_state="CONNECTED",
                health_state="HEALTHY",
                connected_generation=new_generation,
                current_connection_generation=new_generation,
            )
            credential["access_token"] = refresh_res.access_token
            credential_generation = new_generation

        return credential, credential_generation

    def _compose_outbound(
        self, ctx: LoadedSendContext, compliance: ComplianceInfo | None
    ) -> tuple[str, str | None, tuple[tuple[str, str], ...]]:
        """The HTML body, plain-text alternative and extra headers to send.

        The single place every campaign email gets its unsubscribe link, footer,
        List-Unsubscribe headers and (when configured) open-tracking pixel. It is
        added here, at send time, and not at render time: the rendered body is a
        frozen, digest-verified snapshot that must not change, and there are
        several render paths that a render-time footer could miss.
        """
        body = ctx.content_body_html or ""
        if not body or ctx.purpose != "CAMPAIGN":
            return body, None, ()

        settings = self.settings
        link = unsubscribe_url(settings, ctx.workspace_id, ctx.message_id)
        info = compliance or ComplianceInfo()
        text = html_to_text(body) + "\n\n" + build_footer_text(link, info)
        html = inject_footer(body, build_footer_html(link, info))
        if open_tracking_ready(settings):
            html = inject_open_pixel(
                html, open_pixel_url(settings, ctx.workspace_id, ctx.message_id)
            )
        return html, text, unsubscribe_headers(link)

    @staticmethod
    def _thread_context(
        ctx: LoadedSendContext,
    ) -> tuple[tuple[tuple[str, str], ...], str | None, str | None]:
        """(reply headers, Gmail thread id, why it could not be threaded) for a
        follow-up sent in the same thread (ADR-0021). When it cannot be threaded it
        is sent as a normal email with its frozen "Re:" subject: never failed."""
        if ctx.reply_to_message_id is None:
            return (), None, None
        if not ctx.thread_in_reply_to:
            return (), None, "parent_message_id_missing"
        # Graph ignores these headers and threads with createReply instead, from
        # envelope.in_reply_to (ADR-0021 D4).
        references = ctx.thread_references or (ctx.thread_in_reply_to,)
        headers = (
            ("In-Reply-To", ctx.thread_in_reply_to),
            ("References", " ".join(references)),
        )
        thread_id = ctx.thread_provider_id if ctx.mailbox_provider == "GMAIL" else None
        return headers, thread_id, None

    def _finalize_send_result(
        self,
        ctx: LoadedSendContext,
        attempt_id: UUID,
        send_result: ProviderSendResult,
        envelope_rfc_message_id: str | None = None,
    ) -> SendOutcome:
        unthreaded = self._thread_context(ctx)[2] or (
            "provider_thread_not_found" if send_result.thread_dropped else None
        )
        if unthreaded is not None:
            self._audit(
                ctx, action="message.same_thread_unavailable", reason=unthreaded
            )
        # What the recipient will quote in In-Reply-To: the value the provider
        # reports, else the one we put in the envelope.
        rfc_message_id = send_result.rfc_message_id or envelope_rfc_message_id
        if send_result.status == "ACCEPTED":
            applied = self.repository.finalize_attempt_result(
                workspace_id=ctx.workspace_id,
                message_id=ctx.message_id,
                attempt_id=attempt_id,
                message_status="SENT",
                evidence_state="ACCEPTED",
                provider_message_id=send_result.provider_message_id,
                provider_thread_id=send_result.provider_thread_id,
                # Locally-generated attempt identity, always present as
                # acceptance evidence regardless of whether the provider
                # itself returned a message id (Graph/SMTP often don't) --
                # required by message_attempts_acceptance_evidence_check.
                provider_request_id=(
                    ctx.rfc_message_id
                    or rfc_message_id
                    or send_result.provider_message_id
                ),
                rfc_message_id=rfc_message_id,
            )
            if applied == FINALIZE_LATE:
                return self._late_result(ctx, attempt_id, "ACCEPTED")
            self._audit(
                ctx,
                action="message.sent",
                reason=(
                    "accepted_after_recovery"
                    if applied == FINALIZE_LATE_ACCEPTED
                    else None
                ),
            )
            return SendOutcome(
                message_id=ctx.message_id,
                outcome="SENT",
                attempt_id=attempt_id,
                provider_message_id=send_result.provider_message_id,
            )

        if send_result.status == "UNKNOWN":
            record_ambiguous_outcome(ctx.mailbox_provider, send_result.error_category)
            db_error_category = (
                "NETWORK_ERROR"
                if str(send_result.error_category)
                in ("UNKNOWN_OUTCOME", "ErrorCategory.UNKNOWN_OUTCOME")
                else (
                    str(send_result.error_category)
                    if send_result.error_category
                    else None
                )
            )
            applied = self.repository.finalize_attempt_result(
                workspace_id=ctx.workspace_id,
                message_id=ctx.message_id,
                attempt_id=attempt_id,
                message_status="UNKNOWN_OUTCOME",
                evidence_state="UNKNOWN",
                error_category=db_error_category,
                error_code=send_result.error_code,
                rfc_message_id=rfc_message_id,
            )
            if applied == FINALIZE_LATE:
                return self._late_result(ctx, attempt_id, "UNKNOWN")
            self._audit(
                ctx,
                action="message.ambiguous_outcome",
                reason=send_result.error_category,
            )
            return SendOutcome(
                message_id=ctx.message_id,
                outcome="UNKNOWN_OUTCOME",
                reason=send_result.error_category,
                attempt_id=attempt_id,
            )

        # DEFINITIVELY_REJECTED
        if send_result.error_category == "AUTH_FAILURE":
            self.repository.update_mailbox_connection_state(
                workspace_id=ctx.workspace_id,
                mailbox_id=ctx.mailbox_id,
                connection_state="RECONNECT_REQUIRED",
                health_state="DEGRADED",
                connected_generation=None,
                current_connection_generation=ctx.mailbox_current_connection_generation,
            )
            record_auth_failure(ctx.mailbox_provider)
            now = datetime.now(UTC)
            hold_time = now + timedelta(hours=1)
            applied = self.repository.finalize_attempt_result(
                workspace_id=ctx.workspace_id,
                message_id=ctx.message_id,
                attempt_id=attempt_id,
                message_status="RETRY_SCHEDULED",
                evidence_state="REJECTED",
                error_category="AUTH_FAILURE",
                error_code=send_result.error_code,
                retry_count=ctx.retry_count,
                next_retry_at=hold_time,
                due_at=hold_time,
                hold_reason="mailbox_reauth_required",
            )
            if applied == FINALIZE_LATE:
                return self._late_result(ctx, attempt_id, "REJECTED")
            self._audit(
                ctx, action="message.held_for_reauth", reason=send_result.error_code
            )
            return SendOutcome(
                message_id=ctx.message_id,
                outcome="RETRY_SCHEDULED",
                reason="AUTH_FAILURE",
                attempt_id=attempt_id,
            )

        if send_result.error_category == "RATE_LIMIT":
            record_provider_throttle(ctx.mailbox_provider)
            cooldown = (
                send_result.retry_after_seconds
                if (
                    send_result.retry_after_seconds
                    and send_result.retry_after_seconds > 0
                )
                else 60.0
            )
            now = datetime.now(UTC)
            self.repository.update_mailbox_blocked_until(
                workspace_id=ctx.workspace_id,
                mailbox_id=ctx.mailbox_id,
                blocked_until=now + timedelta(seconds=cooldown),
            )

        anchor_at = ctx.raw.get("anchor_at") or ctx.rendered_at
        decision = classify_and_decide(
            error_category=send_result.error_category,
            is_retryable=None,
            retry_count=ctx.retry_count,
            retry_budget=ctx.retry_budget,
            provider_retry_after_seconds=send_result.retry_after_seconds,
            anchor_at=anchor_at,
        )
        if decision.should_retry:
            applied = self.repository.finalize_attempt_result(
                workspace_id=ctx.workspace_id,
                message_id=ctx.message_id,
                attempt_id=attempt_id,
                message_status="RETRY_SCHEDULED",
                evidence_state="REJECTED",
                error_category=send_result.error_category,
                error_code=send_result.error_code,
                retry_count=ctx.retry_count + 1,
                next_retry_at=decision.next_retry_at,
                due_at=decision.next_retry_at,
            )
            if applied == FINALIZE_LATE:
                return self._late_result(ctx, attempt_id, "REJECTED")
            record_retry_scheduled(ctx.mailbox_provider, send_result.error_category)
            self._audit(
                ctx, action="message.retry_scheduled", reason=send_result.error_category
            )
            return SendOutcome(
                message_id=ctx.message_id,
                outcome="RETRY_SCHEDULED",
                reason=send_result.error_category,
                attempt_id=attempt_id,
            )

        if decision.terminal_reason == "retry_budget_exhausted":
            record_retry_exhausted(ctx.mailbox_provider)
        record_terminal_failure(ctx.mailbox_provider, decision.terminal_reason)

        applied = self.repository.finalize_attempt_result(
            workspace_id=ctx.workspace_id,
            message_id=ctx.message_id,
            attempt_id=attempt_id,
            message_status="FAILED",
            evidence_state="REJECTED",
            error_category=send_result.error_category,
            error_code=send_result.error_code,
            terminal_reason=decision.terminal_reason or "send_rejected",
        )
        if applied == FINALIZE_LATE:
            return self._late_result(ctx, attempt_id, "REJECTED")
        self._audit(ctx, action="message.failed", reason=send_result.error_category)
        return SendOutcome(
            message_id=ctx.message_id,
            outcome="FAILED",
            reason=send_result.error_category,
            attempt_id=attempt_id,
        )

    def _late_result(
        self, ctx: LoadedSendContext, attempt_id: UUID, classification: str
    ) -> SendOutcome:
        """The attempt was already moved on (recovery, reconciliation) when its
        result arrived (issue I-01). The repository appended it as attempt
        evidence and changed nothing; it is audited and never retried."""
        self._audit(ctx, action="message.late_provider_result", reason=classification)
        logger.warning(
            "Late provider result recorded without changing the message",
            extra={
                "message_id": str(ctx.message_id),
                "attempt_id": str(attempt_id),
                "classification": classification,
            },
        )
        return SendOutcome(
            message_id=ctx.message_id,
            outcome="NOOP",
            reason="late_provider_result",
            attempt_id=attempt_id,
        )

    def _finalize_not_invoked(
        self, ctx: LoadedSendContext, attempt_id: UUID, failure: _CredentialFailure
    ) -> bool:
        """Record that the provider was never called. False when the attempt had
        already been moved on (then it is only audited as a late result)."""
        hold_time = (
            (datetime.now(UTC) + timedelta(hours=1))
            if failure.message_status == "RETRY_SCHEDULED"
            else None
        )
        applied = self.repository.finalize_attempt_result(
            workspace_id=ctx.workspace_id,
            message_id=ctx.message_id,
            attempt_id=attempt_id,
            message_status=failure.message_status,
            evidence_state="NOT_INVOKED",
            error_category=failure.error_category,
            error_code=failure.error_code,
            terminal_reason=(
                failure.error_code if failure.message_status == "FAILED" else None
            ),
            hold_reason=failure.hold_reason,
            due_at=hold_time,
            next_retry_at=hold_time,
        )
        if applied == FINALIZE_LATE:
            self._late_result(ctx, attempt_id, "NOT_INVOKED")
            return False
        self._audit(ctx, action="message.credential_failure", reason=failure.error_code)
        return True

    # -------------------------------------------------------------------------
    # Shared helpers
    # -------------------------------------------------------------------------

    def _skip(self, ctx: LoadedSendContext, terminal_reason: str) -> SendOutcome:
        record_terminal_failure(ctx.mailbox_provider, terminal_reason)
        self.repository.skip_message(
            workspace_id=ctx.workspace_id,
            message_id=ctx.message_id,
            expected_version=ctx.message_version,
            terminal_reason=terminal_reason,
        )
        self._audit(ctx, action="message.skipped", reason=terminal_reason)
        self.session.commit()
        logger.info(
            "Message skipped (terminal gate rejection)",
            extra={"message_id": str(ctx.message_id), "reason": terminal_reason},
        )
        return SendOutcome(
            message_id=ctx.message_id, outcome="SKIPPED", reason=terminal_reason
        )

    @staticmethod
    def _mailbox_retry_delay(
        ctx: LoadedSendContext, exc: gates.MailboxStateRejected
    ) -> timedelta:
        """How long to leave a message alone after its mailbox refused it. A cooldown
        has a known end (the same clamped, jittered wait a rate-limit hint gets);
        anything else needs a person or a recovery, so wait as for a held mailbox."""
        if exc.terminal_reason == "mailbox_blocked" and ctx.mailbox_blocked_until:
            blocked_until = ctx.mailbox_blocked_until
            if blocked_until.tzinfo is None:
                blocked_until = blocked_until.replace(tzinfo=UTC)
            seconds = (blocked_until - datetime.now(UTC)).total_seconds()
            return deferral_delay(max(0.0, seconds))
        return SAFETY_HOLD_RETRY

    def _hand_back(self, ctx: LoadedSendContext, delay: timedelta) -> None:
        """Return this claimed message to the scheduler, due after `delay`.

        Joins the caller's transaction: the caller commits.
        """
        self.repository.defer_for_capacity(
            workspace_id=ctx.workspace_id,
            message_id=ctx.message_id,
            dispatch_generation=ctx.dispatch_generation,
            due_at=datetime.now(UTC) + delay,
        )

    def _audit(
        self, ctx: LoadedSendContext, *, action: str, reason: str | None
    ) -> None:
        self.repository.record_audit_event(
            workspace_id=ctx.workspace_id,
            action=action,
            target_id=ctx.message_id,
            after_state={"mailbox_id": str(ctx.mailbox_id), "purpose": ctx.purpose},
            reason=reason,
        )


class _AuthorizationAborted(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _CredentialFailure(Exception):
    """ A failure detected BEFORE the provider is invoked (bad/missing credential,
    unreadable attachment). Despite the name it covers any pre-send payload
    problem: the provider was never called, so the message can be failed or
    retried with no ambiguity about whether it went out."""

    def __init__(
        self,
        *,
        error_category: str,
        error_code: str,
        message_status: str,
        hold_reason: str | None = None,
    ) -> None:
        self.error_category = error_category
        self.error_code = error_code
        self.message_status = message_status
        self.hold_reason = hold_reason
        super().__init__(error_code)
