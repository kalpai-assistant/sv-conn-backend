"""One row per service in a booking's cart.

A booking can now carry more than one service (SVC-multi-svc). The choice we
made — and the one this model exists to enforce — is a single booking that
groups the entire cart, dispatched to one provider who can perform *all*
services in it. The alternative (a booking per service, grouped by a
``cart_id`` on Booking) was rejected because it would multiply the number of
rows the dispatch engine has to broadcast, force the customer to sit through
multiple provider assignments per "order", and split payments into N Razorpay
orders — each of which is a chance to leave money on the table when only some
succeed. Grouping in the model keeps the dispatch, tracking, payment, and
review surfaces single-object.

Everything about the *money* on this row is a snapshot at booking time.
``unit_price`` and the derived ``total_price`` / ``platform_commission`` /
``provider_payout`` freeze what the customer agreed to pay for this item at
the moment of booking; a later price update on ``services.base_price`` must
not retroactively change the amount owed. The parent booking's aggregate
``service_amount``/``platform_commission``/``provider_payout`` are the sums
of these — kept denormalized on Booking rather than computed on read because
several long-lived rows (payments, payouts, earnings dashboard) reference
the totals and can't afford a subquery per read.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BookingItem(Base):
    __tablename__ = "booking_items"

    __table_args__ = (
        # Same service can't appear twice on one booking — the cart uses `quantity`
        # to express "two of the same". Enforced at the DB level so a bug in the
        # service layer can't produce two rows the payment code would sum but
        # provider matching would see as one.
        UniqueConstraint("booking_id", "service_id", name="uix_booking_item_service"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # Cascade delete: if a booking is ever hard-deleted (admin flow, GDPR
        # deletion) the items go with it. Bookings are otherwise not deleted —
        # they transition to CANCELLED/EXPIRED and stay for the audit trail.
        ForeignKey("bookings.id", ondelete="CASCADE"),
        index=True,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Price *at booking time*, not the live services.base_price. See module
    # docstring — a later catalogue price change must not retro-alter the bill.
    # Numeric per SVC-150 (see the note on Booking's money columns): item-level
    # totals are summed into the booking's aggregates and then paid, so a float
    # here would leak the same 80.07-can't-be-represented rounding bug into
    # every multi-item cart. SVC-181 brought the last of the money columns
    # onto the same rule.
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # unit_price * quantity, denormalized. Cheaper than a derived column on read
    # and matches how the parent Booking stores its totals.
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Per-item split, so the earnings dashboard can attribute revenue back to a
    # specific service the provider performed (the current per-booking payout
    # would lump three services into one line, hiding which one earned what).
    platform_commission: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    provider_payout: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    booking: Mapped["Booking"] = relationship("Booking", back_populates="items")
    # `lazy="raise_on_sql"` for the same reason Booking.consumer_address uses it:
    # every response path that renders items calls attach_services() explicitly,
    # and a caller who forgets should fail loudly here (with a name) rather than
    # trigger a MissingGreenlet inside pydantic — or, on a list route, one
    # implicit lookup per row.
    service: Mapped["Service"] = relationship("Service", lazy="raise_on_sql")
