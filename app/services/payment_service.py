"""
Payment service: Razorpay order creation, signature verification, and webhook handling.
"""
import hmac
import hashlib
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, PaymentStatus
from app.models.payment import Payment, PaymentRecordStatus, PaymentRecordMethod
from app.models.provider_profile import ProviderProfile
from app.utils.razorpay_client import razorpay_client
from app.exceptions import BookingNotFound, PaymentError
from app.config import get_settings

settings = get_settings()


class PaymentService:
    """Manages Razorpay order lifecycle and payment verification."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Order creation
    # ------------------------------------------------------------------

    async def create_order(self, booking_id: UUID) -> dict:
        """
        Create a Razorpay order for the booking amount.

        For beta the payment lands fully in the platform settlement account — no
        Route ``transfers`` split, so a provider does not need a linked account
        (PAY-1 / SVC-130). Provider payouts are handled separately via RazorpayX
        (PAY-3). Returns order details for frontend checkout.
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)

        amount_paise = int(booking.service_amount * 100)

        order_data = {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": str(booking.id),
        }

        order = razorpay_client.order.create(data=order_data)
        booking.razorpay_order_id = order["id"]
        await self.db.flush()

        return {
            "razorpay_order_id": order["id"],
            "amount": amount_paise,
            "currency": "INR",
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        }

    # ------------------------------------------------------------------
    # Payment verification
    # ------------------------------------------------------------------

    async def verify_payment(
        self,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> Payment:
        """
        Verify the Razorpay HMAC-SHA256 signature.
        On success, create a Payment record and mark the booking as paid.
        """
        expected = hmac.new(
            key=settings.RAZORPAY_KEY_SECRET.encode(),
            msg=f"{razorpay_order_id}|{razorpay_payment_id}".encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, razorpay_signature):
            raise PaymentError("Payment signature verification failed")

        booking = await self._find_booking_by_order(razorpay_order_id)

        payment = Payment(
            booking_id=booking.id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_order_id=razorpay_order_id,
            amount=booking.service_amount,
            platform_fee=booking.platform_commission,
            provider_settlement=booking.provider_payout,
            status=PaymentRecordStatus.CAPTURED,
            method=PaymentRecordMethod.UPI,  # Razorpay resolves method via webhook
        )
        self.db.add(payment)

        booking.payment_status = PaymentStatus.PAID
        await self.db.flush()

        return payment

    # ------------------------------------------------------------------
    # Webhook handler
    # ------------------------------------------------------------------

    async def handle_webhook(self, event: str, payload: dict) -> None:
        """
        Handle Razorpay webhook events (idempotent).
        Primary events: payment.captured, payment.failed, transfer.processed.
        """
        if event == "payment.captured":
            payment_entity = payload.get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            if order_id:
                booking = await self._find_booking_by_order(order_id)
                if booking and booking.payment_status != PaymentStatus.PAID:
                    booking.payment_status = PaymentStatus.PAID
                    await self.db.flush()

        elif event == "payment.failed":
            payment_entity = payload.get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            if order_id:
                try:
                    booking = await self._find_booking_by_order(order_id)
                    # Create a failed payment record for audit trail
                    payment = Payment(
                        booking_id=booking.id,
                        razorpay_order_id=order_id,
                        razorpay_payment_id=payment_entity.get("id", ""),
                        amount=booking.service_amount,
                        platform_fee=booking.platform_commission,
                        provider_settlement=booking.provider_payout,
                        status=PaymentRecordStatus.FAILED,
                        method=PaymentRecordMethod.UPI,
                    )
                    self.db.add(payment)
                    await self.db.flush()
                except Exception:
                    pass  # Booking may not exist; log silently

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_provider_profile(self, provider_user_id: UUID) -> ProviderProfile:
        result = await self.db.execute(
            select(ProviderProfile).where(ProviderProfile.user_id == provider_user_id)
        )
        profile = result.scalar_one_or_none()
        if not profile or not profile.razorpay_linked_account_id:
            raise PaymentError("Provider is not set up for payouts")
        return profile

    async def _find_booking_by_order(self, order_id: str) -> Booking:
        result = await self.db.execute(
            select(Booking).where(Booking.razorpay_order_id == order_id)
        )
        booking = result.scalar_one_or_none()
        if not booking:
            raise PaymentError(f"No booking found for order {order_id}")
        return booking
