from fastapi import APIRouter, Depends, Request, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
import hmac
import hashlib

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.config import get_settings
from app.schemas.payment import (
    CreatePaymentOrderRequest, PaymentOrderResponse,
    VerifyPaymentRequest, RazorpayWebhookPayload,
)
from app.services.payment_service import PaymentService
from app.tasks.notification_tasks import send_booking_notification

router = APIRouter(prefix="/payments", tags=["Payments"])
settings = get_settings()


@router.post("/orders", response_model=PaymentOrderResponse)
async def create_payment_order(
    request: CreatePaymentOrderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await PaymentService(db).create_order(request.booking_id)


@router.post("/verify", status_code=status.HTTP_200_OK)
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
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    await PaymentService(db).handle_webhook(
        event=payload_data.event,
        payload=payload_data.payload,
    )
    return {"status": "ok"}
