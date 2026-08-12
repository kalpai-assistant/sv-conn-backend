"""
Payment service: Razorpay order creation, signature verification, and webhook handling.
"""

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import BookingNotFound, PaymentError
from app.models.booking import Booking, PaymentStatus
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.provider_profile import ProviderProfile
from app.utils.money import to_paise
from app.utils.razorpay_client import razorpay_client

settings = get_settings()
logger = logging.getLogger(__name__)


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
        Route ``transfers`` split, so a provider does not need a linked account.
        Provider payouts are handled separately via RazorpayX. Returns order
        details for frontend checkout.

        SVC-39 G22: charges ``service_amount − discount_amount``. The coupon
        module docstring documents the rule (``effective charge = service_amount
        − D + tip_amount``); tips are added post-completion via ``set_tip``, so
        at order-creation time only the discount applies. Before this fix, every
        coupon booking billed the consumer the full pre-discount amount while
        the coupon was recorded as redeemed.
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)

        # Numeric arithmetic (both operands are Decimal post-SVC-150 + G31), so
        # the subtraction is exact and ``to_paise`` rounds half-up — a ₹100.09
        # booking with a ₹10 coupon bills 9009 paise, not 9008.
        chargeable = booking.service_amount - booking.discount_amount
        amount_paise = to_paise(chargeable)

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
        Primary events: payment.captured, payment.failed, payment.dispute.*.
        """
        if event.startswith("payment.dispute."):
            await self._handle_dispute(event, payload)
            return

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
    # Disputes / chargebacks
    # ------------------------------------------------------------------

    async def _handle_dispute(self, event: str, payload: dict) -> None:
        """
        Record a Razorpay chargeback against its Payment.

        ``payment.dispute.created`` fires when a customer disputes a payment with
        their card issuer; ``.won`` / ``.lost`` follow when it resolves. For beta we
        only stamp ``disputed_at`` and alert — the refund and settlement reversal are
        decided by an admin out of band, so no payment or settlement status is moved
        here (SVC-134).

        Idempotent: Razorpay retries webhooks, and ``disputed_at`` is stamped once so
        a redelivery keeps the original dispute time.
        """
        # Dispute payloads are keyed by payment, not order: the id lives at
        # payload.dispute.entity.payment_id (payload.payment.entity.id carries the
        # same value). The captured/failed branches above match on order_id instead.
        dispute_entity = payload.get("dispute", {}).get("entity", {})
        payment_id = dispute_entity.get("payment_id")
        if not payment_id:
            # Can't map the dispute to anything — surface it rather than 200-ing.
            logger.error("Razorpay %s webhook missing dispute.entity.payment_id", event)
            raise PaymentError("Dispute webhook missing payment id")

        payment = await self._find_payment_by_razorpay_id(payment_id)
        if not payment:
            # A dispute for a payment we have no ledger row for is an anomaly worth
            # investigating, but a retry can't fix it — log loudly and acknowledge.
            logger.error(
                "Razorpay %s for unknown payment %s (dispute=%s)",
                event,
                payment_id,
                dispute_entity.get("id"),
            )
            return

        if event == "payment.dispute.created":
            if payment.disputed_at is None:
                payment.disputed_at = datetime.now(UTC)
                await self.db.flush()
            logger.warning(
                "payment.dispute.created payment=%s booking=%s dispute=%s "
                "amount=%s reason=%s respond_by=%s",
                payment_id,
                payment.booking_id,
                dispute_entity.get("id"),
                dispute_entity.get("amount"),
                dispute_entity.get("reason_code"),
                dispute_entity.get("respond_by"),
            )
            # TODO(SVC-134): Sentry alert + Slack notification. Neither is wired up in
            # the backend yet (the HLD schedules Sentry for Phase 2), so the warning
            # above is the interim signal until that infra lands.

        elif event in ("payment.dispute.won", "payment.dispute.lost"):
            # Deliberately no state change: beta AC leaves refund/settlement to admin.
            logger.warning(
                "%s payment=%s booking=%s dispute=%s (no automated state change)",
                event,
                payment_id,
                payment.booking_id,
                dispute_entity.get("id"),
            )

        else:
            # e.g. payment.dispute.under_review / .action_required / .closed
            logger.info("Ignoring unhandled dispute event: %s", event)

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

    async def _find_payment_by_razorpay_id(self, payment_id: str) -> Payment | None:
        """Look up a ledger row by Razorpay payment id (disputes key on this)."""
        result = await self.db.execute(
            select(Payment)
            .where(Payment.razorpay_payment_id == payment_id)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_booking_by_order(self, order_id: str) -> Booking:
        result = await self.db.execute(select(Booking).where(Booking.razorpay_order_id == order_id))
        booking = result.scalar_one_or_none()
        if not booking:
            raise PaymentError(f"No booking found for order {order_id}")
        return booking
