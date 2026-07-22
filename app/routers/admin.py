"""
Admin router: user management, booking oversight, and KYC approval.
All endpoints require the "admin" role.
"""
from uuid import UUID
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_role
from app.models.user import User
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment
from app.models.provider_profile import ProviderProfile, KYCStatus
from app.schemas.user import UserResponse
from app.schemas.booking import BookingResponse, BookingListResponse
from app.services.payout_service import PayoutService

router = APIRouter(prefix="/admin", tags=["Admin"])

_admin_dep = Depends(require_role("admin"))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users", response_model=list[UserResponse], dependencies=[_admin_dep])
async def list_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch("/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_admin_dep])
async def deactivate_user(user_id: UUID, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if user:
        user.is_active = False
        await db.commit()


# ---------------------------------------------------------------------------
# Provider KYC
# ---------------------------------------------------------------------------

@router.patch("/providers/{user_id}/kyc", status_code=status.HTTP_200_OK, dependencies=[_admin_dep])
async def update_kyc_status(
    user_id: UUID,
    kyc_status: KYCStatus,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ProviderProfile).where(ProviderProfile.user_id == user_id)
    )
    profile = result.scalar_one_or_none()
    if profile:
        profile.kyc_status = kyc_status
        await db.commit()
    return {"kyc_status": kyc_status}


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

@router.get("/bookings", response_model=BookingListResponse, dependencies=[_admin_dep])
async def list_all_bookings(
    booking_status: str | None = Query(None, alias="status"),
    disputed: bool | None = Query(
        None, description="Filter to bookings with (or without) a charged-back payment"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    # A booking can have several payment rows (failed attempts + the captured one), so
    # "is this booking disputed" is an EXISTS over its payments rather than a join —
    # a join would duplicate booking rows once per payment (SVC-134).
    disputed_exists = (
        select(Payment.id)
        .where(Payment.booking_id == Booking.id, Payment.disputed_at.is_not(None))
        .exists()
    )

    stmt = select(Booking, disputed_exists.label("is_disputed"))
    count_stmt = select(func.count()).select_from(Booking)

    if booking_status:
        try:
            status_enum = BookingStatus(booking_status)
            stmt = stmt.where(Booking.status == status_enum)
            count_stmt = count_stmt.where(Booking.status == status_enum)
        except ValueError:
            pass

    if disputed is not None:
        clause = disputed_exists if disputed else ~disputed_exists
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)

    return BookingListResponse(
        bookings=[
            BookingResponse.model_validate(booking).model_copy(
                update={"is_disputed": is_disputed}
            )
            for booking, is_disputed in result.all()
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


# ---------------------------------------------------------------------------
# Provider payouts
# ---------------------------------------------------------------------------

@router.post("/providers/{user_id}/payout", status_code=status.HTTP_200_OK)
async def pay_out_provider(
    user_id: UUID,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Settle a provider's completed-but-unsettled bookings via a RazorpayX payout.

    The triggering admin is injected (not just role-checked) so the payout can be
    written to the audit trail. The transaction is committed by ``get_db`` on a
    clean response, so the payout's booking writes and the commit are owned in one
    place rather than committed here.
    """
    return await PayoutService(db).create_provider_payout(user_id, admin.id)


@router.post(
    "/providers/{user_id}/payout/requeue-failed", status_code=status.HTTP_200_OK
)
async def requeue_failed_settlements(
    user_id: UUID,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reset a provider's FAILED settlements to PENDING so they can be paid again.

    Manual recovery for payouts that failed (bad bank details, KYC hold, ...): once
    the cause is fixed, an admin re-queues the failed settlements, which then become
    eligible for the next payout run. The triggering admin is recorded in the audit
    trail.
    """
    return await PayoutService(db).requeue_failed_settlements(user_id, admin.id)
