"""
Admin router: user management, booking oversight, and KYC approval.
All endpoints require the "admin" role.
"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_role
from app.exceptions import BookingNotFound
from app.models.booking import Booking, BookingStatus, SettlementStatus
from app.models.payment import Payment
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import User
from app.schemas.booking import BookingListResponse, BookingResponse
from app.schemas.payout import PendingSettlementsResponse
from app.schemas.provider import KYCDocsResponse, KYCRejectionRequest, KYCReviewResponse
from app.schemas.user import UserResponse
from app.services.booking_service import BookingService
from app.services.payout_service import PayoutService
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
    rows = list(result.all())
    bookings = [booking for booking, _ in rows]

    # Hydrated even though `for_viewer(viewer_id=None)` then drops the address: the
    # schema reads `Booking.consumer_address` while validating, and that relationship
    # is `lazy="raise_on_sql"` (SVC-170). Withholding by simply not loading would
    # work today and break the moment the redaction rule changes — who may see an
    # address is one decision, and it lives next to the doorstep-PIN rule it sits
    # beside, not in the shape of this query. The single-booking route below is
    # the one that serves it. Same principle for items: attached because the
    # response schema is going to look for them, not because an admin needs the
    # per-item breakdown right now.
    svc = BookingService(db)
    await svc.attach_addresses(bookings)
    await svc.attach_items(bookings)

    return BookingListResponse(
        # No viewer identity to compare against — an admin is never the booking's
        # consumer, so no row here carries its doorstep PIN. This listing spans
        # every booking in the system, live ones included (SVC-169). For the same
        # reason no row carries a consumer address either, though the reasoning
        # differs — bulk PII minimisation rather than secrecy. Both rules live in
        # `for_viewer`; see its docstring (SVC-170). `is_disputed` is stitched in
        # from the EXISTS in the query (SVC-134).
        bookings=[
            BookingResponse.for_viewer(booking, viewer_id=None).model_copy(
                update={"is_disputed": is_disputed}
            )
            for booking, is_disputed in rows
        ],
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

    svc = BookingService(db)
    await svc.attach_addresses([booking])
    await svc.attach_items([booking])
    return BookingResponse.for_admin(booking)


# ---------------------------------------------------------------------------
# Provider payouts
# ---------------------------------------------------------------------------


# Declared before the /providers/{user_id}/... routes: "pending-settlements" is a
# literal segment, and if a GET /providers/{user_id} is ever added below, FastAPI
# would otherwise match this path as a user id.
@router.get(
    "/providers/pending-settlements",
    response_model=PendingSettlementsResponse,
    dependencies=[_admin_dep],
)
async def list_pending_settlements(db: AsyncSession = Depends(get_db)):
    """Providers awaiting settlement, with the amount and booking count owed.

    The discovery half of the payout flow: ``POST /providers/{user_id}/payout`` can
    only be triggered for a provider the admin already knows, so this answers "who
    do we owe, and how much" for the admin payouts screen. Read-only, so it takes
    the role guard rather than injecting the admin (nothing to audit).
    """
    return await PayoutService(db).list_pending_settlements()


@router.post("/providers/{user_id}/payout", status_code=status.HTTP_200_OK)
async def pay_out_provider(
    user_id: UUID,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Settle a provider's completed-but-unsettled bookings via a RazorpayX payout.

    The triggering admin is injected (not just role-checked) so the payout can be
    written to the audit trail. The transaction is committed by ``get_db`` on a
    clean response, so the payout's booking writes and the commit are owned in one
    place rather than committed here.

    The provider's push is dispatched as a background task, which FastAPI runs
    *after* that dependency commits — and skips entirely if the commit raises. The
    money has already left RazorpayX by then, so announcing a payout the database
    failed to record would be worse than a missing push.
    """
    service = PayoutService(db)
    result = await service.create_provider_payout(user_id, admin.id)
    background_tasks.add_task(service.dispatch_pending_notifications)
    return result


@router.post("/providers/{user_id}/payout/requeue-failed", status_code=status.HTTP_200_OK)
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
    return await PayoutService(db).requeue_settlements(
        user_id, admin.id, from_status=SettlementStatus.FAILED
    )


@router.post("/providers/{user_id}/payout/requeue-reversed", status_code=status.HTTP_200_OK)
async def requeue_reversed_settlements(
    user_id: UUID,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reset a provider's REVERSED settlements to PENDING so they can be paid again.

    A reversal means the payout reached the provider and was then clawed back —
    often a KYC/compliance signal, not a simple retry. Only use this once you've
    confirmed the provider is genuinely still owed and the underlying cause is
    fixed; otherwise the re-payment will likely bounce again. Admin-only and
    recorded in the audit trail.
    """
    return await PayoutService(db).requeue_settlements(
        user_id, admin.id, from_status=SettlementStatus.REVERSED
    )
