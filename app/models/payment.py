import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Numeric, String, func
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

    # No unique constraint on booking_id: multiple payment records per booking are
    # allowed (to track failed attempts). The CAPTURED record is the authoritative
    # one (check status field).
    __table_args__ = (
        # Backs the admin bookings list's "has a disputed payment" lookup. Partial,
        # so it stays tiny — disputes are rare.
        Index(
            "idx_payments_disputed_booking",
            "booking_id",
            postgresql_where="disputed_at IS NOT NULL",
        ),
        # SVC-175 (G6): one row per real Razorpay payment id. The old unique on
        # booking_id was dropped for the multiple-attempts pattern, and nothing
        # replaced it — verify_payment could write N duplicates for one payment.
        # Partial so NULLs (the failed branch before Razorpay assigns an id)
        # still coexist per booking.
        Index(
            "uix_payments_razorpay_payment_id",
            "razorpay_payment_id",
            unique=True,
            postgresql_where="razorpay_payment_id IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False
    )
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(255))
    razorpay_order_id: Mapped[str | None] = mapped_column(String(255))
    # Numeric, not Float — see the note on Booking's money columns (SVC-150).
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    platform_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    provider_settlement: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[PaymentRecordStatus] = mapped_column(
        Enum(PaymentRecordStatus), default=PaymentRecordStatus.CREATED
    )
    method: Mapped[PaymentRecordMethod | None] = mapped_column(Enum(PaymentRecordMethod))
    # Set when Razorpay reports a chargeback (payment.dispute.created). Beta keeps
    # this as a flag only: status/settlement are never moved automatically, an admin
    # resolves the dispute out of band.
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    # SVC-186 (G29): back_populates="payments" (plural), matching the list-shaped
    # relationship on Booking. A booking may have several payment rows — failed
    # attempts + the captured one; see the note there.
    booking: Mapped["Booking"] = relationship("Booking", back_populates="payments")
