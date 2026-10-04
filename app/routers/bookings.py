from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_redis, require_role
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
    TipBookingRequest,
)
from app.schemas.team import AssignRequest, TeamPickPro, TeamPickResponse
from app.services.booking_service import BookingService
from app.tasks.notification_tasks import notify_reopened, send_booking_notification
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
            # The schema's model_validator normalises legacy single-service
            # requests (`service_id` only) into a 1-item `items` list, so
            # the service layer only ever sees the cart shape. `items` is
            # guaranteed non-None after validation.
            items=request.items,  # type: ignore[arg-type]
            address_id=request.address_id,
            scheduled_at=request.scheduled_at,
            payment_method=request.payment_method,
            coupon_code=request.coupon_code,
        )
        send_booking_notification.delay(booking_id=str(booking.id), event="new_request")
        return BookingResponse.for_viewer(booking, current_user.id)

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
        response_body = (await do_create()).model_dump(mode="json")
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
    # SVC-168: gate at the endpoint too. The service-layer eligibility check is
    # the real defence; this is defence-in-depth against a future refactor that
    # drops the service check. A consumer or admin hitting /accept must never
    # reach the service call.
    dependencies=[Depends(require_role("provider"))],
    # Declared so they reach the generated client — the Incoming Request Card
    # branches on these codes and must never parse the message (SVC-154).
    #
    # SVC-167: the lost-race case (someone else took it) now returns 410, not
    # 403, matching cancelled/expired. All three "the offer is gone" outcomes
    # share one code the card can key on. 403 stays for "you weren't offered
    # this booking" (SVC-168) — a distinct outcome the card treats differently.
    responses={
        403: {
            "description": "You were not offered this booking. Not visible to a "
            "well-behaved client — the app never sends /accept for a booking it "
            "hasn't been notified about."
        },
        404: {"description": "No such booking"},
        410: {
            "description": "The offer is over — claimed by another provider, "
            "cancelled, or expired. Dismiss the card."
        },
        202: {
            "model": TeamPickResponse,
            "description": "Only with `X-Team-Pick: 1`, for an SP with a team: "
            "nothing is claimed yet. Show the pick-a-Pro modal, then claim with "
            "POST /bookings/{id}/assign.",
        },
    },
)
async def accept_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # SVC-320: opt-in, so a client that doesn't know about the pick step (every
    # app build before SVC-321) can never read "pick a Pro" as "you got it" —
    # it treats any 2xx as a win. Without the header a team SP's accept claims
    # with their own Pro-for-self, exactly like a solo SP's, and they can hand
    # it on with /assign.
    x_team_pick: str | None = Header(default=None, alias="X-Team-Pick"),
):
    """Claim a broadcast booking. First provider to accept wins.

    Idempotent: a provider re-accepting a booking they already hold gets 200 and
    the booking, with no second notification to the consumer. That case is a
    retry after a lost response, not a new acceptance — notifying again would
    tell the consumer twice, and returning an error would tell the *winning*
    provider they lost (SVC-154).
    """
    service = BookingService(db)
    if x_team_pick == "1":
        options = await service.team_pick_options(booking_id, current_user.id)
        if options is not None:
            body = TeamPickResponse(
                booking_id=booking_id,
                pros=[
                    TeamPickPro(
                        id=pro.id,
                        display_name=pro.display_name,
                        profile_photo_url=pro.profile_photo_url,
                        is_self=pro.is_self,
                        busy=busy,
                    )
                    for pro, busy in options
                ],
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED, content=body.model_dump(mode="json")
            )

    booking, newly_accepted = await service.accept_booking(
        booking_id=booking_id, provider_id=current_user.id
    )
    if newly_accepted:
        send_booking_notification.delay(booking_id=str(booking.id), event="accepted")
    return BookingResponse.for_viewer(booking, current_user.id)


