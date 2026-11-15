"""SVC-408: the marketplace pricing schema (epic SVC-406).

``sp_service_pricing`` constraints, the SP boost defaults, and
``bookings.locked_price`` on booking creation. The migration round trip is in
``tests/test_migrations.py``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.models.address import Address
from app.models.category import Category
from app.models.coupon import Coupon, DiscountType
from app.models.price_unit import PriceUnit
from app.models.provider_profile import ProviderProfile
from app.models.service import Service
from app.models.sp_service_pricing import SpServicePricing
from app.schemas.booking import CreateBookingItemRequest
from app.services.booking_service import BookingService
from tests.factories.user_factory import UserFactory


async def _service(db_session, base_price: str = "499.00") -> Service:
    suffix = uuid.uuid4().hex[:8]
    category = Category(name=f"SVC-408 {suffix}", slug=f"svc-408-{suffix}", is_active=True)
    db_session.add(category)
    await db_session.flush()
    service = Service(
        category_id=category.id,
        name=f"SVC-408 svc {suffix}",
        base_price=Decimal(base_price),
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)
    await db_session.flush()
    return service


async def _sp(db_session) -> ProviderProfile:
    user = await UserFactory.create(db_session, role="provider")
    profile = ProviderProfile(user_id=user.id)
    db_session.add(profile)
    await db_session.flush()
    return profile


async def _price(db_session, sp: ProviderProfile, service: Service, **extra) -> SpServicePricing:
    row = SpServicePricing(
        sp_id=sp.user_id,
        service_id=service.id,
        price_amount=extra.pop("price_amount", Decimal("450.00")),
        price_unit=extra.pop("price_unit", PriceUnit.PER_VISIT.value),
        **extra,
    )
    db_session.add(row)
    await db_session.flush()
    return row


# --- sp_service_pricing --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_price_row_gets_its_defaults(db_session):
    sp, service = await _sp(db_session), await _service(db_session)

    row = await _price(db_session, sp, service)
    await db_session.refresh(row)

    assert row.is_active is True
    assert row.price_amount == Decimal("450.00")
    assert row.created_at is not None and row.updated_at is not None


@pytest.mark.asyncio
async def test_one_price_per_sp_and_service(db_session):
    sp, service = await _sp(db_session), await _service(db_session)
    await _price(db_session, sp, service)

    with pytest.raises(IntegrityError, match="uq_sp_service_pricing_sp_service"):
        async with db_session.begin_nested():
            await _price(db_session, sp, service, price_amount=Decimal("300.00"))

    # The same service priced by a second SP, and a second service by the
    # first SP, are both fine.
    await _price(db_session, await _sp(db_session), service)
    await _price(db_session, sp, await _service(db_session))


@pytest.mark.asyncio
async def test_price_unit_must_be_a_known_unit(db_session):
    sp, service = await _sp(db_session), await _service(db_session)

    with pytest.raises(IntegrityError, match="ck_sp_service_pricing_price_unit"):
        async with db_session.begin_nested():
            await _price(db_session, sp, service, price_unit="PER_DAY")

    for unit in PriceUnit:
        await _price(db_session, sp, await _service(db_session), price_unit=unit.value)


@pytest.mark.asyncio
async def test_a_priced_service_cannot_be_hard_deleted(db_session):
    sp, service = await _sp(db_session), await _service(db_session)
    await _price(db_session, sp, service)

    with pytest.raises(IntegrityError, match="fk_sp_service_pricing_service_id"):
        async with db_session.begin_nested():
            await db_session.execute(delete(Service).where(Service.id == service.id))


@pytest.mark.asyncio
async def test_a_priced_sp_cannot_be_hard_deleted(db_session):
    sp, service = await _sp(db_session), await _service(db_session)
    await _price(db_session, sp, service)

    with pytest.raises(IntegrityError, match="fk_sp_service_pricing_sp_id"):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(ProviderProfile).where(ProviderProfile.user_id == sp.user_id)
            )


# --- provider_profiles boost columns -------------------------------------------


@pytest.mark.asyncio
async def test_a_new_sp_has_no_boost_state(db_session):
    sp = await _sp(db_session)
    await db_session.refresh(sp)

    assert sp.boost_eligible_at is None
    assert sp.boost_completed_jobs_count == 0


# --- bookings.locked_price -----------------------------------------------------


async def _consumer_with_address(db_session):
    consumer = await UserFactory.create(db_session, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="1 Test Rd")
    db_session.add(address)
    await db_session.flush()
    return consumer, address


@pytest.mark.asyncio
async def test_create_booking_locks_the_cart_total(db_session):
    consumer, address = await _consumer_with_address(db_session)
    first, second = await _service(db_session, "499.00"), await _service(db_session, "250.50")

    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[
            CreateBookingItemRequest(service_id=first.id, quantity=1),
            CreateBookingItemRequest(service_id=second.id, quantity=2),
        ],
        address_id=address.id,
    )
    await db_session.refresh(booking)

    assert booking.service_amount == Decimal("1000.00")
    assert booking.locked_price == booking.service_amount


@pytest.mark.asyncio
async def test_locked_price_is_the_pre_discount_total(db_session):
    consumer, address = await _consumer_with_address(db_session)
    service = await _service(db_session, "1000.00")
    now = datetime.now(UTC)
    code = f"svc408{uuid.uuid4().hex[:8]}"
    db_session.add(
        Coupon(
            code=code,
            discount_type=DiscountType.PERCENT,
            discount_value=20,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=1),
            usage_limit_per_user=1,
            is_active=True,
        )
    )
    await db_session.flush()

    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[CreateBookingItemRequest(service_id=service.id)],
        address_id=address.id,
        coupon_code=code,
    )
    await db_session.refresh(booking)

    assert booking.discount_amount == Decimal("200.00")
    assert booking.locked_price == Decimal("1000.00") == booking.service_amount
