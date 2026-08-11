"""Coupon domain: list-available, validate, apply-on-booking-create.

The service owns the two rules a coupon has to satisfy at every touchpoint
— eligibility (is this code even usable by this user right now?) and
computation (how much rupee value does it produce on this cart total?).
Kept in one module so those two questions can't drift: the "available"
list must never contain a coupon that "validate" refuses, and "validate"
must produce the same discount that "apply" writes to the booking.

Times are timezone-aware UTC throughout. Coupon.valid_from and .valid_until
are declared timezone=True; comparisons use ``datetime.now(UTC)`` and never
``.utcnow()`` (which returns a naive datetime and would raise TypeError
against the aware DB values before it silently produced wrong answers).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CouponInvalid
from app.models.coupon import Coupon, CouponRedemption, DiscountType


class CouponService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Read: what's available to this user right now
    # ------------------------------------------------------------------

    async def list_available(self, user_id: uuid.UUID) -> list[Coupon]:
        """Coupons this user could apply to a new booking today.

        Filters mirror what ``validate_and_apply`` enforces so this list is
        never a lie: a coupon that would be refused at validate time must
        not appear here. That means:

        * active (``is_active`` true);
        * within its time window (``valid_from <= now < valid_until``);
        * either global (``user_id IS NULL``) or belonging to this user;
        * not exhausted by this user (per-user redemptions
          < ``usage_limit_per_user``);
        * not exhausted globally (total redemptions < ``usage_limit``, when
          set).

        The per-user usage count uses a correlated subquery keyed on
        ``coupon_id + user_id``; the global count another one keyed on
        ``coupon_id``. Two subqueries per row of a small table with a small
        result set — the alternative (fetch candidates, count in Python)
        would round-trip once per row.
        """
        now = datetime.now(UTC)

        per_user_uses = (
            select(func.count(CouponRedemption.id))
            .where(
                CouponRedemption.coupon_id == Coupon.id,
                CouponRedemption.user_id == user_id,
            )
            .correlate(Coupon)
            .scalar_subquery()
        )
        global_uses = (
            select(func.count(CouponRedemption.id))
            .where(CouponRedemption.coupon_id == Coupon.id)
            .correlate(Coupon)
            .scalar_subquery()
        )

        stmt = (
            select(Coupon)
            .where(
                Coupon.is_active.is_(True),
                Coupon.valid_from <= now,
                Coupon.valid_until > now,
                or_(Coupon.user_id.is_(None), Coupon.user_id == user_id),
                per_user_uses < Coupon.usage_limit_per_user,
                or_(Coupon.usage_limit.is_(None), global_uses < Coupon.usage_limit),
            )
            .order_by(Coupon.valid_until.asc())
        )
        return list((await self.db.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------
    # Read: validate a specific code against a cart
    # ------------------------------------------------------------------

    async def validate(
        self, code: str, user_id: uuid.UUID, cart_amount: float
    ) -> tuple[Coupon, float]:
        """Returns ``(coupon, discount_rupees)`` or raises ``CouponInvalid``.

        Every reason to refuse is folded into one exception type — the
        client renders "Coupon not applicable" for the whole class. A
        finer-grained message could name why ("expired" vs "min ₹499"), but
        beta doesn't need it and we're not building an enumeration ledger
        that then has to stay in sync between server and client.
        """
        coupon = await self._load_active(code, user_id)
        if coupon is None:
            raise CouponInvalid("Coupon not applicable")

        if coupon.min_order_amount is not None and cart_amount < coupon.min_order_amount:
            raise CouponInvalid(
                f"Minimum order ₹{int(coupon.min_order_amount)} required for this coupon"
            )

        if not await self._has_remaining_uses(coupon, user_id):
            raise CouponInvalid("Coupon usage limit reached")

        discount = self._compute_discount(coupon, cart_amount)
        # A degenerate case that would otherwise pass — a percent coupon on
        # a ₹0 cart. Guarding at the boundary keeps the endpoint honest;
        # the cart already refuses empty item lists so this is defence in
        # depth.
        if discount <= 0:
            raise CouponInvalid("Coupon does not reduce this order")

        return coupon, discount

    # ------------------------------------------------------------------
    # Write: apply on booking creation
    # ------------------------------------------------------------------

    async def apply_to_booking(
        self,
        coupon: Coupon,
        booking_id: uuid.UUID,
        user_id: uuid.UUID,
        discount_amount: float,
    ) -> CouponRedemption:
        """Create the redemption row.

        The caller must have already computed ``discount_amount`` via
        ``validate()``, and must be inside the same DB transaction as the
        booking insert — the ``UNIQUE(booking_id)`` on
        ``coupon_redemptions`` prevents double-apply within one commit,
        and being inside the booking's transaction means a failed
        redemption rolls the whole booking back rather than persisting a
        booking with no discount row that the ``discount_amount`` on the
        booking implicitly refers to.
        """
        redemption = CouponRedemption(
            coupon_id=coupon.id,
            user_id=user_id,
            booking_id=booking_id,
            discount_applied=discount_amount,
        )
        self.db.add(redemption)
        return redemption

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_active(self, code: str, user_id: uuid.UUID) -> Coupon | None:
        """Fetch by code, applying the window/active/ownership filters that
        every read shares. Case-insensitive on code.
        """
        now = datetime.now(UTC)
        stmt = select(Coupon).where(
            func.lower(Coupon.code) == code.lower(),
            Coupon.is_active.is_(True),
            Coupon.valid_from <= now,
            Coupon.valid_until > now,
            or_(Coupon.user_id.is_(None), Coupon.user_id == user_id),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _has_remaining_uses(self, coupon: Coupon, user_id: uuid.UUID) -> bool:
        """Both per-user and global caps checked.

        Two simple COUNT queries rather than one clever aggregate: at the
        scale a single coupon can be redeemed (bounded by
        ``usage_limit``), the extra round-trip is unmeasurable, and the
        readability of "count all, count by user" beats a CASE-in-SUM by
        a wide margin.
        """
        per_user = (
            await self.db.execute(
                select(func.count(CouponRedemption.id)).where(
                    CouponRedemption.coupon_id == coupon.id,
                    CouponRedemption.user_id == user_id,
                )
            )
        ).scalar_one()
        if per_user >= coupon.usage_limit_per_user:
            return False

        if coupon.usage_limit is not None:
            total = (
                await self.db.execute(
                    select(func.count(CouponRedemption.id)).where(
                        CouponRedemption.coupon_id == coupon.id
                    )
                )
            ).scalar_one()
            if total >= coupon.usage_limit:
                return False
        return True

    @staticmethod
    def _compute_discount(coupon: Coupon, cart_amount: float) -> float:
        """The rupee value the coupon reduces the cart by.

        - PERCENT: percent-of-cart, capped at ``max_discount`` if set,
          then capped at the cart amount itself.
        - FLAT: min(value, cart_amount). No refund of unused value.

        Rounded to two decimal places — payments deal in paise but the
        rest of this codebase keeps rupees as floats. Consistency here
        rather than a lone integer money type.
        """
        if coupon.discount_type is DiscountType.PERCENT:
            raw = cart_amount * (coupon.discount_value / 100.0)
            if coupon.max_discount is not None:
                raw = min(raw, coupon.max_discount)
        else:
            raw = coupon.discount_value
        return round(min(raw, cart_amount), 2)
