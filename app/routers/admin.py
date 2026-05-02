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
from app.models.provider_profile import ProviderProfile, KYCStatus
from app.schemas.user import UserResponse
from app.schemas.booking import BookingResponse, BookingListResponse

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
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Booking)
    count_stmt = select(func.count()).select_from(Booking)

    if booking_status:
        try:
            status_enum = BookingStatus(booking_status)
            stmt = stmt.where(Booking.status == status_enum)
            count_stmt = count_stmt.where(Booking.status == status_enum)
        except ValueError:
            pass

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(stmt)
    bookings = list(result.scalars().all())

    return BookingListResponse(
        bookings=[BookingResponse.model_validate(b) for b in bookings],
        total=total,
        page=page,
        per_page=per_page,
    )
