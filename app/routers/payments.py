import hashlib
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
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
from app.utils.idempotency import IdempotencyStore, hash_body
from app.utils.rate_limiter import rate_limit

router = APIRouter(prefix="/payments", tags=["Payments"])
settings = get_settings()

# SC-35: 20 payment ops per user per hour. Shared namespace across order-
# creation and verify so a single limit covers the whole payment surface —
# stops abuse regardless of whether the attacker is spamming order creation
# or signature verification. Webhook is NOT rate-limited (Razorpay-initiated).
_PAYMENT_RATE_LIMIT = Depends(rate_limit("payment_ops", max_requests=20))


@router.post(
    "/orders",
    response_model=PaymentOrderResponse,
    dependencies=[_PAYMENT_RATE_LIMIT],
    # SVC-158 follow-up: idempotency conflict codes so the generated client
    # can distinguish them from generic 4xx and back off / surface correctly.
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
        return await PaymentService(db).create_order(request.booking_id)

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
    )
    return {"message": "Payment verified", "payment_id": str(payment.id)}


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Fix #7: Verify Razorpay webhook HMAC-SHA256 signature before processing.
    Razorpay sends X-Razorpay-Signature header = HMAC(raw_body, webhook_secret).
    """
    raw_body = await request.body()
    sig_header = request.headers.get("X-Razorpay-Signature", "")

    if settings.RAZORPAY_WEBHOOK_SECRET:
        expected = hmac.new(
            key=settings.RAZORPAY_WEBHOOK_SECRET.encode(),
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

        payload_data = RazorpayWebhookPayload.model_validate(json.loads(raw_body))
    except Exception as err:
        raise HTTPException(status_code=400, detail="Malformed webhook payload") from err

    await PaymentService(db).handle_webhook(
        event=payload_data.event,
        payload=payload_data.payload,
    )
    return {"status": "ok"}
