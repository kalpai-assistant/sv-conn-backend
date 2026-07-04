from uuid import UUID
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.booking import BookingStatus
from app.schemas.booking import (
    CreateBookingRequest, BookingResponse, BookingListResponse,
    StartBookingRequest, CancelBookingRequest,
)
from app.services.booking_service import BookingService
from app.tasks.notification_tasks import send_booking_notification
from app.tasks.payment_tasks import process_cash_payment
from app.models.booking import PaymentMethod
from app.utils.rate_limiter import rate_limit

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.post(
    "/",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    # SC-35: 10 booking creations per consumer per hour. A determined attacker
    # bypasses this with multiple accounts, but each account they burn costs
    # them SMS OTPs — layered defence with the OTP throttle.
    dependencies=[Depends(rate_limit("booking_create", max_requests=10))],
)
async def create_booking(
    request: CreateBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    booking = await BookingService(db).create_booking(
        consumer_id=current_user.id,
        service_id=request.service_id,
        address_id=request.address_id,
        scheduled_at=request.scheduled_at,
        payment_method=request.payment_method,
    )
    send_booking_notification.delay(booking_id=str(booking.id), event="new_request")
    return booking


@router.patch("/{booking_id}/accept", response_model=BookingResponse)
async def accept_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    booking = await BookingService(db).transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.ACCEPTED,
        changed_by=current_user.id,
    )
    send_booking_notification.delay(booking_id=str(booking.id), event="accepted")
    return booking


@router.patch("/{booking_id}/en-route", response_model=BookingResponse)
async def mark_en_route(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await BookingService(db).transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.EN_ROUTE,
        changed_by=current_user.id,
    )


@router.patch("/{booking_id}/start", response_model=BookingResponse)
async def start_booking(
    booking_id: UUID,
    request: StartBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Provider verifies doorstep OTP to start the service."""
    booking = await BookingService(db).transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.IN_PROGRESS,
        changed_by=current_user.id,
        otp=request.otp,
    )
    send_booking_notification.delay(booking_id=str(booking.id), event="in_progress")
    return booking


@router.patch("/{booking_id}/complete", response_model=BookingResponse)
async def complete_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    booking = await BookingService(db).transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.COMPLETED,
        changed_by=current_user.id,
    )
    send_booking_notification.delay(booking_id=str(booking.id), event="completed")
    # Fix #4: Trigger cash payment recording if applicable
    if booking.payment_method == PaymentMethod.CASH:
        process_cash_payment.delay(booking_id=str(booking.id))
    return booking


@router.patch("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: UUID,
    request: CancelBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await BookingService(db).transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.CANCELLED,
        changed_by=current_user.id,
        cancellation_reason=request.reason,
    )


@router.get("/", response_model=BookingListResponse)
async def list_bookings(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await BookingService(db).list_bookings(
        user_id=current_user.id,
        role=current_user.role,
        status_filter=status,
        page=page,
        per_page=per_page,
    )


@router.get("/{booking_id}", response_model=BookingResponse)
async def get_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single booking by ID. Only consumer or assigned provider may access."""
    return await BookingService(db).get_booking(booking_id=booking_id, user_id=current_user.id)
