from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class LoadedSendContext:
    """Authoritative state reloaded from PostgreSQL immediately before a
    send decision -- never trust the Celery task payload beyond its IDs.
    One row each from messages/campaigns/campaign_enrollments/mailboxes/
    mailbox_connections/recipient_addresses, joined under a workspace-scoped
    transaction. Campaign/enrollment fields are None for purpose=CONTROLLED_TEST
    messages, which have no campaign/enrollment at all.
    """

    message_id: UUID
    workspace_id: UUID
    purpose: str
    campaign_id: UUID | None
    enrollment_id: UUID | None
    mailbox_id: UUID
    address_id: UUID
    message_status: str
    dispatch_generation: int
    message_version: int
    retry_count: int
    retry_budget: int
    content_subject: str | None
    content_body_html: str | None
    content_digest: str | None
    renderer_version: int
    rendered_at: datetime | None
    frozen_destination: str | None
    frozen_sender_address: str | None
    frozen_sender_name: str | None
    rfc_message_id: str | None

    campaign_status: str | None
    campaign_planning_status: str | None

    enrollment_state: str | None

    canonical_address: str

    mailbox_workspace_id: UUID
    mailbox_provider: str
    mailbox_provider_account_id: str | None
    mailbox_connection_state: str
    mailbox_health_state: str
    mailbox_policy_state: str
    mailbox_policy_reason: str | None
    mailbox_circuit_state: str
    mailbox_blocked_until: datetime | None
    mailbox_current_connection_generation: int
    mailbox_original_address: str
    mailbox_sender_display_name: str | None
    raw: dict[str, Any]
    mailbox_pending_safety_count: int = 0
    # ADR-0020 resume fence. None for controlled-test sends (no campaign).
    message_schedule_generation: int | None = None
    campaign_schedule_generation: int | None = None
    # A follow-up sent in the same thread (ADR-0021): the message it replies to,
    # that message's Message-ID, every ancestor's Message-ID oldest first, and the
    # parent's provider thread. All empty for any other message.
    reply_to_message_id: UUID | None = None
    thread_in_reply_to: str | None = None
    thread_references: tuple[str, ...] = ()
    thread_provider_id: str | None = None


@dataclass(frozen=True)
class SendOutcome:
    """What actually happened, returned by SendingService.execute for the
    Celery task to log/emit metrics from -- never re-derived by the task
    itself from provider internals."""

    message_id: UUID
    # One of: SENT | FAILED | UNKNOWN_OUTCOME | RETRY_SCHEDULED | SKIPPED |
    # DEFERRED | NOOP
    outcome: str
    reason: str | None = None
    attempt_id: UUID | None = None
    provider_message_id: str | None = None
