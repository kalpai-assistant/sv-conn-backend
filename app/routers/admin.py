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
from app.exceptions import BookingNotFound
from app.models.booking import Booking, BookingStatus
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import User
from app.schemas.booking import BookingListResponse, BookingResponse
from app.schemas.provider import KYCDocsResponse, KYCRejectionRequest, KYCReviewResponse
from app.schemas.user import UserResponse
from app.services.booking_service import BookingService
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

    # Hydrated even though `for_viewer(viewer_id=None)` then drops it: the schema
    # reads `Booking.consumer_address` while validating, and that relationship is
    # `lazy="raise_on_sql"` (SVC-170). Withholding by simply not loading would
    # work today and break the moment the redaction rule changes — who may see an
    # address is one decision, and it lives next to the doorstep-PIN rule it sits
    # beside, not in the shape of this query. The single-booking route below is
    # the one that serves it.
    await BookingService(db).attach_addresses(bookings)

    return BookingListResponse(
        # No viewer identity to compare against — an admin is never the booking's
        # consumer, so no row here carries its doorstep PIN. This listing spans
        # every booking in the system, live ones included (SVC-169). For the same
        # reason no row carries a consumer address either, though the reasoning
        # differs — bulk PII minimisation rather than secrecy. Both rules live in
        # `for_viewer`; see its docstring (SVC-170).
        bookings=[BookingResponse.for_viewer(b, viewer_id=None) for b in bookings],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get(
    "/bookings/{booking_id}",
    response_model=BookingResponse,
    dependencies=[_admin_dep],
    responses={404: {"description": "No such booking"}},
)
async def get_any_booking(booking_id: UUID, db: AsyncSession = Depends(get_db)):
    """Fetch any one booking by id, whoever it belongs to.

    Distinct from ``GET /bookings/{id}``, which admits only that booking's own
    consumer or assigned provider — an admin is neither, so the user-facing route
    refuses them and there was previously no way for support to look at a single
    booking at all.

    This one carries the consumer's address, which the bulk listing above
    deliberately does not (SVC-170). That is the whole distinction: one booking
    asked for by id is a support question about a specific job, where a page of
    every booking on the platform is a standing export of home addresses. It
    still withholds the doorstep PIN — see ``BookingResponse.for_admin``.
    """
    booking = await db.get(Booking, booking_id)
    if booking is None:
        raise BookingNotFound(booking_id)

    await BookingService(db).attach_addresses([booking])
    return BookingResponse.for_admin(booking)
