"""
Booking factory for test fixtures.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, BookingStatus, PaymentMethod, PaymentStatus


class BookingFactory:
    @staticmethod
    async def create(
        db: AsyncSession,
        consumer_id: uuid.UUID,
        provider_id: uuid.UUID,
        service_id: uuid.UUID | None = None,
        address_id: uuid.UUID | None = None,
        status: BookingStatus = BookingStatus.REQUESTED,
        payment_status: PaymentStatus = PaymentStatus.PENDING,
        service_amount: float = 100.0,
        platform_commission: float = 20.0,
        provider_payout: float = 80.0,
        doorstep_otp: str = "1234",
        payment_method: PaymentMethod = PaymentMethod.ONLINE,
        razorpay_order_id: str | None = None,
    ) -> Booking:
        booking = Booking(
            id=uuid.uuid4(),
            consumer_id=consumer_id,
            provider_id=provider_id,
            service_id=service_id or uuid.uuid4(),
            consumer_address_id=address_id or uuid.uuid4(),
            status=status,
            payment_status=payment_status,
            doorstep_otp=doorstep_otp,
            service_amount=service_amount,
            platform_commission=platform_commission,
            provider_payout=provider_payout,
            payment_method=payment_method,
            razorpay_order_id=razorpay_order_id,
        )
        db.add(booking)
        await db.flush()
        return booking
