"""
Admin router: user management, booking oversight, and KYC approval.
All endpoints require the "admin" role.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_role
from app.models.booking import Booking, BookingStatus
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import User
from app.schemas.booking import BookingListResponse, BookingResponse
from app.schemas.provider import KYCDocsResponse, KYCRejectionRequest, KYCReviewResponse
from app.schemas.user import UserResponse
from app.services.provider_service import ProviderService

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


@router.patch(
    "/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_admin_dep]
)
async def deactivate_user(user_id: UUID, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if user:
        user.is_active = False
        await db.commit()


# ---------------------------------------------------------------------------
# Provider KYC (SC-31)
# ---------------------------------------------------------------------------


@router.get("/providers/kyc/pending", response_model=list[UserResponse], dependencies=[_admin_dep])
async def list_pending_kyc(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List providers whose KYC is awaiting admin review."""
    stmt = (
        select(User)
        .join(ProviderProfile, ProviderProfile.user_id == User.id)
        .where(ProviderProfile.kyc_status == KYCStatus.SUBMITTED)
        .order_by(ProviderProfile.kyc_submitted_at.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get(
    "/providers/{user_id}/kyc",
    response_model=KYCDocsResponse,
    dependencies=[_admin_dep],
)
async def get_provider_kyc_review(user_id: UUID, db: AsyncSession = Depends(get_db)):
    """
    Full KYC review payload for an admin: profile metadata + presigned URLs for
    the uploaded docs (1h TTL). Powers the admin KYC review page (SVC-90).
    """
    return await ProviderService(db).get_kyc_review_payload(user_id)


@router.post(
    "/providers/{user_id}/kyc/verify",
    response_model=KYCReviewResponse,
    dependencies=[_admin_dep],
)
async def verify_provider_kyc(user_id: UUID, db: AsyncSession = Depends(get_db)):
    profile = await ProviderService(db).verify_kyc(user_id)
    await db.commit()
    return profile


@router.post(
    "/providers/{user_id}/kyc/reject",
    response_model=KYCReviewResponse,
    dependencies=[_admin_dep],
)
async def reject_provider_kyc(
    user_id: UUID,
    body: KYCRejectionRequest,
    db: AsyncSession = Depends(get_db),
):
    profile = await ProviderService(db).reject_kyc(user_id, reason=body.reason)
    await db.commit()
    return profile


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
