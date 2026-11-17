"""
SVC-411: a booking with a chosen SP is billed at that SP's price, locked per item.

- With ``sp_id`` every item names its variant and the unit price the customer
  was quoted, else 422.
- Each item is checked against the SP's ``sp_service_pricing`` row for
  (sp_id, variant). No row or a switched-off one is a 409 ``SP_UNAVAILABLE``
  (re-pick), and that wins over a price difference elsewhere in the cart. A
  different price is a 409 ``PRICE_CHANGED`` listing every changed item. Either
  way nothing is written.
- On success ``unit_price`` is the SP's price, ``locked_price`` the sum of the
  item totals, and the commission is the variant's own share of its price
  applied to the item total (``commission.split_at_price``).
- The rows are read ``FOR SHARE``, so a price edit can't land between the check
  and the insert.
- Without ``sp_id`` nothing changes: the variant's fixed amounts, and any
  ``quoted_price`` is ignored.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.exceptions import AppException
from app.models.address import Address
from app.models.booking import Booking
from app.models.booking_item import BookingItem
from app.models.category import Category
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.schemas.booking import CreateBookingItemRequest
from app.services.booking_service import BookingService
from app.services.commission import split_at_price
from app.services.standard_variant import standard_variant
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import ADDRESS_POINT, _eligible_provider

CREATE = "/api/v1/bookings/"

# AC gas charging, the SVC-410 example: ₹2,499, ₹750 of it the platform's.
BASE = Decimal("2499.00")
COMMISSION = Decimal("750.00")


async def _service(db, *, base_price=BASE, commission=COMMISSION):
    """A service with one variant at ``base_price``, ``commission`` of it the
    platform's. Returns ``(service, variant)``."""
    slug = f"lock-{uuid.uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db.add(category)
    await db.flush()
    service = Service(
        id=uuid.uuid4(),
        category_id=category.id,
        name="AC gas charging",
        base_price=base_price,
        duration_minutes=60,
        is_active=True,
    )
    db.add(service)
    variant = standard_variant(service)
    variant.platform_commission = commission
    variant.provider_payout = base_price - commission
    db.add(variant)
    await db.flush()
    return service, variant


async def _price(db, sp, service, variant, price, *, active=True):
    row = SpServicePricing(
        sp_id=sp.id,
        service_id=service.id,
        service_variant_id=variant.id,
        price_amount=Decimal(price),
        price_unit="PER_VISIT",
        is_active=active,
    )
    db.add(row)
    await db.flush()
    return row


async def _consumer(db):
    consumer = await UserFactory.create(db, role="consumer")
    address = Address(
        user_id=consumer.id, label="home", full_address="12 MG Road", location=ADDRESS_POINT
    )
    db.add(address)
    await db.flush()
    return consumer, address


def _item(service, variant, quoted, quantity=1):
    return {
        "service_id": str(service.id),
        "service_variant_id": str(variant.id),
        "quoted_price": quoted,
        "quantity": quantity,
    }


async def _post(client, auth_headers, consumer, address, items, sp=None):
    body = {"items": items, "address_id": str(address.id), "payment_method": "cash"}
    if sp is not None:
        body["sp_id"] = str(sp.id)
    return await client.post(CREATE, json=body, headers=auth_headers(str(consumer.id), "consumer"))


async def _booking_count(db, consumer) -> int:
    await db.flush()
    return await db.scalar(
        select(func.count()).select_from(Booking).where(Booking.consumer_id == consumer.id)
    )


