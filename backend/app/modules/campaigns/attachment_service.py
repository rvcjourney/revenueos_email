from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.orm import Session

from app.api.deps import WorkspaceContext
from app.core.config import Settings
from app.core.errors import AppError
from app.core.permissions import has_permission
from app.modules.campaigns.attachments import (
    MAX_ATTACHMENTS,
    MAX_INLINE_IMAGES,
    MAX_TOTAL_BYTES,
    extract_cid_references,
    storage_safe_name,
    validate_upload,
)
from app.modules.campaigns.repository import CampaignRepository
from app.modules.campaigns.schemas import StepAttachmentOut, StepAttachmentUrlOut
from app.modules.imports.storage import (
    StorageError,
    StorageObjectMissingError,
    StorageUnavailableError,
    SupabaseStorageClient,
)

logger = logging.getLogger(__name__)

# Long enough that images in an open editor keep displaying; the URL is a
# private capability for one object and expires on its own.
SIGNED_URL_TTL_SECONDS = 1800


def default_storage_client() -> SupabaseStorageClient:
    settings = Settings.current()
    return SupabaseStorageClient(settings, bucket=settings.supabase_attachments_bucket)


def to_attachment_out(row: RowMapping | dict[str, Any]) -> StepAttachmentOut:
    return StepAttachmentOut(
        id=row["id"],
        step_id=row["step_id"],
        filename=row["filename"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        disposition=row["disposition"],
        content_id=row["content_id"],
        created_at=row["created_at"],
    )


def _storage_error(exc: StorageError) -> AppError:
    """Client-safe classification; never leaks storage internals."""
    if isinstance(exc, StorageUnavailableError):
        return AppError(
            "attachment_storage_unavailable",
            "File storage is temporarily unavailable. Try again shortly.",
            status_code=503,
        )
    if isinstance(exc, StorageObjectMissingError):
        return AppError(
            "attachment_missing", "The file could not be found", status_code=404
        )
    return AppError(
        "attachment_storage_error",
        "The file could not be processed",
        status_code=500,
    )


class StepAttachmentService:
    """Upload, list, delete and preview the files attached to an email step.

    Files are stored in a private bucket under
    {workspace}/{campaign}/{step}/{random}-{name}; this service is the only code
    that talks to storage for them, always with the server-side key. Every
    lookup is scoped by workspace and campaign, so another tenant's step or
    attachment id resolves to 404.
    """

    def __init__(
        self,
        session: Session,
        storage_factory: Callable[[], Any] = default_storage_client,
    ) -> None:
        self.session = session
        self.repo = CampaignRepository(session)
        self._storage_factory = storage_factory

    def _draft_step(
        self, context: WorkspaceContext, campaign_id: UUID, step_id: UUID
    ) -> RowMapping:
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
                "Only an EMAIL step can have attachments",
                status_code=422,
            )
        return campaign

    def _require_editable(
        self, context: WorkspaceContext, campaign: RowMapping
    ) -> bool:
        """True when this is a paused-campaign edit (ADR-0020). Attachments are
        read live and checksum-verified at send time, so changing them needs no
        message rewrite; only STANDARD campaigns and campaigns.execute holders."""
        if campaign["status"] == "DRAFT":
            return False
        if campaign["status"] == "PAUSED" and campaign["activation_id"] is not None:
            if not has_permission(context.role_code, "campaigns.execute"):
                raise AppError(
                    "forbidden",
                    "Only a manager can change attachments of a paused campaign",
                    status_code=403,
                )
            if campaign.get("campaign_type") == "HYPER_PERSONALIZED":
                raise AppError(
                    "state_conflict",
                    "The emails of a personalized campaign can't be changed once "
                    "it has started",
                    status_code=409,
                )
            return True
        raise AppError(
            "state_conflict",
            "Attachments can only be changed while the campaign is DRAFT or paused",
            status_code=409,
        )

    def _audit_paused(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        step_id: UUID,
        operation: str,
    ) -> None:
        self.repo.record_campaign_audit(
            workspace_id=context.workspace_id,
            actor_id=context.user_id,
            action="campaign.sequence_edited_while_paused",
            campaign_id=campaign_id,
            after_state={"operation": operation, "step_id": str(step_id), "fields": []},
        )

    def list_attachments(
        self, context: WorkspaceContext, campaign_id: UUID, step_id: UUID
    ) -> list[StepAttachmentOut]:
        self._draft_step(context, campaign_id, step_id)
        rows = self.repo.list_step_attachments(
            workspace_id=context.workspace_id, step_id=step_id
        )
        return [to_attachment_out(r) for r in rows]

    def upload(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        step_id: UUID,
        *,
        filename: str | None,
        data: bytes,
        disposition: str,
    ) -> tuple[StepAttachmentOut, bool]:
        """Returns (attachment, created). Uploading the same file again returns
        the existing attachment with created=False."""
        campaign = self._draft_step(context, campaign_id, step_id)
        paused = self._require_editable(context, campaign)
        if disposition not in ("ATTACHMENT", "INLINE"):
            raise AppError(
                "validation_error", "Invalid attachment kind", status_code=422
            )
        validated = validate_upload(
            filename=filename, data=data, disposition=disposition
        )

        existing = self.repo.list_step_attachments(
            workspace_id=context.workspace_id, step_id=step_id
        )
        duplicate = next(
            (
                r
                for r in existing
                if r["sha256"] == validated.sha256 and r["disposition"] == disposition
            ),
            None,
        )
        if duplicate is not None:
            return to_attachment_out(duplicate), False

        attachments = [r for r in existing if r["disposition"] == "ATTACHMENT"]
        inline = [r for r in existing if r["disposition"] == "INLINE"]
        if disposition == "ATTACHMENT" and len(attachments) >= MAX_ATTACHMENTS:
            raise AppError(
                "attachment_limit",
                f"A step can have at most {MAX_ATTACHMENTS} attachments",
                status_code=422,
            )
        if disposition == "INLINE" and len(inline) >= MAX_INLINE_IMAGES:
            raise AppError(
                "attachment_limit",
                f"A step can have at most {MAX_INLINE_IMAGES} images",
                status_code=422,
            )
        # Shared objects (copies) are counted once per row, which is the size the
        # message will actually carry.
        total_after = sum(r["size_bytes"] for r in existing) + validated.size_bytes
        if total_after > MAX_TOTAL_BYTES:
            raise AppError(
                "attachment_limit",
                "Attachments on one step can total at most 2.5 MB",
                status_code=422,
            )

        step = self.repo.get_step(workspace_id=context.workspace_id, step_id=step_id)
        assert step is not None
        storage_key = (
            f"{context.workspace_id}/{campaign_id}/{step_id}/"
            f"{uuid.uuid4().hex}-{storage_safe_name(validated.filename)}"
        )
        storage = self._storage_factory()
        try:
            storage.upload_object(
                storage_key, content_type=validated.content_type, data=data
            )
        except StorageError as exc:
            raise _storage_error(exc) from exc

        row = self.repo.insert_attachment(
            workspace_id=context.workspace_id,
            campaign_id=campaign_id,
            sequence_id=UUID(str(step["sequence_id"])),
            step_id=step_id,
            disposition=disposition,
            content_id=secrets.token_urlsafe(12),
            storage_key=storage_key,
            filename=validated.filename,
            content_type=validated.content_type,
            size_bytes=validated.size_bytes,
            sha256=validated.sha256,
            created_by=context.user_id,
        )
        if row is None:
            # Lost a race with an identical upload: keep theirs, drop our object.
            self._delete_object_quietly(storage, storage_key)
            winner = self.repo.find_attachment_by_content(
                workspace_id=context.workspace_id,
                step_id=step_id,
                sha256=validated.sha256,
                disposition=disposition,
            )
            if winner is None:
                raise AppError(
                    "conflict", "The file could not be attached", status_code=409
                )
            return to_attachment_out(winner), False
        if paused:
            self._audit_paused(context, campaign_id, step_id, "attachment_added")
        return to_attachment_out(row), True

    def delete(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        step_id: UUID,
        attachment_id: UUID,
    ) -> None:
        campaign = self._draft_step(context, campaign_id, step_id)
        paused = self._require_editable(context, campaign)
        row = self.repo.get_attachment(
            workspace_id=context.workspace_id,
            step_id=step_id,
            attachment_id=attachment_id,
        )
        if row is None:
            raise AppError("not_found", "Attachment not found", status_code=404)
        if paused and row["disposition"] == "INLINE":
            step = self.repo.get_step(
                workspace_id=context.workspace_id, step_id=step_id
            )
            if step is not None and row["content_id"] in extract_cid_references(
                step["email_body_html"]
            ):
                raise AppError(
                    "validation_error",
                    "This image is still used in the email. Remove it from the "
                    "email text first.",
                    status_code=422,
                )
        self.repo.delete_attachment(
            workspace_id=context.workspace_id, attachment_id=attachment_id
        )
        # Only remove the object when no other row (a copied step/campaign)
        # still points at it. Best-effort: a failure leaves an orphan object,
        # which is harmless and never blocks the user.
        remaining = self.repo.count_storage_key_references(
            workspace_id=context.workspace_id, storage_key=row["storage_key"]
        )
        if remaining == 0:
            self._delete_object_quietly(self._storage_factory(), row["storage_key"])
        if paused:
            self._audit_paused(context, campaign_id, step_id, "attachment_removed")

    def signed_url(
        self,
        context: WorkspaceContext,
        campaign_id: UUID,
        step_id: UUID,
        attachment_id: UUID,
    ) -> StepAttachmentUrlOut:
        self._draft_step(context, campaign_id, step_id)
        row = self.repo.get_attachment(
            workspace_id=context.workspace_id,
            step_id=step_id,
            attachment_id=attachment_id,
        )
        if row is None:
            raise AppError("not_found", "Attachment not found", status_code=404)
        try:
            url = self._storage_factory().create_signed_url(
                row["storage_key"], expires_in=SIGNED_URL_TTL_SECONDS
            )
        except StorageError as exc:
            raise _storage_error(exc) from exc
        return StepAttachmentUrlOut(url=url, expires_in=SIGNED_URL_TTL_SECONDS)

    @staticmethod
    def _delete_object_quietly(storage: Any, key: str) -> None:
        try:
            storage.delete_object(key)
        except StorageError:
            logger.warning("Could not delete an unreferenced attachment object")
