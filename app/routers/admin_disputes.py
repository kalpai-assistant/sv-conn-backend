"""SVC-431: customer disputes and their refunds. Roles in ADMIN.md.

Admin, ops and support read and write. Support's refunds are capped at
``pricing_config.support_refund_limit`` rupees (403 ``REFUND_OVER_SUPPORT_LIMIT``);
admin and ops are uncapped. Every write is audited (``dispute.create`` /
``review`` / ``resolve``). A dispute refund never changes the provider's payout.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_roles
from app.models.user import User
from app.schemas.admin_disputes import (
    AdminDisputeCreate,
    AdminDisputeDetail,
    AdminDisputeListResponse,
    AdminDisputeResolve,
    AdminDisputeRow,
    DisputeStatus,
    ReasonCode,
)
from app.services.admin_audit import audit_actor
from app.services.admin_dispute_service import AdminDisputeService

router = APIRouter(prefix="/admin/disputes", tags=["Admin"])

_any_admin_role = require_roles("admin", "ops", "support")


@router.get("", response_model=AdminDisputeListResponse, dependencies=[Depends(_any_admin_role)])
async def list_disputes(
    status_filter: DisputeStatus | None = Query(None, alias="status"),
    reason_code: ReasonCode | None = Query(None),
    date_from: datetime | None = Query(None, description="created_at >= this"),
    date_to: datetime | None = Query(None, description="created_at < this"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Every dispute, newest first. A datetime with no offset is read as UTC."""
    return await AdminDisputeService(db).list_disputes(
        status_filter=status_filter,
        reason_code=reason_code,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/bookings/{booking_id}",
    response_model=AdminDisputeDetail,
    dependencies=[Depends(_any_admin_role)],
)
async def get_booking_disputes(booking_id: UUID, db: AsyncSession = Depends(get_db)):
    """A booking's dispute page: the booking, what is left to refund, its open
    dispute and every earlier one. 200 with no disputes too, so support can see
    the booking before logging one. 404 ``BOOKING_NOT_FOUND``."""
    return await AdminDisputeService(db).detail(booking_id)


@router.post("", response_model=AdminDisputeRow, status_code=status.HTTP_201_CREATED)
async def create_dispute(
    body: AdminDisputeCreate,
    request: Request,
    admin: User = Depends(_any_admin_role),
    db: AsyncSession = Depends(get_db),
):
    """Log a dispute on the customer's behalf. 409 ``DISPUTE_BOOKING_NOT_FINISHED``
    unless the booking is completed or cancelled; 409 ``DISPUTE_ALREADY_OPEN``
    (``details.dispute_id``) if one is RAISED or IN_REVIEW."""
    return await AdminDisputeService(db).create(audit_actor(request, admin), body)


@router.post("/{dispute_id}/review", response_model=AdminDisputeRow)
async def review_dispute(
    dispute_id: UUID,
    request: Request,
    admin: User = Depends(_any_admin_role),
    db: AsyncSession = Depends(get_db),
):
    """Pick up a RAISED dispute (→ IN_REVIEW). Optional: resolve works from either.
    409 ``DISPUTE_INVALID_STATE`` otherwise."""
    return await AdminDisputeService(db).review(audit_actor(request, admin), dispute_id)


@router.post("/{dispute_id}/resolve", response_model=AdminDisputeRow)
async def resolve_dispute(
    dispute_id: UUID,
    body: AdminDisputeResolve,
    request: Request,
    admin: User = Depends(_any_admin_role),
    db: AsyncSession = Depends(get_db),
):
    """Settle the dispute. An online refund goes to Razorpay before anything is
    saved and the dispute stays RESOLVED until ``refund.processed`` closes it. A
    cash refund is a record of money paid outside the app, and it and NO_REFUND
    close at once. The customer gets a push after the commit.

    * 409 ``DISPUTE_INVALID_STATE`` (already resolved), ``NOTHING_TO_REFUND``.
    * 422 ``DISPUTE_INVALID`` (``details.field``; ``details.refundable`` for an
      amount over what is left).
    * 403 ``REFUND_OVER_SUPPORT_LIMIT`` (``details.limit``).
    * 422 ``REFUND_REJECTED`` (``details.razorpay_error``): Razorpay said no.
    * 502 ``REFUND_GATEWAY_ERROR``: no answer. A retry is safe; it finds a
      refund already made for this dispute instead of refunding twice.
    """
    return await AdminDisputeService(db).resolve(
        audit_actor(request, admin), admin, dispute_id, body
    )
