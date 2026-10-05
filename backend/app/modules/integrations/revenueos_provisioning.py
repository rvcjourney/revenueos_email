"""RevenueOS user provisioning (ADR-0020): one request that gives a person an
account and a workspace, through the workspace bootstrap command the UI already
uses and the owner-adds-member command of migration 0038.

The RevenueOS acting user owns every workspace created here, so the campaign
intake route (ADR-0019) can reach it at once. No login password is handled
here. An optional SMTP block connects or updates the workspace's sending
mailbox through the same MailboxService the Mailboxes page uses.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.context import set_transaction_context
from app.modules.campaigns.repository import CampaignRepository
from app.modules.integrations.supabase_auth_admin import EnsuredUser
from app.modules.mailboxes.repository import MailboxRepository
from app.modules.mailboxes.schemas import SmtpConnectRequest, SmtpUpdateRequest
from app.modules.mailboxes.service import MailboxService

logger = logging.getLogger(__name__)

PROVISION_OPERATION = "revenueos.user_provision"


class RevenueOSUserProvisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # RevenueOS's own id for this person/client. It is the idempotency key: the
    # same reference never creates a second workspace.
    reference: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    # Defaults to "<email> Workspace".
    workspace_name: str | None = Field(default=None, min_length=1, max_length=200)
    # OWNER is never granted: the acting user owns the workspace.
    role: Literal["ADMIN", "MANAGER", "MEMBER", "VIEWER"] = "ADMIN"
    # The sending mailbox, as entered on the Mailboxes > Connect SMTP form.
    # Omitted: mailboxes are left alone. It is not part of the reference's
    # identity, so the same reference may send new details later.
    smtp: SmtpConnectRequest | None = None

    @field_validator("reference")
    @classmethod
    def _reference(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reference must not be blank")
        return cleaned

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        cleaned = value.strip().lower()
        local, _, domain = cleaned.partition("@")
        if (
            not local
            or "." not in domain
            or "@" in domain
            or any(char.isspace() for char in cleaned)
        ):
            raise ValueError("a valid email address is required")
        return cleaned

    @field_validator("workspace_name")
    @classmethod
    def _workspace_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("workspace_name must not be blank")
        return cleaned

    @property
    def resolved_workspace_name(self) -> str:
        return self.workspace_name or f"{self.email} Workspace"[:200]


class RevenueOSUserProvisionOut(BaseModel):
    status: Literal["provisioned"] = "provisioned"
    reference: str
    user_id: UUID
    email: str
    workspace_id: UUID
    workspace_name: str
    role: str
    # True when this request created the account; False when it already existed.
    user_created: bool
    # True when this reference was already provisioned and nothing new was created.
    duplicate: bool
    # Set only when the request carried an `smtp` block.
    mailbox_id: UUID | None = None
    mailbox_status: Literal["connected", "updated", "failed"] | None = None
    # Why the mailbox was refused; the account and workspace still stand.
    mailbox_error: str | None = None


def _payload_hash(payload: RevenueOSUserProvisionIn) -> str:
    encoded = json.dumps(
        {
            "email": payload.email,
            "role": payload.role,
            "workspace_name": payload.resolved_workspace_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bootstrap_request_key(reference: str) -> str:
    # The bootstrap ledger caps its key at 200 characters, as long as a
    # reference may be, so the reference is digested rather than prefixed.
    return "revenueos.user:" + hashlib.sha256(reference.encode("utf-8")).hexdigest()


class RevenueOSProvisioningService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = CampaignRepository(session)

    def provision(
        self,
        *,
        actor_id: UUID,
        payload: RevenueOSUserProvisionIn,
        user: EnsuredUser,
    ) -> RevenueOSUserProvisionOut:
        result = self._provision_account(actor_id, payload, user)
        if payload.smtp is not None:
            self._apply_smtp(actor_id, result, payload.smtp)
        return result

    def _provision_account(
        self,
        actor_id: UUID,
        payload: RevenueOSUserProvisionIn,
        user: EnsuredUser,
    ) -> RevenueOSUserProvisionOut:
        set_transaction_context(self.session, user_id=actor_id)
        self._ensure_profile(actor_id)

        workspace = self._bootstrap_workspace(payload)
        workspace_id = UUID(str(workspace["workspace_id"]))
        set_transaction_context(self.session, workspace_id=workspace_id)
        if workspace["bootstrapped"]:
            self._audit_bootstrap(actor_id, workspace_id, workspace["workspace_name"])

        payload_hash = _payload_hash(payload)
        receipt = self.repo.get_command_receipt(
            workspace_id=workspace_id,
            actor_id=actor_id,
            operation=PROVISION_OPERATION,
            request_key=payload.reference,
        )
        if receipt is not None:
            return self._replay(payload, user, workspace, receipt, payload_hash)

        try:
            receipt = self.repo.insert_pending_command_receipt(
                workspace_id=workspace_id,
                actor_id=actor_id,
                operation=PROVISION_OPERATION,
                request_key=payload.reference,
                payload_hash=payload_hash,
                resource_type="workspace",
                resource_id=workspace_id,
            )
        except IntegrityError as exc:
            # command_receipts_identity_key: a simultaneous request with the
            # same reference won; this transaction can only roll back.
            raise AppError(
                "conflict",
                "A request with this reference is already being processed. "
                "Please retry shortly.",
                status_code=409,
            ) from exc

        role = self._member_role(user.user_id)
        if role is None:
            role = self._add_member(actor_id, workspace_id, user.user_id, payload)

        self.repo.complete_command_receipt(
            workspace_id=workspace_id,
            receipt_id=UUID(str(receipt["id"])),
            response_version=int(workspace["membership_version"]),
        )
        logger.info(
            "RevenueOS user provisioned",
            extra={
                "workspace_id": str(workspace_id),
                "user_id": str(user.user_id),
                "user_created": user.created,
                "role_code": role,
            },
        )
        return RevenueOSUserProvisionOut(
            reference=payload.reference,
            user_id=user.user_id,
            email=payload.email,
            workspace_id=workspace_id,
            workspace_name=workspace["workspace_name"],
            role=role,
            user_created=user.created,
            duplicate=False,
        )

    def _replay(
        self,
        payload: RevenueOSUserProvisionIn,
        user: EnsuredUser,
        workspace: RowMapping,
        receipt: RowMapping,
        payload_hash: str,
    ) -> RevenueOSUserProvisionOut:
        if receipt["payload_hash"] != payload_hash:
            raise AppError(
                "conflict",
                "This reference was already used with a different email, role or "
                "workspace name",
                status_code=409,
            )
        role = self._member_role(user.user_id)
        if receipt["status"] != "COMPLETED" or role is None:
            # The receipt commits together with the membership, so the access
            # was removed afterwards. Never grant it again silently under a
            # reference that was already acknowledged.
            raise AppError(
                "conflict",
                "This reference was already used and the user no longer has "
                "access to its workspace",
                status_code=409,
            )
        return RevenueOSUserProvisionOut(
            reference=payload.reference,
            user_id=user.user_id,
            email=payload.email,
            workspace_id=UUID(str(workspace["workspace_id"])),
            workspace_name=workspace["workspace_name"],
            role=role,
            user_created=False,
            duplicate=True,
        )

    def _apply_smtp(
        self,
        actor_id: UUID,
        result: RevenueOSUserProvisionOut,
        smtp: SmtpConnectRequest,
    ) -> None:
        """Connect the mailbox for this sender address, or update it when the
        workspace already has one. Runs for a repeated reference too: that is
        how RevenueOS changes a user's SMTP details."""
        mailboxes = MailboxService(MailboxRepository(self.session))
        address = smtp.email_address.strip().lower()
        existing = next(
            (
                row
                for row in mailboxes.repo.list_mailboxes(result.workspace_id)
                if row["provider"] == "SMTP"
                and str(row["original_address"]).lower() == address
                and row["connection_state"] != "DISCONNECTED"
            ),
            None,
        )
        if existing is not None:
            result.mailbox_id = UUID(str(existing["id"]))
        try:
            # A savepoint, so a refused mailbox (wrong password, unreachable
            # host) undoes only itself and the account and workspace commit.
            with self.session.begin_nested():
                if existing is None:
                    result.mailbox_id = mailboxes.connect_smtp_mailbox(
                        result.workspace_id, actor_id, smtp
                    ).mailbox_id
                    result.mailbox_status = "connected"
                else:
                    mailboxes.update_smtp_mailbox(
                        result.workspace_id,
                        actor_id,
                        UUID(str(existing["id"])),
                        SmtpUpdateRequest(**smtp.model_dump(exclude={"email_address"})),
                    )
                    result.mailbox_status = "updated"
        except AppError as exc:
            result.mailbox_status = "failed"
            result.mailbox_error = exc.message
        except IntegrityError:
            # The same host, port and username already belong to another
            # mailbox: a different sender address here
            # (mailboxes_provider_account_key) or any mailbox of another
            # workspace (mailboxes_provider_account_global_key), which row-level
            # security hides from the service's own pre-check.
            result.mailbox_status = "failed"
            result.mailbox_error = (
                "This SMTP login is already connected to another mailbox"
            )
        logger.info(
            "RevenueOS mailbox applied",
            extra={
                "workspace_id": str(result.workspace_id),
                "mailbox_id": str(result.mailbox_id) if result.mailbox_id else None,
                "mailbox_status": result.mailbox_status,
            },
        )

    def _ensure_profile(self, user_id: UUID) -> None:
        # Self-only INSERT (profiles_api_insert): the transaction's user context
        # must already be `user_id`.
        self.session.execute(
            text("INSERT INTO profiles (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"),
            {"id": str(user_id)},
        )

    def _bootstrap_workspace(self, payload: RevenueOSUserProvisionIn) -> RowMapping:
        row = (
            self.session.execute(
                text(
                    "SELECT * FROM public.app_bootstrap_workspace(:name, :request_key)"
                ),
                {
                    "name": payload.resolved_workspace_name,
                    "request_key": _bootstrap_request_key(payload.reference),
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            # A replayed key returns no row once the acting user is no longer
            # the Owner of the workspace it created.
            raise AppError(
                "conflict",
                "This reference was already used and its workspace is no longer "
                "available",
                status_code=409,
            )
        return row

    def _audit_bootstrap(self, actor_id: UUID, workspace_id: UUID, name: str) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO audit_events
                    (workspace_id, actor_kind, actor_id, action, target_type,
                     target_id, after_state)
                VALUES
                    (:workspace_id, 'USER', :actor_id, 'workspace.bootstrap',
                     'workspace', :workspace_id, CAST(:after_state AS jsonb))
                """
            ),
            {
                "workspace_id": str(workspace_id),
                "actor_id": str(actor_id),
                "after_state": json.dumps({"name": name, "source": "revenueos"}),
            },
        )

    def _member_role(self, user_id: UUID) -> str | None:
        rows = (
            self.session.execute(
                text(
                    "SELECT user_id, role_code, status "
                    "FROM public.app_list_workspace_members()"
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            if str(row["user_id"]) == str(user_id) and row["status"] == "ACTIVE":
                return str(row["role_code"])
        return None

    def _add_member(
        self,
        actor_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        payload: RevenueOSUserProvisionIn,
    ) -> str:
        # The membership needs the person's profile, and a profile can only be
        # inserted by its own user, so the context is theirs for that one row.
        set_transaction_context(self.session, user_id=user_id)
        self._ensure_profile(user_id)
        set_transaction_context(self.session, user_id=actor_id)

        row = (
            self.session.execute(
                text(
                    "SELECT * FROM public.app_provision_workspace_member("
                    ":user_id, :role_code)"
                ),
                {"user_id": str(user_id), "role_code": payload.role},
            )
            .mappings()
            .one()
        )
        if row["created"]:
            self.session.execute(
                text(
                    """
                    INSERT INTO audit_events
                        (workspace_id, actor_kind, actor_id, action, target_type,
                         target_id, after_state)
                    VALUES
                        (:workspace_id, 'USER', :actor_id, 'membership.provisioned',
                         'workspace_membership', :target_id,
                         CAST(:after_state AS jsonb))
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "actor_id": str(actor_id),
                    "target_id": str(row["membership_id"]),
                    "after_state": json.dumps(
                        {
                            "user_id": str(user_id),
                            "role_code": row["role_code"],
                            "source": "revenueos",
                        }
                    ),
                },
            )
        return str(row["role_code"])
