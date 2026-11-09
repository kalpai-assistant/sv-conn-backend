"""SVC-429: the admin coupon surface. Roles in ADMIN.md.

Admin and ops write, support reads. ``{code}`` is case-insensitive. Every write
that changes something is audited (``coupon.create`` / ``update`` / ``deactivate``
/ ``activate``). There is no delete, and no duplicate endpoint.
"""

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_roles
from app.models.user import User
from app.schemas.admin_coupons import (
    AdminCouponCreate,
    AdminCouponDetail,
    AdminCouponListResponse,
    AdminCouponRow,
    AdminCouponUpdate,
    CouponStatus,
)
from app.services.admin_audit import audit_actor
from app.services.admin_coupon_service import AdminCouponService

router = APIRouter(prefix="/admin/coupons", tags=["Admin"])

_any_admin_role_dep = Depends(require_roles("admin", "ops", "support"))


@router.get("", response_model=AdminCouponListResponse, dependencies=[_any_admin_role_dep])
async def list_coupons(
    status_filter: CouponStatus | None = Query(
        None,
        alias="status",
        description="inactive, expired, exhausted (global usage_limit reached), scheduled "
        "(valid_from in the future) or active. Each coupon has exactly one.",
    ),
    q: str | None = Query(None, max_length=32, description="Any part of the code, any case"),
    personal: bool | None = Query(
        None, description="Personal (referral) coupons only (true) or global only (false)"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Every coupon, active or not, newest first."""
    return await AdminCouponService(db).list_coupons(
        status_filter=status_filter, q=q, personal=personal, page=page, per_page=per_page
    )


@router.post("", response_model=AdminCouponRow, status_code=status.HTTP_201_CREATED)
async def create_coupon(
    body: AdminCouponCreate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """A new global coupon. 409 ``COUPON_CODE_TAKEN`` if the code exists in any
    case; a refused value is a 422 ``COUPON_INVALID`` naming ``details.field``."""
    return await AdminCouponService(db).create(audit_actor(request, admin), body)


@router.get("/{code}", response_model=AdminCouponDetail, dependencies=[_any_admin_role_dep])
async def get_coupon(code: str, db: AsyncSession = Depends(get_db)):
    """The coupon with its usage: total discount given and the latest 20
    redemptions. 404 ``COUPON_NOT_FOUND``."""
    return await AdminCouponService(db).detail(code)


@router.patch("/{code}", response_model=AdminCouponRow)
async def update_coupon(
    code: str,
    body: AdminCouponUpdate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Change any editable field, validated against the row as it would be saved.
    ``usage_limit`` can't go below the uses so far. A save that changes nothing
    is a 200 with no audit row."""
    return await AdminCouponService(db).update(audit_actor(request, admin), code, body)


@router.post("/{code}/deactivate", response_model=AdminCouponRow)
async def deactivate_coupon(
    code: str,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Switch the coupon off. Already off: 200, no audit row."""
    return await AdminCouponService(db).set_active(audit_actor(request, admin), code, False)


@router.post("/{code}/activate", response_model=AdminCouponRow)
async def activate_coupon(
    code: str,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Switch the coupon back on. Already on: 200, no audit row."""
    return await AdminCouponService(db).set_active(audit_actor(request, admin), code, True)
