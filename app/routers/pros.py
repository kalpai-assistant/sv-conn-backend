"""The Pro's own surface (SVC-320).

No ``require_role`` here, on purpose: a Pro is whoever has an *active*
``professionals`` row, and most of them signed up as consumers (the app's sign-up
always sends ``role=consumer``). Every endpoint authorizes on that row instead,
via ``get_current_pro``; job endpoints also need it to be the job's Pro.

A solo SP's Pro-for-self is the same user as the SP, so this surface works for
them too, and their ``/providers`` + ``/bookings`` routes are unchanged.

A single job is read through ``GET /bookings/{id}``, which admits the assigned
Pro (PR 3); there is no ``/pros/me/jobs/{id}``.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.booking import BookingStatus
from app.models.professional import Professional
from app.models.user import User
from app.routers.bookings import notify_transition, viewer_for
from app.routers.providers import read_image_upload
from app.schemas.booking import BookingListResponse, BookingResponse, StartBookingRequest
from app.schemas.team import (
    DeclineJobResponse,
    ProfessionalResponse,
    ProLocationRequest,
    ProMeResponse,
    ProOnlineRequest,
    ProStatusResponse,
)
from app.services.booking_service import BookingService
from app.services.pro_service import ProService
from app.tasks.notification_tasks import notify_reopened, send_booking_notification
from app.utils.rate_limiter import rate_limit

router = APIRouter(prefix="/pros", tags=["Pros"])


async def get_current_pro(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Professional:
    """The caller's active Pro row, or 404 ``NOT_A_PRO``. Any role."""
    return await ProService(db).get_active_pro(current_user.id)


_NOT_A_PRO = {404: {"description": "NOT_A_PRO: the caller has no active Pro row"}}


@router.get("/me", response_model=ProMeResponse, responses=_NOT_A_PRO)
async def get_my_pro_profile(
    pro: Professional = Depends(get_current_pro),
    db: AsyncSession = Depends(get_db),
):
    """My Pro profile, my SP's brand, and the KYC that applies to me. No payout
    details: those belong to the SP (SVC-327)."""
    return await ProService(db).profile(pro)


