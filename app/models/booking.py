import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Numeric, String, func
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
    EXPIRED = "expired"
    REFUND_PROCESSING = "refund_processing"
    REFUND_FAILED = "refund_failed"


class SettlementStatus(str, enum.Enum):
    """Provider-payout lifecycle. Driven by ``PayoutService`` — every read/write of
    ``bookings.settlement_status`` goes through it."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    # Completed payout later clawed back by the bank / RazorpayX. Distinct from
    # FAILED (never paid): REVERSED means money went out and came back.
    REVERSED = "reversed"


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
    # The catalogue used to live directly on this row via `service_id`. It's
    # in `booking_items` now (one row per service in the cart) — a single
    # booking can carry more than one service, dispatched to one provider who
    # covers all of them (matching-policy (i); see BookingItem module docstring
    # for the alternatives that were rejected). All existing consumers of
    # `booking.service_id` were updated to read via `booking.items`; see the
    # migration for the backfill that moved every pre-existing booking into a
    # 1-item cart.
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
    # These three are sums over `items` — snapshotted here rather than derived
    # on read because payments/payouts/earnings read them from long-lived rows
    # (a payment row is retained years after the booking) and a per-read
    # subquery on the item table would show up on every listing.
    #
    # Money is Numeric, never Float (SVC-150): a Float cannot hold 80.07 exactly,
    # and truncating that to paise underpaid the provider by ₹0.01 on ~1 in 22
    # amounts. Scale 2 also makes the column round imprecise input on write.
    service_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    platform_commission: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    provider_payout: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Coupon reduction applied at booking creation. Comes out of the
    # platform side — provider_payout stays anchored to service_amount.
    # See coupon module docstring for the money split. Numeric per SVC-150
    # (see the field comment above); brought under the same rule as the
    # rest of the money in SVC-39 G31.
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    # Optional gratuity added by the consumer after completion. 100% flows
    # to the provider — added on top of provider_payout at payout time.
    # Kept on the booking row (rather than the review row) so a consumer
    # who tips but skips leaving a review is still tipping.
    tip_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    payment_method: Mapped[PaymentMethod | None] = mapped_column(Enum(PaymentMethod))
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING
    )
    # Provider-payout lifecycle. Driven by ``PayoutService`` — see the enum's
    # docstring. ``_owed_filter`` gates on ``PENDING`` so a booking already in
    # PROCESSING / COMPLETED / FAILED / REVERSED never re-enters the payout batch.
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
    # SVC-177 (§4 T+1 gate): timestamp of when Razorpay settled this booking's
    # payment to our bank account. NULL until the settlement webhook
    # (`settlement.processed`) confirms the money is in our hands. The wallet
    # gate excludes credits whose backing booking has `settled_at IS NULL` so
    # we never pay a provider out of money we haven't received yet. Cash
    # bookings stay NULL indefinitely (the money went to the provider at the
    # door — there's no Razorpay settlement) but they never credit the wallet
    # either, so the gate is a no-op for them.
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # `lazy="raise_on_sql"` — every response path that renders items calls
    # BookingService.attach_items() explicitly (mirroring the attach_addresses
    # convention SVC-170 established). Caller who forgets fails loudly here
    # rather than triggering MissingGreenlet inside pydantic serialisation, or
    # — worse — one implicit lookup per row on the list endpoint.
    items: Mapped[list["BookingItem"]] = relationship(
        "BookingItem",
        back_populates="booking",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
    )
    # Read on a response path (SVC-170: `BookingResponse.address`), and every one
    # of those runs under AsyncSession — where an implicit lazy load is not a slow
    # query but a MissingGreenlet 500, and on `GET /bookings/` would be one per
    # row. `raise_on_sql` turns "nobody loaded this" into a named error at the
    # point of the mistake instead. Paths that need it call
    # `BookingService.attach_addresses`, which populates it in one query for any
    # number of bookings; there is deliberately no lazy fallback to paper over a
    # path that forgets.
    consumer_address: Mapped["Address"] = relationship("Address", lazy="raise_on_sql")
    # Ordered and raise_on_sql for the same reason `items` and
    # `consumer_address` are (SVC-248). `BookingResponse.status_history`
    # serialises this, so a read path that forgets to hydrate must fail by
    # name here rather than as a MissingGreenlet inside pydantic — and on the
    # list route an implicit load would be one query per row.
    #
    # `order_by` rather than sorting at the call site: oldest-first is what a
    # timeline means, and putting it on the relationship keeps that true for
    # any future loader, not just `BookingService.attach_status_logs`.
    status_logs: Mapped[list["BookingStatusLog"]] = relationship(
        "BookingStatusLog",
        back_populates="booking",
        lazy="raise_on_sql",
        order_by="BookingStatusLog.created_at",
    )
    review: Mapped["Review"] = relationship("Review", back_populates="booking", uselist=False)
    # SVC-186 (G29): plural, not scalar. `44a9651a7ef8_drop_payment_unique_constraint`
    # deliberately allowed multiple payment rows per booking (failed attempts +
    # the captured one), and SVC-175 (G6) further pinned that shape at the DB
    # level. A scalar relationship (`uselist=False`) would raise
    # `MultipleResultsFound` on exactly the bookings that had a retry — the
    # ones most likely to be under investigation for the refund flow.
    # Consumers pick the CAPTURED row themselves: for the common case,
    # `PaymentService._find_payment_by_razorpay_id` already keys on the
    # Razorpay payment id, which is unique per SVC-175's partial index.
    payments: Mapped[list["Payment"]] = relationship("Payment", back_populates="booking")
