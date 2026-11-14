"""A customer's dispute about a booking, and how support settled it (SVC-431).

Lifecycle: ``RAISED`` → ``IN_REVIEW`` (an admin picked it up; optional) →
``RESOLVED`` → ``CLOSED``. A Razorpay refund stays ``RESOLVED`` with
``refund_status`` PENDING until the ``refund.processed`` webhook closes it; a
``refund.failed`` leaves it ``RESOLVED`` with ``refund_status`` FAILED. A manual
refund (cash, paid outside the app) and a no-refund resolution close at once.

Money: ``refund_amount`` is rupees, like every money column. The dispute's own
Razorpay refund id lives here, never in ``payments.refund_id``, which belongs to
the cancel refund. A dispute refund never touches ``booking.payment_status``,
``payments.status`` or ``payments.refund_*``: the platform absorbs it, and the
provider's payout is unchanged (``PayoutService._owed_filter``).

The enum-like columns are varchar + CHECK, not Postgres ENUMs (SVC-436
convention), so adding a value is a one-line constraint swap.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

DISPUTE_REASON_CODES = ("NO_SHOW", "QUALITY", "BILLING", "BEHAVIOR", "OTHER")
DISPUTE_STATUSES = ("RAISED", "IN_REVIEW", "RESOLVED", "CLOSED")
DISPUTE_OPEN_STATUSES = ("RAISED", "IN_REVIEW")
DISPUTE_RESOLUTION_TYPES = ("FULL_REFUND", "PARTIAL_REFUND", "NO_REFUND")
DISPUTE_REFUND_CHANNELS = ("RAZORPAY", "MANUAL")
DISPUTE_REFUND_STATUSES = ("PENDING", "PROCESSED", "FAILED")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class BookingDispute(Base):
    __tablename__ = "booking_disputes"
    __table_args__ = (
        CheckConstraint(_in("reason_code", DISPUTE_REASON_CODES), name="ck_dispute_reason_code"),
        CheckConstraint(_in("status", DISPUTE_STATUSES), name="ck_dispute_status"),
        CheckConstraint(
            f"resolution_type IS NULL OR {_in('resolution_type', DISPUTE_RESOLUTION_TYPES)}",
            name="ck_dispute_resolution_type",
        ),
        CheckConstraint(
            f"refund_channel IS NULL OR {_in('refund_channel', DISPUTE_REFUND_CHANNELS)}",
            name="ck_dispute_refund_channel",
        ),
        CheckConstraint(
            f"refund_status IS NULL OR {_in('refund_status', DISPUTE_REFUND_STATUSES)}",
            name="ck_dispute_refund_status",
        ),
        CheckConstraint(
            "refund_amount IS NULL OR refund_amount > 0", name="ck_dispute_refund_amount"
        ),
        # At most one open dispute per booking.
        Index(
            "uix_booking_disputes_open_booking",
            "booking_id",
            unique=True,
            postgresql_where="status IN ('RAISED', 'IN_REVIEW')",
        ),
        Index(
            "uix_booking_disputes_razorpay_refund_id",
            "razorpay_refund_id",
            unique=True,
            postgresql_where="razorpay_refund_id IS NOT NULL",
        ),
        Index("idx_booking_disputes_booking", "booking_id"),
        Index("idx_booking_disputes_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False
    )
    # The booking's customer. v1 disputes are logged by an admin on their behalf.
    raised_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # The admin who logged it; NULL once a consumer raise-dispute endpoint exists.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reason_code: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="RAISED", server_default="RAISED"
    )
    resolution_type: Mapped[str | None] = mapped_column(String(16))
    refund_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    refund_channel: Mapped[str | None] = mapped_column(String(16))
    razorpay_refund_id: Mapped[str | None] = mapped_column(String(255))
    refund_status: Mapped[str | None] = mapped_column(String(16))
    refund_failed_reason: Mapped[str | None] = mapped_column(Text)
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