@router.get("/me/jobs", response_model=BookingListResponse, responses=_NOT_A_PRO)
async def list_my_jobs(
    scope: Literal["active", "past"] = Query("active"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    pro: Professional = Depends(get_current_pro),
    db: AsyncSession = Depends(get_db),
):
    """Jobs assigned to me, newest first. ``active``: accepted, en route or in
    progress. ``past``: completed, cancelled, expired, or given back."""
    bookings, total = await ProService(db).list_jobs(pro, scope, page, per_page)
    return BookingListResponse(
        bookings=[BookingResponse.for_viewer(b, viewer_for(b, pro.user_id)) for b in bookings],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post("/me/online", response_model=ProStatusResponse, responses=_NOT_A_PRO)
async def set_my_online(
    request: ProOnlineRequest,
    pro: Professional = Depends(get_current_pro),
    db: AsyncSession = Depends(get_db),
):
    """Go online or offline. For a Pro-for-self this is the SP's own toggle
    (same service-area rule, 400 ``SERVICE_AREA_MISSING``)."""
    return await ProService(db).set_online(pro, request.is_online)


@router.post(
    "/me/location",
    response_model=ProStatusResponse,
    # A 30 s heartbeat is 2 a minute; 10 leaves room for retries and app wakes.
    dependencies=[Depends(rate_limit("pro_location", max_requests=10, window_seconds=60))],
    responses=_NOT_A_PRO,
)
async def update_my_location(
    request: ProLocationRequest,
    pro: Professional = Depends(get_current_pro),
    db: AsyncSession = Depends(get_db),
):
    """Location heartbeat while online, every ~30 s. Stored on my Pro row only;
    it does not feed live tracking."""
    return await ProService(db).set_location(pro, request.lat, request.lng)


async def _move(
    db: AsyncSession, pro: Professional, booking_id: UUID, status: BookingStatus, otp=None
) -> BookingResponse:
    booking = await ProService(db).job_transition(pro, booking_id, status, otp=otp)
    notify_transition(booking)
    return BookingResponse.for_viewer(booking, viewer_for(booking, pro.user_id))


_JOB = {
    404: {"description": "Not a job assigned to me (or I'm not a Pro)"},
    409: {"description": "Not a legal move from the job's current state"},
}


@router.post("/me/jobs/{booking_id}/en-route", response_model=BookingResponse, responses=_JOB)
async def pro_en_route(
    booking_id: UUID,
    pro: Professional = Depends(get_current_pro),
    db: AsyncSession = Depends(get_db),
):
    """I'm on my way. The consumer gets "Provider on the way"."""
    return await _move(db, pro, booking_id, BookingStatus.EN_ROUTE)


@router.post("/me/jobs/{booking_id}/start", response_model=BookingResponse, responses=_JOB)
async def pro_start(
    booking_id: UUID,
    request: StartBookingRequest,
    pro: Professional = Depends(get_current_pro),
    db: AsyncSession = Depends(get_db),
):
    """Start at the door with the consumer's OTP (400 if it doesn't match)."""
    return await _move(db, pro, booking_id, BookingStatus.IN_PROGRESS, otp=request.otp)


@router.post("/me/jobs/{booking_id}/complete", response_model=BookingResponse, responses=_JOB)
async def pro_complete(
    booking_id: UUID,
    pro: Professional = Depends(get_current_pro),
    db: AsyncSession = Depends(get_db),
):
    """Done. The consumer is told, and a cash job's payment is recorded."""
    return await _move(db, pro, booking_id, BookingStatus.COMPLETED)


@router.post(
    "/me/kyc",
    response_model=ProfessionalResponse,
    status_code=201,
    responses={
        **_NOT_A_PRO,
        409: {"description": "SELF_PRO_KYC_IS_SP_KYC: a Pro-for-self uses /providers/me/kyc"},
    },
)
async def submit_my_kyc(
    aadhaar_front: UploadFile = File(..., description="Aadhaar front image"),
    aadhaar_back: UploadFile = File(..., description="Aadhaar back image"),
    selfie: UploadFile = File(..., description="Live selfie"),
    pro: Professional = Depends(get_current_pro),
    db: AsyncSession = Depends(get_db),
):
    """My Aadhaar identity KYC (SVC-327). Same upload and AI check as an SP's,
    recorded on my Pro row only. Payout KYC stays with my SP."""
    return await ProService(db).submit_kyc(
        pro,
        aadhaar_front=await read_image_upload(aadhaar_front, "aadhaar_front"),
        aadhaar_back=await read_image_upload(aadhaar_back, "aadhaar_back"),
        selfie=await read_image_upload(selfie, "selfie"),
    )


@router.post(
    "/me/jobs/{booking_id}/decline",
    response_model=DeclineJobResponse,
    responses={
        404: {"description": "Not a job assigned to me (or no such booking)"},
        409: {"description": "Too late to decline: already en route, or finished"},
    },
)
async def decline_job(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn down a job I've been assigned, before setting off.

    If my SP has someone else on the team, the job stays with them and they're
    asked to pick another Pro (``awaiting_repick``). Otherwise it goes back to
    the broadcast for other SPs (``reopened``), and the consumer is told we're
    finding someone else.
    """
    booking, outcome = await BookingService(db).decline_job(booking_id, current_user.id)
    if outcome == "reopened":
        notify_reopened(booking.id)
    elif booking.provider_id != current_user.id:
        # An SP declining their own Pro-for-self's job doesn't need telling.
        send_booking_notification.delay(
            booking_id=str(booking.id), event="pro_declined", actor_id=str(current_user.id)
        )
    return DeclineJobResponse(booking_id=booking.id, outcome=outcome)
