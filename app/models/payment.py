import uuid
import enum
from datetime import datetime
from sqlalchemy import Float, String, ForeignKey, DateTime, func, Enum, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class PaymentRecordStatus(str, enum.Enum):
    CREATED = "created"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentRecordMethod(str, enum.Enum):
    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"
    CASH = "cash"


class Payment(Base):
    """
    Ledger record for a completed payment transaction.
    One payment record per booking.
    """

    __tablename__ = "payments"

    __table_args__ = (
        # Allow multiple payment records per booking (to track failed attempts)
        # The CAPTURED record is the authoritative one (check status field)
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False
    )
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(255))
    razorpay_order_id: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    platform_fee: Mapped[float] = mapped_column(Float, nullable=False)
    provider_settlement: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[PaymentRecordStatus] = mapped_column(
        Enum(PaymentRecordStatus), default=PaymentRecordStatus.CREATED
    )
    method: Mapped[PaymentRecordMethod | None] = mapped_column(Enum(PaymentRecordMethod))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    booking: Mapped["Booking"] = relationship("Booking", back_populates="payment")
