"""Coupons and their redemptions.

The coupon domain is deliberately kept small for beta. What lives here:

* ``Coupon`` — the offer itself: a code, a discount rule, when it is valid,
  who may use it, and how many times.
* ``CouponRedemption`` — a use of a coupon on a specific booking. One row
  per successful application; per-user usage limits are enforced by counting
  these rows and there is a DB-level ``UNIQUE(coupon_id, booking_id)`` so a
  booking cannot be double-billed the same coupon.

What does *not* live here yet:
* No stacking. One coupon per booking, enforced by ``UNIQUE(booking_id)``
  on ``coupon_redemptions``.
* No user-visible admin surface — coupon rows are inserted by hand or by
  the (future) referral job. The consumer endpoints only list what a user
  can use and validate a specific code.
* No cash-refund of unused coupon value. If the customer's cart total drops
  below the coupon's flat value at booking time we cap the discount at the
  cart amount; the leftover is not credited anywhere. This is the safe
  default — the alternative is a wallet, which we've explicitly deferred.

Money on a redeemed booking, given the coupon reduced the bill by ``D``:

- ``booking.service_amount``: unchanged. Original sum-of-items. Anchors
  the commission split so the platform's cut is unaffected by the amount
  the customer *actually* paid.
- ``booking.discount_amount``: ``D``. What the platform ate.
- ``booking.provider_payout``: unchanged. The pro is paid on what the job
  was worth, not what the customer paid.
- Effective charge to customer: ``service_amount - D + tip_amount``.

This split (platform absorbs the coupon; provider paid on original) is the
industry-standard behaviour and the model the payment code below relies
on. If we ever want to run "provider funds the discount" campaigns that
has to be an explicit override, not the default.
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DiscountType(str, enum.Enum):
    """How ``Coupon.discount_value`` reduces the bill.

    ``PERCENT``: value is a percent (0–100). ``max_discount`` caps the
    resulting rupee amount — a "20% off" without a cap on a big cart would
    give away the whole store.

    ``FLAT``: value is a rupee amount. Capped at the cart total at
    application time (see the coupon service) so a ₹500-off coupon on a
    ₹200 cart discounts ₹200, not ₹500.
    """

    PERCENT = "percent"
    FLAT = "flat"


class Coupon(Base):
    __tablename__ = "coupons"

    __table_args__ = (
        # Human-typed field. Case-insensitivity is enforced by lower()ing at
        # write and read time — a CITEXT column would work but pins us to
        # postgres for no meaningful gain here.
        UniqueConstraint("code", name="uix_coupon_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Short user-facing code (FIRST20, RAVIREF, etc.). Stored lowercased;
    # normalise on write and match with lower() on read. 32 chars is
    # generous — referral codes are ~8 characters, promo codes ~10.
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    discount_type: Mapped[DiscountType] = mapped_column(Enum(DiscountType), nullable=False)
    # For PERCENT this is a whole number 0–100. For FLAT, rupees. Numeric per
    # SVC-150 (see the note on Booking's money columns); brought under the same
    # rule as the rest of the money in SVC-181 so the discount that leaves
    # `_compute_discount` and lands on the booking row goes through one type
    # end-to-end.
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Nullable = no cap. Only meaningful for PERCENT coupons; ignored for
    # FLAT (the flat amount is its own cap).
    max_discount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Nullable = no minimum. Cart total must be at least this to qualify.
    min_order_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # Nullable = global coupon (anyone may use). Non-null = tied to one user
    # — this is how referral bonuses are represented (both giver and
    # receiver get their own personal coupon row).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )

    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Total redemptions across all users. Nullable = unlimited.
    usage_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-user cap. 1 by default matches "first-booking coupon" and the
    # referral reward pattern. Set > 1 for open-ended promotional coupons.
    usage_limit_per_user: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Kill switch. A deactivated coupon disappears from the user's list and
    # is refused on validate, without needing to backdate valid_until.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    redemptions: Mapped[list["CouponRedemption"]] = relationship(
        "CouponRedemption", back_populates="coupon", cascade="all, delete-orphan"
    )


class CouponRedemption(Base):
    """One row per successful application of a coupon to a booking."""

    __tablename__ = "coupon_redemptions"

    __table_args__ = (
        # A single booking can never have two redemptions — matches "one
        # coupon per booking", enforced here so a race between two /coupons/
        # applications on the same cart cannot both win.
        UniqueConstraint("booking_id", name="uix_redemption_booking"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    coupon_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coupons.id"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), index=True
    )
    # The rupee amount actually deducted. Recorded rather than recomputed
    # so the ledger survives a later change to the coupon's discount rule.
    # Numeric per SVC-181 (see the note on Coupon.discount_value); the ledger
    # is money and must match the aggregates it justifies on the booking row.
    discount_applied: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    coupon: Mapped["Coupon"] = relationship("Coupon", back_populates="redemptions")
