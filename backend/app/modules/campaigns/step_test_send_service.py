from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.errors import AppError
from app.modules.campaigns.attachment_service import default_storage_client
from app.modules.campaigns.message_rendering import render_step_content, reply_subject
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import StepTestSendIn
from app.modules.imports.storage import (
    StorageError,
    StorageObjectMissingError,
    StorageUnavailableError,
)
from app.modules.mailboxes.providers.base import EnvelopeAttachment
from app.modules.mailboxes.repository import MailboxRepository
from app.modules.mailboxes.schemas import MailboxTestSendResult
from app.modules.mailboxes.service import MailboxService
from app.modules.templates.rendering import DEFAULT_SAMPLE_DATA
from app.modules.templates.sanitizer import sanitize_email_html
from app.modules.templates.variables import validate_template_content

TEST_SUBJECT_PREFIX = "[Test] "
_MAX_SUBJECT_LENGTH = 500


class StepTestSendService:
    """Sends one real, rendered copy of a sequence step to an address the caller
    explicitly confirms.

    It renders with the same `render_step_content` the campaign uses, then hands
    the result to MailboxService.send_controlled_test_email, so mailbox state,
    recipient validation, suppression, authorization and attempt bookkeeping are
    the existing controlled-test pipeline rather than a second implementation.
    The message is a CONTROLLED_TEST: it never touches campaign enrollments,
    audience or progression.
    """

    def __init__(
        self,
        session: Session,
        storage_factory: Callable[[], Any] = default_storage_client,
    ) -> None:
        self.session = session
        self.repo = CampaignRepository(session)
        self._storage_factory = storage_factory

    def _load_attachments(
        self, context: WorkspaceContext, step_id: UUID
    ) -> tuple[EnvelopeAttachment, ...]:
        rows = self.repo.list_step_attachments(
            workspace_id=context.workspace_id, step_id=step_id
        )
        if not rows:
            return ()
        storage = self._storage_factory()
        loaded: list[EnvelopeAttachment] = []
        for row in rows:
            try:
                data = storage.download_object(row["storage_key"])
            except StorageObjectMissingError:
                raise AppError(
                    "attachment_missing",
                    f"The file \"{row['filename']}\" is missing from storage. "
                    "Remove it and upload it again.",
                    status_code=409,
                ) from None
            except StorageUnavailableError:
                raise AppError(
                    "attachment_storage_unavailable",
                    "File storage is temporarily unavailable. Try again shortly.",
                    status_code=503,
                ) from None
            except StorageError:
                raise AppError(
                    "attachment_storage_error",
                    "The attachments could not be read",
                    status_code=500,
                ) from None
            if (
                len(data) != row["size_bytes"]
                or hashlib.sha256(data).hexdigest() != row["sha256"]
            ):
                raise AppError(
                    "attachment_corrupt",
                    f"The file \"{row['filename']}\" failed its integrity check. "
                    "Remove it and upload it again.",
                    status_code=409,
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

    def _previous_subject(self, context: WorkspaceContext, step: Any) -> str | None:
        """The subject of the nearest earlier email that has its own (the one a
        chain of same-thread replies starts from), unrendered."""
        steps = self.repo.list_steps_ordered(
            workspace_id=context.workspace_id, sequence_id=step["sequence_id"]
        )
        earlier = [
            s
            for s in steps
            if s["kind"] == "EMAIL"
            and s["position"] < step["position"]
            and not s.get("email_same_thread")
        ]
        return earlier[-1]["email_subject"] if earlier else None

    def send(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        step_id: UUID,
        payload: StepTestSendIn,
        request_key: str,
    ) -> MailboxTestSendResult:
        if not payload.confirm_recipient:
            raise AppError(
                "validation_error",
                "Confirm the recipient before sending a test email",
                status_code=422,
            )

        # All reads through the campaign repository come first: the mailbox
        # service switches the DB role for its own writes.
        campaign = self.repo.get_campaign(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        if campaign is None:
            raise AppError("not_found", "Campaign not found", status_code=404)

        step = self.repo.get_step(workspace_id=context.workspace_id, step_id=step_id)
        if step is None or str(step["campaign_id"]) != str(campaign_id):
            raise AppError("not_found", "Sequence step not found", status_code=404)
        if step["kind"] != "EMAIL":
            raise AppError(
                "validation_error",
                "Only an EMAIL step can be test-sent",
                status_code=422,
            )

        assigned = self.repo.list_campaign_mailboxes(
            workspace_id=context.workspace_id, campaign_id=campaign_id
        )
        wanted = str(payload.mailbox_id)
        if not any(str(row["mailbox_id"]) == wanted for row in assigned):
            raise AppError(
                "validation_error",
                "That mailbox is not assigned to this campaign",
                status_code=422,
            )

        if payload.audience_member_id is not None:
            member = self.repo.get_accepted_audience_member(
                workspace_id=context.workspace_id,
                campaign_id=campaign_id,
                member_id=payload.audience_member_id,
            )
            if member is None:
                raise AppError(
                    "not_found", "Prospect not found in this campaign", status_code=404
                )
            variables = dict(member["frozen_variables"] or {})
        else:
            variables = dict(DEFAULT_SAMPLE_DATA)

        # Unsaved editor content wins over the saved step, so what is tested is
        # what the user is looking at. Draft HTML is sanitized exactly as it
        # would be on save; saved content is already stored in its final form.
        subject = (
            payload.email_subject
            if payload.email_subject is not None
            else step["email_subject"]
        )
        if payload.email_body_html is not None:
            body_html = sanitize_email_html(payload.email_body_html)
        else:
            body_html = step["email_body_html"] or ""
        preheader = (
            payload.email_preheader
            if payload.email_preheader is not None
            else step["email_preheader"]
        )
        preheader = (preheader or "").strip() or None
        if step.get("email_same_thread"):
            # Sent as a reply in a real campaign (ADR-0021): show the subject it will
            # carry. A test is a one-off, so it gets no threading headers.
            subject = self._previous_subject(context, step)
        if not subject or not subject.strip():
            raise AppError(
                "validation_error",
                "Add a subject before sending a test email",
                status_code=422,
            )
        validate_template_content(subject, body_html, preheader)
        # Fetched (and verified) before anything is sent, so a missing file is a
        # clear error here instead of a test email without its attachment.
        attachments = self._load_attachments(context, step_id)

        rendered = render_step_content(
            subject=subject.strip(),
            body_html=body_html,
            preheader=preheader,
            frozen_variables=variables,
            renderer_version=1,
        )
        shown_subject = (
            reply_subject(rendered.subject)
            if step.get("email_same_thread")
            else rendered.subject
        )
        test_subject = f"{TEST_SUBJECT_PREFIX}{shown_subject}"[:_MAX_SUBJECT_LENGTH]

        # Binds the idempotency receipt to this step and this exact content, so
        # the same key is never reused for a different message.
        fingerprint = hashlib.sha256(
            f"{step_id}\x00{test_subject}\x00{rendered.body_html}".encode()
        ).hexdigest()

        mailbox_service = MailboxService(MailboxRepository(self.session))
        return mailbox_service.send_controlled_test_email(
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            mailbox_id=payload.mailbox_id,
            recipient_email=payload.recipient_email,
            request_key=request_key,
            subject=test_subject,
            body_html=rendered.body_html,
            operation="campaign.step_test_send",
            payload_fingerprint=fingerprint,
            attachments=attachments,
        )