@router.post(
    "/{booking_id}/assign",
    response_model=BookingResponse,
    dependencies=[Depends(require_role("provider"))],
    responses={
        403: {"description": "You were not offered this booking"},
        404: {"description": "No such booking, or that Pro isn't on your team"},
        409: {
            "description": "PRO_BUSY: the Pro is on another unfinished job; or the "
            "booking is already en route or later"
        },
        410: {"description": "The offer is over — claimed by another SP, cancelled or expired"},
    },
)
async def assign_booking(
    booking_id: UUID,
    request: AssignRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Give this booking to one of my Pros (SVC-320 two-level dispatch).

    On an open booking this is the claim, first write wins, with the same
    answers as accept: 403 not offered, 410 gone. On a booking I already hold
    at ACCEPTED it sets or replaces the Pro, e.g. after a decline. Assigning the
    Pro already on it is an idempotent 200 with no push.
    """
    booking, newly_claimed, pro = await BookingService(db).assign(
        booking_id=booking_id, sp_id=current_user.id, professional_id=request.professional_id
    )
    if newly_claimed:
        send_booking_notification.delay(booking_id=str(booking.id), event="accepted")
    if pro is not None and not pro.is_self:
        send_booking_notification.delay(booking_id=str(booking.id), event="assigned")
    return BookingResponse.for_viewer(booking, current_user.id)


@router.post(
    "/{booking_id}/release",
    response_model=BookingResponse,
    dependencies=[Depends(require_role("provider"))],
    responses={
        403: {"description": "You do not hold this booking"},
        409: {"description": "Someone is already en route, or the booking is finished"},
    },
)
async def release_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Give an accepted booking back so other SPs can take it (SVC-320).

    For when nobody on my team can do it. It goes back to REQUESTED with a
    fresh 2-minute window, the consumer is told we're finding someone else, and
    it is broadcast again to everyone except me. Only before anyone sets off.
    """
    booking = await BookingService(db).release(booking_id=booking_id, sp_id=current_user.id)
    notify_reopened(booking.id)
    return BookingResponse.for_viewer(booking, current_user.id)


@router.patch(
    "/{booking_id}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    # Same defence-in-depth as /accept (SVC-168): a consumer or admin must never
    # reach the service call, even though the offer-row lookup would refuse them
    # anyway.
    dependencies=[Depends(require_role("provider"))],
    responses={
        403: {"description": "You were not offered this booking"},
    },
)
async def decline_booking(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn down a job offer (SVC-254). Backs the card's "Not now".

    Without this the poll would re-serve a dismissed offer every fifteen seconds
    until the window closed — worse than never showing it. Declining only
    silences it for this provider; the booking stays open for everyone else.

    Idempotent, and quiet about it: declining an offer already answered returns
    204 rather than an error. A double tap is the common cause, and the
    provider's intent is satisfied either way. 403 when they were never offered
    the booking — the same answer ``/offer`` gives a non-recipient, so decline
    can't be used to probe which bookings exist.
    """
    await BookingService(db).decline_offer(booking_id=booking_id, provider_id=current_user.id)


@router.patch("/{booking_id}/en-route", response_model=BookingResponse)
async def mark_en_route(
    booking_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    booking = await BookingService(db).transition_status(
        booking_id=booking_id,
        new_status=BookingStatus.EN_ROUTE,
        changed_by=current_user.id,
    )
    # SVC-334: the consumer is waiting on exactly this, and it used to be silent.
    send_booking_notification.delay(booking_id=str(booking.id), event="en_route")
    # The window SVC-169 is about: ACCEPTED and EN_ROUTE are the two states
    # where the PIN is still live, and both of these routes are provider-driven.
    return BookingResponse.for_viewer(booking, current_user.id)


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
    return BookingResponse.for_viewer(booking, current_user.id)


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
    return BookingResponse.for_viewer(booking, current_user.id)


@router.patch(
    "/{booking_id}/cancel",
    response_model=BookingResponse,
    responses={409: {"description": "PROVIDER_EN_ROUTE: the provider is on the way"}},
)
async def cancel_booking(
    booking_id: UUID,
    request: CancelBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a booking — or, for its provider while ACCEPTED, give it back.

    SVC-344: a provider backing out of an accepted job reopens it (back to
    searching, no fee to anyone, the consumer told we're finding someone else)
    instead of cancelling it on the consumer. Once en route it's a 409
    ``PROVIDER_EN_ROUTE``: that needs support. The consumer cancels as before.
    """
    booking, outcome = await BookingService(db).cancel(
        booking_id=booking_id, user_id=current_user.id, reason=request.reason
    )
    if outcome == "reopened":
        notify_reopened(booking.id)
        return BookingResponse.for_viewer(booking, current_user.id)
    # SVC-278: cancel was the only lifecycle transition that told nobody, so
    # the other party found out by opening the app — a provider could travel to
    # a job the consumer had already called off. `actor_id` because either side
    # can cancel and the push goes to the one who didn't.
    send_booking_notification.delay(
        booking_id=str(booking.id), event="cancelled", actor_id=str(current_user.id)
    )
    return BookingResponse.for_viewer(booking, current_user.id)


@router.post(
    "/{booking_id}/tip",
    response_model=BookingResponse,
    responses={
        403: {"description": "Only the booking's own consumer can add a tip"},
        404: {"description": "No such booking"},
        409: {
            "description": "Booking is not in a state where a tip is accepted "
            "(only after COMPLETED, and not after refund)."
        },
    },
)
async def tip_booking(
    booking_id: UUID,
    request: TipBookingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Consumer adds (or updates) a gratuity on a completed booking.

    100% of the tip flows to the provider — added on top of provider_payout
    at payout time. A subsequent call with a different amount replaces
    rather than accumulates; ``amount=0`` removes the tip. Bounds live in
    the request schema; state + access checks live in the service layer.
    """
    booking = await BookingService(db).set_tip(
        booking_id=booking_id, consumer_id=current_user.id, amount=request.amount
    )
    return BookingResponse.for_viewer(booking, current_user.id)


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
    """Fetch a single booking by ID. Only consumer or assigned provider may access.

    Both parties get the same schema, so the response is built per-caller rather
    than split by ``response_model``: the assigned provider sees the booking
    without ``doorstep_otp`` (SVC-169).
    """
    booking = await BookingService(db).get_booking(booking_id=booking_id, user_id=current_user.id)
    return BookingResponse.for_viewer(booking, current_user.id)
