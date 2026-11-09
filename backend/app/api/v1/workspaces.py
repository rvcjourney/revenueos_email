from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import (
    WorkspaceContext,
    get_current_user,
    get_db,
    get_workspace_context,
)
from app.core.auth import AuthenticatedPrincipal
from app.core.errors import AppError
from app.core.permissions import require_permission
from app.modules.unsubscribe.compliance import validate_compliance_section
from app.schemas.workspace import (
    MembershipOut,
    WorkspaceCreateIn,
    WorkspaceCreateOut,
    WorkspaceListItem,
    WorkspaceOut,
    WorkspaceUpdateIn,
)

router = APIRouter()


@router.get("", response_model=list[WorkspaceListItem])
def list_my_workspaces(
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[WorkspaceListItem]:
    rows = (
        db.execute(text("SELECT * FROM public.app_list_my_workspaces()"))
        .mappings()
        .all()
    )
    return [WorkspaceListItem(**row) for row in rows]


@router.post("", response_model=WorkspaceCreateOut, status_code=201)
def create_workspace(
    payload: WorkspaceCreateIn,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=200
    ),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceCreateOut:
    """Create (or, on retry, re-return) the caller's workspace + OWNER membership.

    Delegates the actual insert to app_bootstrap_workspace(), the one runtime
    path permitted to write workspaces/workspace_memberships (see
    supabase/migrations/0006_workspace_bootstrap.sql) -- this endpoint never
    inserts into those tables directly. The Idempotency-Key header is the
    stable request key a double-click or retry must reuse.
    """
    row = (
        db.execute(
            text("SELECT * FROM public.app_bootstrap_workspace(:name, :request_key)"),
            {"name": payload.name, "request_key": idempotency_key},
        )
        .mappings()
        .first()
    )
    if row is None:  # pragma: no cover - function always returns one row or raises
        raise AppError("internal_error", "Workspace bootstrap failed", status_code=500)

    if row["bootstrapped"]:
        db.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(row["workspace_id"])},
        )
        db.execute(
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
                "workspace_id": str(row["workspace_id"]),
                "actor_id": str(principal.user_id),
                "after_state": json.dumps({"name": row["workspace_name"]}),
            },
        )

    return WorkspaceCreateOut(
        id=row["workspace_id"],
        name=row["workspace_name"],
        status=row["workspace_status"],
        role_code=row["role_code"],
        membership_id=row["membership_id"],
        membership_version=row["membership_version"],
    )


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> WorkspaceOut:
    row = (
        db.execute(
            text(
                "SELECT id, name, status, defaults, version "
                "FROM workspaces WHERE id = :id"
            ),
            {"id": str(context.workspace_id)},
        )
        .mappings()
        .first()
    )
    if row is None:  # pragma: no cover - RLS already proved membership above
        raise AppError("not_found", "Workspace not found", status_code=404)
    return WorkspaceOut(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        defaults=row["defaults"],
        version=row["version"],
        role_code=context.role_code,
    )


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
def update_workspace(
    payload: WorkspaceUpdateIn,
    context: WorkspaceContext = Depends(require_permission("workspace.manage")),
    db: Session = Depends(get_db),
) -> WorkspaceOut:
    """Update only name/defaults, gated by the workspace.manage capability.

    Optimistic concurrency: the row only updates if it still matches
    expected_version; PostgreSQL owns the actual version increment via the
    app_touch_row trigger, this never increments it independently.
    """
    if payload.name is None and payload.defaults is None:
        raise AppError(
            "validation_error", "No mutable fields supplied", status_code=422
        )

    set_clauses: list[str] = []
    params: dict[str, object] = {
        "id": str(context.workspace_id),
        "expected_version": payload.expected_version,
    }
    if payload.name is not None:
        set_clauses.append("name = :name")
        params["name"] = payload.name
    if payload.defaults is not None:
        defaults = dict(payload.defaults)
        if "compliance" in defaults:
            defaults["compliance"] = validate_compliance_section(defaults["compliance"])
        set_clauses.append("defaults = CAST(:defaults AS jsonb)")
        params["defaults"] = json.dumps(defaults)

    row = (
        db.execute(
            text(
                f"UPDATE workspaces SET {', '.join(set_clauses)} "
                "WHERE id = :id AND version = :expected_version "
                "RETURNING id, name, status, defaults, version"
            ),
            params,
        )
        .mappings()
        .first()
    )
    if row is None:
        exists = db.execute(
            text("SELECT 1 FROM workspaces WHERE id = :id"),
            {"id": str(context.workspace_id)},
        ).first()
        if exists is None:
            raise AppError("not_found", "Workspace not found", status_code=404)
        raise AppError(
            "version_conflict",
            "Workspace was modified by another request; reload and retry",
            status_code=409,
        )
    return WorkspaceOut(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        defaults=row["defaults"],
        version=row["version"],
        role_code=context.role_code,
    )


@router.get("/{workspace_id}/members", response_model=list[MembershipOut])
def list_workspace_members(
    context: WorkspaceContext = Depends(get_workspace_context),
    db: Session = Depends(get_db),
) -> list[MembershipOut]:
    rows = (
        db.execute(text("SELECT * FROM public.app_list_workspace_members()"))
        .mappings()
        .all()
    )
    return [MembershipOut(**row) for row in rows]
