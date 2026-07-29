import uuid
import enum
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Numeric, ForeignKey, Enum, DateTime, func, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class BookingStatus(str, enum.Enum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    EN_ROUTE = "en_route"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PaymentMethod(str, enum.Enum):
    CASH = "cash"
    ONLINE = "online"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"
    # Reserved for the three-axis design — not used at runtime yet.
    EXPIRED = "expired"
    REFUND_PROCESSING = "refund_processing"
    REFUND_FAILED = "refund_failed"


class SettlementStatus(str, enum.Enum):
    """Provider-payout lifecycle. Reserved for future use — not read at runtime yet."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    # Completed payout later clawed back by the bank / RazorpayX. Distinct from
    # FAILED (never paid): REVERSED means money went out and came back.
    REVERSED = "reversed"


# Valid state transitions — enforced in booking_service.py
VALID_TRANSITIONS: dict[BookingStatus, list[BookingStatus]] = {
    BookingStatus.REQUESTED: [BookingStatus.ACCEPTED, BookingStatus.EXPIRED, BookingStatus.CANCELLED],
    BookingStatus.ACCEPTED: [BookingStatus.EN_ROUTE, BookingStatus.CANCELLED],
    BookingStatus.EN_ROUTE: [BookingStatus.IN_PROGRESS, BookingStatus.CANCELLED],
    BookingStatus.IN_PROGRESS: [BookingStatus.COMPLETED],
    BookingStatus.COMPLETED: [],
    BookingStatus.CANCELLED: [],
    BookingStatus.EXPIRED: [],
}


class Booking(Base):
    __tablename__ = "bookings"

    __table_args__ = (
        # Partial indexes for efficient active-booking lookups
        Index(
            "idx_bookings_consumer_status",
            "consumer_id",
            "status",
            postgresql_where="status NOT IN ('COMPLETED', 'CANCELLED', 'EXPIRED')",
        ),
        Index(
            "idx_bookings_provider_status",
            "provider_id",
            "status",
            postgresql_where="status NOT IN ('COMPLETED', 'CANCELLED', 'EXPIRED')",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    consumer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id")
    )
    consumer_address_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus), default=BookingStatus.REQUESTED, index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doorstep_otp: Mapped[str] = mapped_column(String(4))
    # Money is Numeric, never Float (SVC-150): a Float cannot hold 80.07 exactly, and
    # truncating that to paise underpaid the provider by ₹0.01 on ~1 in 22 amounts.
    # Scale 2 also makes the column itself round imprecise input on write.
    service_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    platform_commission: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    provider_payout: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_method: Mapped[PaymentMethod | None] = mapped_column(Enum(PaymentMethod))
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING
    )
    # Provider-payout lifecycle. Reserved for future use — not read at runtime yet.
    settlement_status: Mapped[SettlementStatus] = mapped_column(
        Enum(SettlementStatus), default=SettlementStatus.PENDING, server_default="PENDING"
    )
    # RazorpayX payout this booking was settled in; links the payout webhook back to
    # the exact batch so concurrent payouts settle independently.
    settlement_payout_id: Mapped[str | None] = mapped_column(String(255))
    # Why the most recent payout attempt didn't succeed (e.g. RazorpayX rejection
    # reason). Set on payout.rejected; cleared when the next attempt starts.
    settlement_failure_reason: Mapped[str | None] = mapped_column(String(500))
    razorpay_order_id: Mapped[str | None] = mapped_column(String(255))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    consumer: Mapped["User"] = relationship(
        "User", back_populates="bookings_as_consumer", foreign_keys=[consumer_id]
    )
    provider: Mapped["User"] = relationship(
        "User", back_populates="bookings_as_provider", foreign_keys=[provider_id]
    )
    service: Mapped["Service"] = relationship("Service")
    consumer_address: Mapped["Address"] = relationship("Address")
    status_logs: Mapped[list["BookingStatusLog"]] = relationship(
        "BookingStatusLog", back_populates="booking"
    )
    review: Mapped["Review"] = relationship(
        "Review", back_populates="booking", uselist=False
    )
    payment: Mapped["Payment"] = relationship(
        "Payment", back_populates="booking", uselist=False
    )
