import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.payment import (
    CreatePaymentOrderRequest,
    PaymentOrderResponse,
    RazorpayWebhookPayload,
    VerifyPaymentRequest,
)
from app.services.payment_service import PaymentService
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
)
async def create_payment_order(
    request: CreatePaymentOrderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await PaymentService(db).create_order(request.booking_id)


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
