from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_redis
from app.exceptions import IdempotencyInProgress
from app.models.booking import BookingStatus, PaymentMethod
from app.models.user import User
from app.schemas.booking import (
    BookingListResponse,
    BookingOfferResponse,
    BookingResponse,
    CancelBookingRequest,
    CreateBookingRequest,
    StartBookingRequest,
)
from app.services.booking_service import BookingService
from app.tasks.notification_tasks import send_booking_notification
from app.tasks.payment_tasks import process_cash_payment
from app.utils.idempotency import IdempotencyStore, hash_body
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
    # SVC-158: declared so the generated client sees the idempotency-conflict
    # codes and can map them to sensible client behaviour instead of a generic
    # 4xx.
    responses={
        409: {
            "description": "A request with this Idempotency-Key is already in "
            "progress. Retry after a short backoff."
        },
        422: {
            "description": "Idempotency-Key was reused with a different request "
            "body. Generate a new key for each distinct request."
        },
    },
)
async def create_booking(
    request: CreateBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    # SVC-158: optional. Clients that don't send it get the pre-idempotency
    # behaviour (still safe — 10/hour rate limit caps abuse). Once the mobile
    # app sends it on every booking, double-taps + network-retry storms can't
    # create duplicate bookings.
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    async def do_create() -> BookingResponse:
        booking = await BookingService(db).create_booking(
            consumer_id=current_user.id,
            service_id=request.service_id,
            address_id=request.address_id,
            scheduled_at=request.scheduled_at,
            payment_method=request.payment_method,
        )
        send_booking_notification.delay(booking_id=str(booking.id), event="new_request")
        return booking

    if not idempotency_key:
        return await do_create()

    store = IdempotencyStore(redis, str(current_user.id), idempotency_key)
    body_hash = hash_body(request.model_dump(mode="json"))

    # Fast path — we've already served this exact request.
    cached = await store.check(body_hash)
    if cached is not None:
        status_code, cached_body = cached
        return JSONResponse(status_code=status_code, content=cached_body)

    # Try to claim the lock. If another request beat us to it we re-check —
    # if it's already done we serve the cached response, otherwise 409.
    if not await store.reserve(body_hash):
        cached = await store.check(body_hash)
        if cached is not None:
            status_code, cached_body = cached
            return JSONResponse(status_code=status_code, content=cached_body)
        raise IdempotencyInProgress()

    try:
        booking = await do_create()
        response_body = BookingResponse.model_validate(booking).model_dump(mode="json")
        await store.commit(body_hash, status.HTTP_201_CREATED, response_body)
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=response_body)
    except Exception:
        # Drop the pending sentinel so a retry isn't blocked by a lock that
        # will only expire after PENDING_TTL_SECONDS.
        await store.rollback()
        raise


@router.patch(
    "/{booking_id}/accept",
    response_model=BookingResponse,
    # Declared so they reach the generated client — the Incoming Request Card
    # branches on these codes and must never parse the message (SVC-154).
    responses={
        403: {"description": "Another provider claimed this booking first"},
        404: {"description": "No such booking"},
        410: {
            "description": "The booking is no longer available — cancelled or "
            "expired. Dismiss the card."
        },
    },
)
async def accept_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Claim a broadcast booking. First provider to accept wins.

    Idempotent: a provider re-accepting a booking they already hold gets 200 and
    the booking, with no second notification to the consumer. That case is a
    retry after a lost response, not a new acceptance — notifying again would
    tell the consumer twice, and returning an error would tell the *winning*
    provider they lost (SVC-154).
    """
    booking, newly_accepted = await BookingService(db).accept_booking(
        booking_id=booking_id, provider_id=current_user.id
    )
    if newly_accepted:
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


@router.get(
    "/{booking_id}/offer",
    response_model=BookingOfferResponse,
    # Declared so they reach the generated client. The card's behaviour is keyed
    # entirely on these codes, and without this the spec advertises only 200/422 —
    # the app would have no contract for the two cases that actually drive the UI.
    # First route in this repo to document its error responses; worth copying.
    responses={
        403: {"description": "You were not offered this booking"},
        404: {"description": "No such booking"},
        410: {
            "description": "The offer is over — claimed by another provider, "
            "cancelled, or expired. Dismiss the card."
        },
    },
)
async def get_booking_offer(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What a provider may see about a broadcast job before accepting it (SVC-153).

    Backs the Incoming Request Card: the ``new_request`` push carries only a booking
    id, and ``GET /{booking_id}`` refuses a provider who has not been assigned yet.

    403 when the caller was not offered this booking, 410 once it is no longer
    open. Both come from the service so they carry the standard error shape — the
    card keys off the status code, not the message. Role is not gated here: the
    eligibility check subsumes it, since only a matching provider can pass it.
    """
    return await BookingService(db).get_offer(booking_id=booking_id, provider_id=current_user.id)


@router.get("/{booking_id}", response_model=BookingResponse)
async def get_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single booking by ID. Only consumer or assigned provider may access."""
    return await BookingService(db).get_booking(booking_id=booking_id, user_id=current_user.id)
