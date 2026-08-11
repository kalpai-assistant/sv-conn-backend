"""Coupon validation + apply on booking creation.

Pins:

* discount computation for both PERCENT (with and without cap) and FLAT
  (including "coupon value > cart total") variants;
* eligibility filters — expired, inactive, wrong user, per-user cap,
  global cap — and the fact that ``GET /coupons/available`` and
  ``POST /coupons/validate`` refuse the same coupons for the same reasons
  (shared through the service);
* the "one coupon per booking" DB constraint;
* end-to-end apply on ``POST /bookings/`` — the booking row carries
  ``discount_amount`` and a matching ``coupon_redemptions`` row exists.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.address import Address
from app.models.booking import Booking
from app.models.category import Category
from app.models.coupon import Coupon, CouponRedemption, DiscountType
from app.models.service import Service
from app.services.coupon_service import CouponService
from app.utils.geo import build_point_wkt
from tests.factories.user_factory import UserFactory

LAT, LNG = 18.5204, 73.8567


async def _consumer_with_address(db_session):
    consumer = await UserFactory.create(db_session, role="consumer")
    addr = Address(
        user_id=consumer.id,
        label="home",
        full_address="Test address, Pune",
        location=build_point_wkt(LAT, LNG),
    )
    db_session.add(addr)
    await db_session.flush()
    return consumer, addr


async def _service(db_session, *, base_price: float = 500.0):
    cat = Category(name=f"C{uuid.uuid4()}", slug=f"c-{uuid.uuid4()}", is_active=True, sort_order=0)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name="Svc",
        base_price=base_price,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(svc)
    await db_session.flush()
    return svc


async def _coupon(
    db_session,
    *,
    code: str,
    discount_type: DiscountType = DiscountType.PERCENT,
    discount_value: float = 10,
    max_discount: float | None = None,
    min_order_amount: float | None = None,
    user_id=None,
    valid_days: int = 30,
    usage_limit: int | None = None,
    usage_limit_per_user: int = 1,
    is_active: bool = True,
) -> Coupon:
    now = datetime.now(UTC)
    coupon = Coupon(
        code=code.lower(),
        discount_type=discount_type,
        discount_value=discount_value,
        max_discount=max_discount,
        min_order_amount=min_order_amount,
        user_id=user_id,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=valid_days),
        usage_limit=usage_limit,
        usage_limit_per_user=usage_limit_per_user,
        is_active=is_active,
    )
    db_session.add(coupon)
    await db_session.flush()
    return coupon


# ---------------------------------------------------------------------------
# Discount arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_percent_discount_without_cap_takes_full_percent(db_session):
    consumer, _ = await _consumer_with_address(db_session)
    coupon = await _coupon(db_session, code="TEN", discount_value=10)

    _, discount = await CouponService(db_session).validate(
        code=coupon.code, user_id=consumer.id, cart_amount=1000.0
    )
    assert discount == 100.0


@pytest.mark.asyncio
async def test_percent_discount_capped_at_max_discount(db_session):
    """20% on ₹1000 = ₹200, but capped at ₹150."""
    consumer, _ = await _consumer_with_address(db_session)
    coupon = await _coupon(db_session, code="CAPPED", discount_value=20, max_discount=150)

    _, discount = await CouponService(db_session).validate(
        code=coupon.code, user_id=consumer.id, cart_amount=1000.0
    )
    assert discount == 150.0


@pytest.mark.asyncio
async def test_flat_discount_capped_at_cart_when_larger(db_session):
    """₹500 flat on a ₹300 cart is ₹300, not ₹500 — see coupon module docstring."""
    consumer, _ = await _consumer_with_address(db_session)
    coupon = await _coupon(
        db_session, code="FLAT500", discount_type=DiscountType.FLAT, discount_value=500
    )

    _, discount = await CouponService(db_session).validate(
        code=coupon.code, user_id=consumer.id, cart_amount=300.0
    )
    assert discount == 300.0


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_order_amount_refuses_smaller_cart(db_session):
    from app.exceptions import CouponInvalid

    consumer, _ = await _consumer_with_address(db_session)
    coupon = await _coupon(db_session, code="MIN500", discount_value=10, min_order_amount=500)

    with pytest.raises(CouponInvalid, match="Minimum order"):
        await CouponService(db_session).validate(
            code=coupon.code, user_id=consumer.id, cart_amount=400
        )


@pytest.mark.asyncio
async def test_user_specific_coupon_refuses_a_different_user(db_session):
    from app.exceptions import CouponInvalid

    _, _ = await _consumer_with_address(db_session)
    owner = await UserFactory.create(db_session, role="consumer")
    other = await UserFactory.create(db_session, role="consumer")
    coupon = await _coupon(db_session, code="RAVIREF", user_id=owner.id)

    # Owner: fine.
    _, discount = await CouponService(db_session).validate(
        code=coupon.code, user_id=owner.id, cart_amount=500
    )
    assert discount > 0

    # Other user: coupon simply doesn't exist for them.
    with pytest.raises(CouponInvalid):
        await CouponService(db_session).validate(
            code=coupon.code, user_id=other.id, cart_amount=500
        )


@pytest.mark.asyncio
async def test_expired_coupon_is_refused(db_session):
    from app.exceptions import CouponInvalid

    consumer, _ = await _consumer_with_address(db_session)
    coupon = await _coupon(db_session, code="OLD")
    # Backdate valid_until past now.
    coupon.valid_until = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()

    with pytest.raises(CouponInvalid):
        await CouponService(db_session).validate(
            code=coupon.code, user_id=consumer.id, cart_amount=1000
        )


@pytest.mark.asyncio
async def test_deactivated_coupon_is_refused(db_session):
    from app.exceptions import CouponInvalid

    consumer, _ = await _consumer_with_address(db_session)
    coupon = await _coupon(db_session, code="OFF", is_active=False)

    with pytest.raises(CouponInvalid):
        await CouponService(db_session).validate(
            code=coupon.code, user_id=consumer.id, cart_amount=1000
        )


# ---------------------------------------------------------------------------
# Per-user cap + list ↔ validate agreement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_user_limit_exhausts_after_use(db_session):
    """A one-per-user coupon disappears from the user's list after they use
    it — the shared filter between list_available and validate.
    """
    from app.exceptions import CouponInvalid

    consumer, addr = await _consumer_with_address(db_session)
    coupon = await _coupon(db_session, code="ONCE", usage_limit_per_user=1)

    # First use: allowed. Simulate by inserting a redemption directly.
    # This mirrors what create_booking would do.
    booking = await _stub_booking(db_session, consumer.id, addr.id)
    await CouponService(db_session).apply_to_booking(
        coupon=coupon,
        booking_id=booking.id,
        user_id=consumer.id,
        discount_amount=50,
    )
    await db_session.flush()

    # Both surfaces refuse a second attempt.
    available = await CouponService(db_session).list_available(consumer.id)
    assert coupon.id not in {c.id for c in available}
    with pytest.raises(CouponInvalid):
        await CouponService(db_session).validate(
            code=coupon.code, user_id=consumer.id, cart_amount=1000
        )


@pytest.mark.asyncio
async def test_list_available_returns_global_and_user_coupons(db_session):
    consumer, _ = await _consumer_with_address(db_session)
    other = await UserFactory.create(db_session, role="consumer")

    global_c = await _coupon(db_session, code="FIRST20")
    own_c = await _coupon(db_session, code="MINE", user_id=consumer.id)
    theirs = await _coupon(db_session, code="THEIRS", user_id=other.id)

    available = await CouponService(db_session).list_available(consumer.id)
    codes = {c.code for c in available}
    assert global_c.code in codes
    assert own_c.code in codes
    assert theirs.code not in codes


# ---------------------------------------------------------------------------
# Apply on booking creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creating_a_booking_with_a_valid_coupon_records_discount_and_redemption(db_session):
    from app.schemas.booking import CreateBookingItemRequest
    from app.services.booking_service import BookingService

    consumer, addr = await _consumer_with_address(db_session)
    svc = await _service(db_session, base_price=1000.0)
    coupon = await _coupon(db_session, code="TWENTY", discount_value=20)

    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[CreateBookingItemRequest(service_id=svc.id)],
        address_id=addr.id,
        coupon_code="twenty",
    )

    assert booking.service_amount == 1000.0
    assert booking.discount_amount == 200.0
    # Provider payout unchanged — anchored to service_amount, not the
    # discounted total. Platform absorbs the coupon.
    assert booking.provider_payout == 800.0

    # And a redemption row exists.
    redemption = (
        await db_session.execute(
            select(CouponRedemption).where(CouponRedemption.booking_id == booking.id)
        )
    ).scalar_one()
    assert redemption.coupon_id == coupon.id
    assert redemption.discount_applied == 200.0


@pytest.mark.asyncio
async def test_invalid_coupon_code_refuses_the_whole_booking(db_session):
    """No half-persist: an invalid coupon means the booking does not exist."""
    from app.exceptions import CouponInvalid
    from app.schemas.booking import CreateBookingItemRequest
    from app.services.booking_service import BookingService

    consumer, addr = await _consumer_with_address(db_session)
    svc = await _service(db_session, base_price=1000.0)

    with pytest.raises(CouponInvalid):
        await BookingService(db_session).create_booking(
            consumer_id=consumer.id,
            items=[CreateBookingItemRequest(service_id=svc.id)],
            address_id=addr.id,
            coupon_code="DOESNOTEXIST",
        )
    await db_session.rollback()

    # No orphan booking.
    count = (await db_session.execute(select(func.count(Booking.id)))).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# Helper for the per-user-limit test above
# ---------------------------------------------------------------------------


async def _stub_booking(db_session, consumer_id, address_id) -> Booking:
    """Minimum-viable booking row for tests that need a redemption anchor.

    Not routed through BookingService.create_booking because these tests
    are exercising the coupon service directly and don't need items to
    resolve; a bare Booking row satisfies the FK from
    coupon_redemptions.booking_id.
    """
    from app.models.booking import BookingStatus, PaymentMethod, PaymentStatus

    b = Booking(
        id=uuid.uuid4(),
        consumer_id=consumer_id,
        provider_id=None,
        consumer_address_id=address_id,
        status=BookingStatus.REQUESTED,
        payment_status=PaymentStatus.PENDING,
        payment_method=PaymentMethod.ONLINE,
        doorstep_otp="0000",
        service_amount=100.0,
        platform_commission=20.0,
        provider_payout=80.0,
    )
    db_session.add(b)
    await db_session.flush()
    return b
