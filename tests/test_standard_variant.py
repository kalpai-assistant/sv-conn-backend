"""SVC-480: every service has at least one variant.

- Both admin create paths add a "Standard" variant: the service's price and
  duration, GST 0, sort 0, active, split like ``create_booking``'s legacy branch.
- So a booking on a new service costs and pays what the legacy (no-variant)
  branch would have charged: the same total, commission and payout.
- A price change on a service with exactly one variant row moves that variant
  too, keeping its commission ratio (legacy split from a 0 price), recorded in
  the same audit row. 2+ variants: untouched.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.address import Address
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.schemas.booking import CreateBookingItemRequest
from app.services.booking_service import BookingService
from app.services.standard_variant import STANDARD_VARIANT_LABEL
from app.utils.money import to_decimal
from tests.factories.user_factory import UserFactory
from tests.test_admin_catalog import A, _audit, _cat, _service_body, _svc
from tests.test_admin_console_auth import _admin, _session


def _rate() -> Decimal:
    return to_decimal(get_settings().PLATFORM_COMMISSION_PERCENT) / 100


async def _variants(db, service_id) -> list[ServiceVariant]:
    return list(
        (
            await db.scalars(
                select(ServiceVariant)
                .where(ServiceVariant.service_id == service_id)
                .order_by(ServiceVariant.sort_order, ServiceVariant.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _admin_created(client, db, admin, cat, price) -> uuid.UUID:
    r = await client.post(
        f"{A}/services",
        headers=_session(admin),
        json=_service_body(cat, base_price=str(price), duration_minutes=75),
    )
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def _variant(db, service, price, commission, *, is_active=True, sort_order=0):
    price, commission = Decimal(price), Decimal(commission)
    row = ServiceVariant(
        service_id=service.id,
        label=f"V {uuid.uuid4().hex[:6]}",
        base_price=price,
        platform_commission=commission,
        provider_payout=price - commission,
        sort_order=sort_order,
        is_active=is_active,
    )
    db.add(row)
    await db.flush()
    return row


def _money(variant) -> tuple:
    return (variant.base_price, variant.platform_commission, variant.provider_payout)


# --- create ---------------------------------------------------------------------


async def test_admin_create_adds_the_standard_variant(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    r = await client.post(
        f"{A}/services",
        headers=_session(admin),
        json=_service_body(cat, base_price="499.99", duration_minutes=75),
    )
    assert r.status_code == 201, r.text
    body = r.json()

    [variant] = await _variants(db_session, uuid.UUID(body["id"]))
    commission = to_decimal(Decimal("499.99") * _rate())
    assert (variant.label, variant.subcategory) == (STANDARD_VARIANT_LABEL, None)
    assert _money(variant) == (Decimal("499.99"), commission, Decimal("499.99") - commission)
    assert (variant.duration_minutes, variant.gst_percent) == (75, Decimal("0"))
    assert (variant.sort_order, variant.is_active) == (0, True)
    # The detail the create returns shows it.
    assert [v["id"] for v in body["variants"]] == [str(variant.id)]
    assert body["active_variant_count"] == 1


async def test_the_legacy_create_route_adds_it_too(client, db_session, auth_headers):
    admin = await UserFactory.create(db_session, role="admin")
    cat = await _cat(db_session)
    r = await client.post(
        "/api/v1/services",
        json={
            "category_id": str(cat.id),
            "name": "Deep Clean",
            "base_price": 500.0,
            "duration_minutes": 90,
        },
        headers=auth_headers(str(admin.id), "admin"),
    )
    assert r.status_code == 201, r.text
    [variant] = await _variants(db_session, uuid.UUID(r.json()["id"]))
    commission = to_decimal(Decimal("500") * _rate())
    assert variant.label == STANDARD_VARIANT_LABEL
    assert _money(variant) == (Decimal("500.00"), commission, Decimal("500.00") - commission)
    assert [v["id"] for v in r.json()["variants"]] == [str(variant.id)]


# --- a booking costs and pays what it did before ---------------------------------


async def _book(db, service_id, quantity):
    consumer = await UserFactory.create(db, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="1 Test Rd")
    db.add(address)
    await db.flush()
    booking = await BookingService(db).create_booking(
        consumer_id=consumer.id,
        items=[CreateBookingItemRequest(service_id=service_id, quantity=quantity)],
        address_id=address.id,
    )
    await db.refresh(booking)
    return booking


@pytest.mark.parametrize(
    ("price", "quantity"),
    [("499.00", 1), ("499.00", 3), ("499.99", 1), ("333.33", 1)],
)
async def test_a_booking_on_a_new_service_charges_what_the_legacy_branch_did(
    client, db_session, price, quantity
):
    """Before SVC-480 an admin-created service had no variant and was billed by
    the legacy branch; now it has the Standard variant. Same money either way.
    (For an odd-paise price at quantity > 1 the split can move by up to
    ``quantity`` paise, because the variant rounds per unit: not tested here.)"""
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    legacy = await _svc(db_session, cat, base_price=Decimal(price))  # no variant
    new = await _admin_created(client, db_session, admin, cat, price)

    before = await _book(db_session, legacy.id, quantity)
    after = await _book(db_session, new, quantity)

    def money(b):
        return (b.service_amount, b.platform_commission, b.provider_payout, b.locked_price)

    assert money(after) == money(before)
    assert after.service_amount == Decimal(price) * quantity


# --- a price change moves a single variant ---------------------------------------


async def test_a_price_change_moves_the_only_variant_keeping_its_ratio(client, db_session):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session), base_price=Decimal("1000.00"))
    variant = await _variant(db_session, service, "1000.00", "300.00")

    r = await client.patch(
        f"{A}/services/{service.id}", headers=_session(admin), json={"base_price": "333.33"}
    )
    assert r.status_code == 200, r.text

    [after] = await _variants(db_session, service.id)
    # 333.33 × 0.3 = 99.999 → 100.00
    assert _money(after) == (Decimal("333.33"), Decimal("100.00"), Decimal("233.33"))
    [row] = await _audit(db_session, admin)
    assert row.before_json == {
        "base_price": 1000.0,
        "variant": {
            "id": str(variant.id),
            "base_price": 1000.0,
            "platform_commission": 300.0,
            "provider_payout": 700.0,
        },
    }
    assert row.after_json == {
        "base_price": 333.33,
        "variant": {
            "id": str(variant.id),
            "base_price": 333.33,
            "platform_commission": 100.0,
            "provider_payout": 233.33,
        },
    }


async def test_an_inactive_only_variant_follows_the_price_too(client, db_session):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session), base_price=Decimal("1000.00"))
    await _variant(db_session, service, "1000.00", "250.00", is_active=False)

    r = await client.patch(
        f"{A}/services/{service.id}", headers=_session(admin), json={"base_price": "999"}
    )
    assert r.status_code == 200, r.text
    [after] = await _variants(db_session, service.id)
    assert _money(after) == (Decimal("999.00"), Decimal("249.75"), Decimal("749.25"))


async def test_a_zero_priced_variant_gets_the_legacy_split(client, db_session):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session), base_price=Decimal("0.00"))
    await _variant(db_session, service, "0.00", "0.00")

    r = await client.patch(
        f"{A}/services/{service.id}", headers=_session(admin), json={"base_price": "499.99"}
    )
    assert r.status_code == 200, r.text
    [after] = await _variants(db_session, service.id)
    commission = to_decimal(Decimal("499.99") * _rate())
    assert _money(after) == (Decimal("499.99"), commission, Decimal("499.99") - commission)


async def test_a_service_with_two_variants_keeps_them(client, db_session):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session), base_price=Decimal("2499.00"))
    one = await _variant(db_session, service, "2499.00", "749.70", sort_order=0)
    two = await _variant(db_session, service, "2999.00", "899.70", sort_order=1)
    before = [_money(one), _money(two)]

    r = await client.patch(
        f"{A}/services/{service.id}", headers=_session(admin), json={"base_price": "1999"}
    )
    assert r.status_code == 200, r.text
    assert [_money(v) for v in await _variants(db_session, service.id)] == before
    [row] = await _audit(db_session, admin)
    assert (row.before_json, row.after_json) == ({"base_price": 2499.0}, {"base_price": 1999.0})


async def test_no_price_change_leaves_the_variant(client, db_session):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session), base_price=Decimal("1000.00"))
    variant = await _variant(db_session, service, "800.00", "240.00")  # out of step

    r = await client.patch(
        f"{A}/services/{service.id}", headers=_session(admin), json={"duration_minutes": 90}
    )
    assert r.status_code == 200, r.text
    [after] = await _variants(db_session, service.id)
    assert _money(after) == _money(variant)
    [row] = await _audit(db_session, admin)
    assert "variant" not in row.before_json


async def test_the_legacy_patch_route_moves_it_too(client, db_session, auth_headers):
    admin = await UserFactory.create(db_session, role="admin")
    service = await _svc(db_session, await _cat(db_session), base_price=Decimal("1000.00"))
    variant = await _variant(db_session, service, "1000.00", "300.00")

    r = await client.patch(
        f"/api/v1/services/{service.id}",
        json={"base_price": 1500.0},
        headers=auth_headers(str(admin.id), "admin"),
    )
    assert r.status_code == 200, r.text
    [after] = await _variants(db_session, service.id)
    assert _money(after) == (Decimal("1500.00"), Decimal("450.00"), Decimal("1050.00"))
    assert r.json()["variants"][0]["base_price"] == 1500.0
    [row] = await _audit(db_session, admin)
    assert row.before_json["variant"] == {
        "id": str(variant.id),
        "base_price": 1000.0,
        "platform_commission": 300.0,
        "provider_payout": 700.0,
    }
    assert row.after_json["variant"]["base_price"] == 1500.0


async def test_the_service_row_itself_is_unchanged_by_the_sync(client, db_session):
    """Only the variant moves alongside: the service gets exactly the new price."""
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session), base_price=Decimal("1000.00"))
    await _variant(db_session, service, "1000.00", "300.00")
    r = await client.patch(
        f"{A}/services/{service.id}", headers=_session(admin), json={"base_price": "1200"}
    )
    assert r.status_code == 200, r.text
    fresh = await db_session.scalar(
        select(Service).where(Service.id == service.id).execution_options(populate_existing=True)
    )
    assert fresh.base_price == Decimal("1200.00")
