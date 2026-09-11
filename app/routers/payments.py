import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, get_redis
from app.exceptions import IdempotencyInProgress
from app.models.user import User
from app.schemas.payment import (
    CreatePaymentOrderRequest,
    PaymentOrderResponse,
    RazorpayWebhookPayload,
    VerifyPaymentRequest,
)
from app.services.payment_service import PaymentService
from app.services.payout_service import PayoutService
from app.utils.idempotency import IdempotencyStore, hash_body
from app.utils.rate_limiter import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])
settings = get_settings()

# SC-35: 20 payment ops per user per hour. Shared namespace across order-
# creation and verify so a single limit covers the whole payment surface —
# stops abuse regardless of whether the attacker is spamming order creation
# or signature verification. Webhook is NOT rate-limited (Razorpay-initiated).
#
# TEMPORARY BUMP to 1000 for the 2026-09 live-payment testing sprint. A
# normal book+pay cycle uses 2 calls (create-order + verify), so 1000/hour
# = ~500 attempts per user per hour — effectively no ceiling during
# testing. Revert to 20 once the live-mode rollout is done. If this
# comment is still here past October the sprint is over; shrink it back.
_PAYMENT_RATE_LIMIT = Depends(rate_limit("payment_ops", max_requests=1000))


@router.post(
    "/orders",
    response_model=PaymentOrderResponse,
    dependencies=[_PAYMENT_RATE_LIMIT],
    responses={
        # SVC-39 G5 — caller must own the booking.
        403: {"description": "You do not have access to this booking."},
        # SVC-158 follow-up: idempotency conflict codes so the generated client
        # can distinguish them from generic 4xx and back off / surface correctly.
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
async def create_payment_order(
    request: CreatePaymentOrderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    # SVC-158 follow-up. `POST /bookings` gets the same treatment; this one
    # matters more because the side effect is real money: without idempotency
    # a double-tapped Pay button creates two Razorpay orders, both of which
    # can be paid, and we've charged the user twice for one booking.
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    async def do_create() -> dict:
        return await PaymentService(db).create_order(request.booking_id, current_user.id)

    if not idempotency_key:
        return await do_create()

    store = IdempotencyStore(redis, str(current_user.id), idempotency_key)
    body_hash = hash_body(request.model_dump(mode="json"))

    cached = await store.check(body_hash)
    if cached is not None:
        status_code, cached_body = cached
        return JSONResponse(status_code=status_code, content=cached_body)

    if not await store.reserve(body_hash):
        cached = await store.check(body_hash)
        if cached is not None:
            status_code, cached_body = cached
            return JSONResponse(status_code=status_code, content=cached_body)
        raise IdempotencyInProgress()

    try:
        order = await do_create()
        await store.commit(body_hash, status.HTTP_200_OK, order)
        return JSONResponse(status_code=status.HTTP_200_OK, content=order)
    except Exception:
        # Drop the pending sentinel so a retry can proceed without waiting
        # out PENDING_TTL_SECONDS.
        await store.rollback()
        raise


@router.post(
    "/verify",
    status_code=status.HTTP_200_OK,
    dependencies=[_PAYMENT_RATE_LIMIT],
    responses={
        # SVC-39 G5 — caller must own the booking the order was created for.
        403: {"description": "You do not have access to this booking."},
    },
)
async def verify_payment(
    request: VerifyPaymentRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    payment = await PaymentService(db).verify_payment(
        razorpay_order_id=request.razorpay_order_id,
        razorpay_payment_id=request.razorpay_payment_id,
        razorpay_signature=request.razorpay_signature,
        consumer_id=current_user.id,
    )
    return {"message": "Payment verified", "payment_id": str(payment.id)}


async def _verify_and_parse(request: Request, secret: str) -> RazorpayWebhookPayload:
    """
    Verify a Razorpay webhook's HMAC-SHA256 signature and parse its body.

    Razorpay sends X-Razorpay-Signature = HMAC(raw_body, webhook_secret). The
    payments and payouts (RazorpayX) products each have their own secret, so the
    caller passes the secret for the product that owns the endpoint.

    SVC-39 G3: refuses when the secret is empty in any non-dev environment. The
    endpoint is unauthenticated by design (Razorpay initiates), so an unset
    secret used to leave the route open to forged webhooks — anyone could flip
    a booking to PAID, mark settlements COMPLETED, or return a payout to
    PENDING. Same discipline `AuthService._is_e2e_test_phone` applies to the
    E2E OTP bypass. Dev keeps the old behaviour (log-warn and accept) so a
    local `.env` without secrets doesn't need every test to fake a signature.
    """
    raw_body = await request.body()
    sig_header = request.headers.get("X-Razorpay-Signature", "")

    if not secret:
        if settings.ENVIRONMENT != "dev":
            logger.error(
                "Webhook secret not configured for %s in environment=%s — refusing",
                request.url.path,
                settings.ENVIRONMENT,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Webhook secret not configured",
            )
        logger.warning(
            "Webhook signature verification bypassed for %s (empty secret in dev)",
            request.url.path,
        )
    else:
        expected = hmac.new(
            key=secret.encode(),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid webhook signature",
            )

    try:
        import json

        return RazorpayWebhookPayload.model_validate(json.loads(raw_body))
    except Exception as err:
        raise HTTPException(status_code=400, detail="Malformed webhook payload") from err


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Razorpay Payments webhook — verified against RAZORPAY_WEBHOOK_SECRET."""
    payload_data = await _verify_and_parse(request, settings.RAZORPAY_WEBHOOK_SECRET)
    await PaymentService(db).handle_webhook(
        event=payload_data.event,
        payload=payload_data.payload,
    )
    return {"status": "ok"}


@router.post("/payout-webhook", status_code=status.HTTP_200_OK)
async def razorpayx_payout_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    RazorpayX (payouts) webhook — verified against RAZORPAYX_WEBHOOK_SECRET.

    RazorpayX is a separate Razorpay product with its own webhook secret, so its
    payout.processed / payout.failed / payout.reversed events must arrive on a
    dedicated endpoint rather than the Payments webhook (different secret).

    The provider's push is dispatched as a background task so it runs only after
    ``get_db`` commits. If this delivery rolls back, RazorpayX retries it and the
    retry settles the same bookings — pushing inline would notify the provider once
    per attempt.
    """
    payload_data = await _verify_and_parse(request, settings.RAZORPAYX_WEBHOOK_SECRET)
    service = PayoutService(db)
    await service.handle_payout_webhook(
        event=payload_data.event,
        payload=payload_data.payload,
    )
    background_tasks.add_task(service.dispatch_pending_notifications)
    return {"status": "ok"}
