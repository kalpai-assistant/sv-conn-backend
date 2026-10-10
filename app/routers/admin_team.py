"""
Admin team management (SVC-329): owners, their Pros (removed included), invite
history, force-remove, reassign, and the admin audit log.

Mounted under ``/admin`` like ``admin.py``, so ``AdminIPAllowlistMiddleware``
covers it, and every route requires the admin role. Paths follow the existing
admin ones: owners at ``/admin/providers/{user_id}``, Pros at
``/admin/pros/{professional_id}``.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_role
from app.models.user import User
from app.schemas.admin_team import (
    AdminActionRow,
    AdminInviteRow,
    AdminProDetail,
    AdminProviderRow,
    AdminTeamPro,
    ForceRemoveRequest,
    ForceRemoveResponse,
    ReassignRequest,
    ReassignResponse,
)
from app.services.admin_team_service import AdminTeamService

router = APIRouter(prefix="/admin", tags=["Admin"])

_admin_dep = Depends(require_role("admin"))


@router.get("/providers", response_model=list[AdminProviderRow], dependencies=[_admin_dep])
async def search_providers(
    q: str | None = Query(
        None,
        min_length=2,
        max_length=100,
        description="Matches phone (any part, spaces/dashes ignored), name or business name. "
        "Omit to list every owner.",
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Owners (users with a provider profile), newest first."""
    return await AdminTeamService(db).search_providers(q, page, per_page)


@router.get(
    "/providers/{sp_user_id}",
    response_model=AdminProviderRow,
    dependencies=[_admin_dep],
)
async def get_provider(sp_user_id: UUID, db: AsyncSession = Depends(get_db)):
    """One owner: the same row the search returns, for the owner page header.
    404 ``SP_NOT_FOUND`` for an unknown id or a user who isn't an owner."""
    return await AdminTeamService(db).provider(sp_user_id)


@router.get(
    "/providers/{sp_user_id}/team",
    response_model=list[AdminTeamPro],
    dependencies=[_admin_dep],
)
async def get_provider_team(sp_user_id: UUID, db: AsyncSession = Depends(get_db)):
    """Every Pro row under this owner, removed ones included (``is_active``):
    Pro-for-self first, then active, then by join date. 404 ``SP_NOT_FOUND``."""
    return await AdminTeamService(db).team(sp_user_id)


@router.get(
    "/providers/{sp_user_id}/invites",
    response_model=list[AdminInviteRow],
    dependencies=[_admin_dep],
)
async def get_provider_invites(sp_user_id: UUID, db: AsyncSession = Depends(get_db)):
    """The owner's invites, newest first, any status. No codes. Stale PENDING
    rows are saved as EXPIRED on the way out, as on the SP's own list."""
    invites = await AdminTeamService(db).invites(sp_user_id)
    await db.commit()
    return invites


@router.get("/pros/{professional_id}", response_model=AdminProDetail, dependencies=[_admin_dep])
async def get_pro(professional_id: UUID, db: AsyncSession = Depends(get_db)):
    """One Pro row, removed or not, and the owner it belongs to."""
    return await AdminTeamService(db).pro_detail(professional_id)


@router.post("/pros/{professional_id}/force-remove", response_model=ForceRemoveResponse)
async def force_remove_pro(
    professional_id: UUID,
    body: ForceRemoveRequest,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Remove a Pro from their owner's team (soft), e.g. when the owner is
    unresponsive. The same removal the owner does: 404 ``PRO_NOT_FOUND`` if
    missing or already removed, 400 ``CANNOT_REMOVE_SELF`` for an owner's own
    Pro-for-self, 409 ``PRO_HAS_ACTIVE_JOB`` while they're on a job. Audited."""
    self_reenabled = await AdminTeamService(db).force_remove(admin.id, professional_id, body.reason)
    await db.commit()
    return ForceRemoveResponse(professional_id=professional_id, self_reenabled=self_reenabled)


@router.post("/pros/{professional_id}/reassign", response_model=ReassignResponse)
async def reassign_pro(
    professional_id: UUID,
    body: ReassignRequest,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Move a person to another owner's team by admin authority: no invite or
    accept by either party (mergers, support). KYC is never copied: a row the
    target already had for them is reactivated with its own KYC, else a new row
    starts PENDING. Audited.

    Refused: 404 ``PRO_NOT_FOUND`` (missing or removed), 400
    ``CANNOT_REASSIGN_SELF``, 400 ``SAME_SERVICE_PROVIDER``, 404
    ``TARGET_SP_NOT_FOUND``, 409 ``TARGET_SP_INACTIVE``, 409
    ``TARGET_SP_KYC_INCOMPLETE``, 409 ``USER_INACTIVE``, 409
    ``ALREADY_ON_TEAM``, 409 ``PRO_HAS_ACTIVE_JOB``.
    """
    old_id, pro, reactivated, self_reenabled = await AdminTeamService(db).reassign(
        admin.id, professional_id, body.target_sp_user_id, body.reason
    )
    await db.commit()
    return ReassignResponse(
        old_professional_id=old_id,
        professional=pro,
        reactivated=reactivated,
        self_reenabled=self_reenabled,
    )


@router.get("/actions", response_model=list[AdminActionRow], dependencies=[_admin_dep])
async def list_admin_actions(
    target_type: Literal["user", "professional"] | None = Query(None),
    target_id: UUID | None = Query(None),
    sp_user_id: UUID | None = Query(
        None,
        description="An owner's history: actions on them or their team, including a "
        "Pro moved to or from them.",
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """The admin audit log, newest first."""
    return await AdminTeamService(db).actions(
        target_type=target_type,
        target_id=target_id,
        sp_user_id=sp_user_id,
        page=page,
        per_page=per_page,
    )
