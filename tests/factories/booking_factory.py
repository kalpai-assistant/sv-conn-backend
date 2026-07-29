"""
Booking factory for test fixtures.
"""
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.booking import Booking, BookingStatus, PaymentMethod, PaymentStatus
from app.utils.money import to_decimal


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
            # Money columns are Numeric/Decimal (SVC-150). Tests pass plain floats
            # for readability, so coerce here — otherwise the in-memory object holds
            # a float until it round-trips through the DB, and any Decimal
            # arithmetic against it raises TypeError.
            service_amount=to_decimal(service_amount),
            platform_commission=to_decimal(platform_commission),
            provider_payout=to_decimal(provider_payout),
            payment_method=payment_method,
            razorpay_order_id=razorpay_order_id,
        )
        db.add(booking)
        await db.flush()
        return booking
