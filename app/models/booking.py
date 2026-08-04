import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, String, func
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


# Valid state transitions — enforced in booking_service.py
VALID_TRANSITIONS: dict[BookingStatus, list[BookingStatus]] = {
    BookingStatus.REQUESTED: [
        BookingStatus.ACCEPTED,
        BookingStatus.EXPIRED,
        BookingStatus.CANCELLED,
    ],
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consumer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id"))
    consumer_address_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus), default=BookingStatus.REQUESTED, index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Nullable because we zero it out as soon as the transition it protects
    # (en_route → in_progress) happens — no reason to keep a live plaintext
    # secret in a row that will outlive its use by weeks. Also cleared on
    # cancellation. See booking_service.transition_status.
    doorstep_otp: Mapped[str | None] = mapped_column(String(4), nullable=True)
    service_amount: Mapped[float] = mapped_column(Float, nullable=False)
    platform_commission: Mapped[float] = mapped_column(Float, nullable=False)
    provider_payout: Mapped[float] = mapped_column(Float, nullable=False)
    payment_method: Mapped[PaymentMethod | None] = mapped_column(Enum(PaymentMethod))
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING
    )
    razorpay_order_id: Mapped[str | None] = mapped_column(String(255))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
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
    review: Mapped["Review"] = relationship("Review", back_populates="booking", uselist=False)
    payment: Mapped["Payment"] = relationship("Payment", back_populates="booking", uselist=False)