# --- Happy path ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("price", "quantity", "total", "commission", "payout"),
    [
        # Below the platform price: 4000 x 750/2499 = 1200.48 (SVC-410's 600.24 x 2).
        ("2000.00", 2, "4000.00", "1200.48", "2799.52"),
        # At the platform price the variant's fixed amounts come back.
        ("2499.00", 1, "2499.00", "750.00", "1749.00"),
        # Above it, the same share of more.
        ("2999.50", 1, "2999.50", "900.21", "2099.29"),
    ],
)
async def test_a_quoted_price_the_sp_still_charges_is_locked_per_item(
    client, db_session, auth_headers, price, quantity, total, commission, payout
):
    service, variant = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    await _price(db_session, sp, service, variant, price)
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client,
        auth_headers,
        consumer,
        address,
        [_item(service, variant, float(price), quantity)],
        sp=sp,
    )

    assert resp.status_code == 201, resp.text
    [line] = resp.json()["items"]
    assert line["unit_price"] == float(price)
    assert line["total_price"] == float(total)

    booking = await db_session.get(Booking, uuid.UUID(resp.json()["id"]))
    item = await db_session.scalar(select(BookingItem).where(BookingItem.booking_id == booking.id))
    assert item.service_variant_id == variant.id
    assert (item.unit_price, item.total_price) == (Decimal(price), Decimal(total))
    assert (item.platform_commission, item.provider_payout) == (
        Decimal(commission),
        Decimal(payout),
    )
    assert booking.locked_price == booking.service_amount == Decimal(total)
    assert (booking.platform_commission, booking.provider_payout) == (
        Decimal(commission),
        Decimal(payout),
    )


