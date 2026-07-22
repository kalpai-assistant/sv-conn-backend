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
from app.services.payout_service import PayoutService
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


async def _verify_and_parse(request: Request, secret: str) -> RazorpayWebhookPayload:
    """
    Verify a Razorpay webhook's HMAC-SHA256 signature and parse its body.

    Razorpay sends X-Razorpay-Signature = HMAC(raw_body, webhook_secret). The
    payments and payouts (RazorpayX) products each have their own secret, so the
    caller passes the secret for the product that owns the endpoint.
    """
    raw_body = await request.body()
    sig_header = request.headers.get("X-Razorpay-Signature", "")

    if secret:
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
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed webhook payload")


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
    db: AsyncSession = Depends(get_db),
):
    """
    RazorpayX (payouts) webhook — verified against RAZORPAYX_WEBHOOK_SECRET.

    RazorpayX is a separate Razorpay product with its own webhook secret, so its
    payout.processed / payout.failed / payout.reversed events must arrive on a
    dedicated endpoint rather than the Payments webhook (different secret).
    """
    payload_data = await _verify_and_parse(request, settings.RAZORPAYX_WEBHOOK_SECRET)
    await PayoutService(db).handle_payout_webhook(
        event=payload_data.event,
        payload=payload_data.payload,
    )
    return {"status": "ok"}
