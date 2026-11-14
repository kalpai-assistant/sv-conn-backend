"""
The admin audit log (SVC-423): read-only, admin role only, because ops shouldn't
review their own actions and support shouldn't see them. Entries are immutable:
there is no update or delete route. Runbook: ADMIN.md.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_roles
from app.schemas.admin_audit import AdminAuditFilters, AdminAuditListResponse
from app.services.admin_audit import AdminAuditService

router = APIRouter(prefix="/admin", tags=["Admin"])

_admin_dep = Depends(require_roles("admin"))


@router.get("/audit", response_model=AdminAuditListResponse, dependencies=[_admin_dep])
async def list_audit_log(
    admin_user_id: UUID | None = Query(None),
    action: str | None = Query(None),
    target_type: str | None = Query(None),
    target_id: str | None = Query(None),
    date_from: datetime | None = Query(
        None, description="ISO datetime, inclusive. No offset means UTC."
    ),
    date_to: datetime | None = Query(
        None, description="ISO datetime, exclusive. No offset means UTC."
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Every audited admin write, newest first."""
    return await AdminAuditService(db).list_rows(
        admin_user_id=admin_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
    )


@router.get("/audit/filters", response_model=AdminAuditFilters, dependencies=[_admin_dep])
async def get_audit_filters(db: AsyncSession = Depends(get_db)):
    """The admins, actions and target types present in the log, for dropdowns."""
    return await AdminAuditService(db).filters()