@pytest.mark.asyncio
async def test_a_quoted_price_may_be_sent_as_a_string(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    await _price(db_session, sp, service, variant, "1999.90")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client, auth_headers, consumer, address, [_item(service, variant, "1999.9")], sp=sp
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["items"][0]["unit_price"] == 1999.9


@pytest.mark.asyncio
async def test_a_multi_item_cart_sums_the_locked_items(client, db_session, auth_headers):
    service_a, variant_a = await _service(db_session)
    service_b, variant_b = await _service(
        db_session, base_price=Decimal("1000.00"), commission=Decimal("300.00")
    )
    sp = await _eligible_provider(db_session, service_a)
    db_session.add(ProviderServiceModel(provider_id=sp.id, service_id=service_b.id))
    await _price(db_session, sp, service_a, variant_a, "2000.00")
    await _price(db_session, sp, service_b, variant_b, "800.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client,
        auth_headers,
        consumer,
        address,
        [_item(service_a, variant_a, 2000), _item(service_b, variant_b, 800, quantity=3)],
        sp=sp,
    )

    assert resp.status_code == 201, resp.text
    booking = await db_session.get(Booking, uuid.UUID(resp.json()["id"]))
    # 2000 + 3 x 800; commission 600.24 + 2400 x 300/1000 = 720.00.
    assert booking.locked_price == Decimal("4400.00")
    assert booking.platform_commission == Decimal("1320.24")
    assert booking.provider_payout == Decimal("3079.76")


# --- Refusals -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_changed_price_is_409_with_the_current_price_and_writes_nothing(
    client, db_session, auth_headers
):
    service, variant = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    await _price(db_session, sp, service, variant, "2100.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client, auth_headers, consumer, address, [_item(service, variant, 2000)], sp=sp
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "PRICE_CHANGED"
    assert body["details"] == {
        "items": [
            {
                "service_id": str(service.id),
                "service_variant_id": str(variant.id),
                "quoted_price": 2000.0,
                "current_price": 2100.0,
            }
        ]
    }
    assert await _booking_count(db_session, consumer) == 0


@pytest.mark.asyncio
async def test_a_paisa_off_is_a_changed_price(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    await _price(db_session, sp, service, variant, "2000.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client, auth_headers, consumer, address, [_item(service, variant, 1999.99)], sp=sp
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "PRICE_CHANGED"


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["switched_off", "no_row", "another_sps_row"])
async def test_an_sp_without_an_active_price_is_sp_unavailable(
    client, db_session, auth_headers, why
):
    service, variant = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    if why == "switched_off":
        await _price(db_session, sp, service, variant, "2000.00", active=False)
    elif why == "another_sps_row":
        other = await _eligible_provider(db_session, service)
        await _price(db_session, other, service, variant, "2000.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client, auth_headers, consumer, address, [_item(service, variant, 2000)], sp=sp
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "SP_UNAVAILABLE"
    assert await _booking_count(db_session, consumer) == 0


async def _two_item_cart(db):
    """Two services the SP covers and prices; returns what the test needs."""
    service_a, variant_a = await _service(db)
    service_b, variant_b = await _service(db)
    sp = await _eligible_provider(db, service_a)
    db.add(ProviderServiceModel(provider_id=sp.id, service_id=service_b.id))
    await db.flush()
    return sp, (service_a, variant_a), (service_b, variant_b)


@pytest.mark.asyncio
async def test_a_cart_with_one_changed_item_lists_only_that_item(client, db_session, auth_headers):
    sp, (service_a, variant_a), (service_b, variant_b) = await _two_item_cart(db_session)
    await _price(db_session, sp, service_a, variant_a, "2000.00")
    await _price(db_session, sp, service_b, variant_b, "1500.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client,
        auth_headers,
        consumer,
        address,
        [_item(service_a, variant_a, 2000), _item(service_b, variant_b, 1400)],
        sp=sp,
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "PRICE_CHANGED"
    assert resp.json()["details"]["items"] == [
        {
            "service_id": str(service_b.id),
            "service_variant_id": str(variant_b.id),
            "quoted_price": 1400.0,
            "current_price": 1500.0,
        }
    ]
    assert await _booking_count(db_session, consumer) == 0


@pytest.mark.asyncio
async def test_a_switched_off_item_wins_over_a_changed_one(client, db_session, auth_headers):
    sp, (service_a, variant_a), (service_b, variant_b) = await _two_item_cart(db_session)
    await _price(db_session, sp, service_a, variant_a, "2100.00")  # changed
    await _price(db_session, sp, service_b, variant_b, "1500.00", active=False)
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client,
        auth_headers,
        consumer,
        address,
        [_item(service_a, variant_a, 2000), _item(service_b, variant_b, 1500)],
        sp=sp,
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "SP_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        pytest.param(lambda s, v: {"service_id": str(s.id)}, id="no_variant_no_price"),
        pytest.param(
            lambda s, v: {"service_id": str(s.id), "service_variant_id": str(v.id)},
            id="no_quoted_price",
        ),
        pytest.param(lambda s, v: {"service_id": str(s.id), "quoted_price": 2000}, id="no_variant"),
        pytest.param(lambda s, v: _item(s, v, 0), id="zero_price"),
        pytest.param(lambda s, v: _item(s, v, "2000.005"), id="three_decimals"),
    ],
)
async def test_with_sp_id_an_item_needs_a_variant_and_a_quoted_price(
    client, db_session, auth_headers, item
):
    service, variant = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    await _price(db_session, sp, service, variant, "2000.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(client, auth_headers, consumer, address, [item(service, variant)], sp=sp)

    assert resp.status_code == 422, resp.text
    assert await _booking_count(db_session, consumer) == 0


@pytest.mark.asyncio
async def test_the_legacy_single_service_shape_with_sp_id_is_422(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    await _price(db_session, sp, service, variant, "2000.00")
    consumer, address = await _consumer(db_session)

    resp = await client.post(
        CREATE,
        json={"service_id": str(service.id), "address_id": str(address.id), "sp_id": str(sp.id)},
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert resp.status_code == 422, resp.text


# --- Without sp_id ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_sp_id_the_variant_amounts_stand_and_a_quoted_price_is_ignored(
    client, db_session, auth_headers
):
    service, variant = await _service(db_session)
    # An SP with a different price changes nothing for an open booking.
    sp = await _eligible_provider(db_session, service)
    await _price(db_session, sp, service, variant, "2000.00")
    consumer, address = await _consumer(db_session)

    resp = await _post(
        client, auth_headers, consumer, address, [_item(service, variant, 2000, quantity=2)]
    )

    assert resp.status_code == 201, resp.text
    booking = await db_session.get(Booking, uuid.UUID(resp.json()["id"]))
    assert booking.chosen_sp_id is None
    assert booking.locked_price == booking.service_amount == Decimal("4998.00")
    assert booking.platform_commission == Decimal("1500.00")
    assert booking.provider_payout == Decimal("3498.00")
    item = await db_session.scalar(select(BookingItem).where(BookingItem.booking_id == booking.id))
    assert item.unit_price == BASE


# --- The rule -----------------------------------------------------------------------


def test_split_at_price_is_the_variant_share_rounded_to_paise():
    variant = ServiceVariant(base_price=BASE, platform_commission=COMMISSION)
    # SVC-410's example: 2000 x 750/2499 = 600.2400960...
    assert split_at_price(variant, Decimal("2000")) == (Decimal("600.24"), Decimal("1399.76"))
    assert split_at_price(variant, BASE) == (COMMISSION, BASE - COMMISSION)
    # 1666 x 750/2499 = 499.99999...: rounded to the paisa, not truncated.
    assert split_at_price(variant, Decimal("1666.00")) == (Decimal("500.00"), Decimal("1166.00"))


# --- Concurrency (committed rows on two connections) -------------------------------


class _World:
    """Committed rows for one test, deleted again in ``teardown``."""

    def __init__(self):
        self.engine = create_async_engine(TEST_DATABASE_URL)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def seed(self):
        async with self.factory() as db:
            self.service, self.variant = await _service(db)
            self.sp = await _eligible_provider(db, self.service)
            self.row = await _price(db, self.sp, self.service, self.variant, "2000.00")
            self.consumer, self.address = await _consumer(db)
            self.category_id = self.service.category_id
            await db.commit()

    def items(self, quoted):
        return [
            CreateBookingItemRequest(
                service_id=self.service.id,
                service_variant_id=self.variant.id,
                quoted_price=Decimal(quoted),
            )
        ]

    async def create(self, db, quoted):
        return await BookingService(db).create_booking(
            consumer_id=self.consumer.id,
            items=self.items(quoted),
            address_id=self.address.id,
            payment_method="cash",
            sp_id=self.sp.id,
        )

    async def teardown(self):
        if not hasattr(self, "consumer"):  # seed never committed
            await self.engine.dispose()
            return
        users = [str(self.sp.id), str(self.consumer.id)]
        async with self.factory() as db:
            bookings = "SELECT id FROM bookings WHERE consumer_id::text = :c"
            params = {"c": str(self.consumer.id)}
            for table in ("booking_items", "booking_status_logs"):
                await db.execute(
                    text(f"DELETE FROM {table} WHERE booking_id IN ({bookings})"), params
                )
            await db.execute(text("DELETE FROM bookings WHERE consumer_id::text = :c"), params)
            for sql in (
                "DELETE FROM sp_service_pricing WHERE sp_id::text = ANY(:u)",
                "DELETE FROM provider_services WHERE provider_id::text = ANY(:u)",
                "DELETE FROM provider_profiles WHERE user_id::text = ANY(:u)",
                "DELETE FROM addresses WHERE user_id::text = ANY(:u)",
                "DELETE FROM users WHERE id::text = ANY(:u)",
            ):
                await db.execute(text(sql), {"u": users})
            await db.execute(
                text("DELETE FROM service_variants WHERE service_id = :s"), {"s": self.service.id}
            )
            await db.execute(text("DELETE FROM services WHERE id = :s"), {"s": self.service.id})
            await db.execute(text("DELETE FROM categories WHERE id = :c"), {"c": self.category_id})
            await db.commit()
        await self.engine.dispose()


def _run(scenario):
    async def main():
        world = _World()
        try:
            await world.seed()
            return await scenario(world)
        finally:
            await world.teardown()

    return asyncio.run(main())


def test_a_booking_waits_for_an_in_flight_price_edit_and_sees_the_new_price():
    async def scenario(world):
        async with world.factory() as editor, world.factory() as booker:
            await editor.execute(
                update(SpServicePricing)
                .where(SpServicePricing.id == world.row.id)
                .values(price_amount=Decimal("2200.00"))
            )
            booking = asyncio.create_task(world.create(booker, "2000.00"))
            await asyncio.sleep(1.0)
            # FOR SHARE waits on the editor's row lock.
            assert not booking.done()
            await editor.commit()
            with pytest.raises(AppException) as refused:
                await asyncio.wait_for(booking, timeout=30)
            await booker.rollback()
        return refused.value

    refused = _run(scenario)
    assert refused.code == "PRICE_CHANGED"
    assert refused.details["items"][0]["current_price"] == 2200.0


def test_a_price_edit_waits_for_an_in_flight_booking():
    async def scenario(world):
        async with world.factory() as booker, world.factory() as editor:
            booking = await world.create(booker, "2000.00")
            await editor.execute(text("SET LOCAL lock_timeout = '500ms'"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                await editor.execute(
                    update(SpServicePricing)
                    .where(SpServicePricing.id == world.row.id)
                    .values(price_amount=Decimal("2200.00"))
                )
            await editor.rollback()
            await booker.commit()
            return booking.locked_price

    assert _run(scenario) == Decimal("2000.00")
