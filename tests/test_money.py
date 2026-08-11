"""SVC-150 — money is Decimal end to end; no paisa is lost converting to paise."""

from decimal import Decimal

import pytest

from app.utils.money import to_decimal, to_paise

# --- the conversion itself ---------------------------------------------------


@pytest.mark.parametrize(
    "rupees,expected_paise",
    [
        # The values the old float path got wrong — each was 1 paisa short.
        ("80.07", 8007),
        ("80.10", 8010),
        ("100.09", 10009),
        ("999.20", 99920),
        ("1.13", 113),
        ("2.01", 201),
        # Ordinary values that were always fine.
        ("0.00", 0),
        ("1.00", 100),
        ("500.00", 50000),
        ("9999999999.99", 999999999999),
    ],
)
def test_to_paise_is_exact(rupees, expected_paise):
    assert to_paise(Decimal(rupees)) == expected_paise


def test_to_paise_rounds_half_up_not_down():
    """Half-paisa amounts round up. Truncation is what underpaid providers."""
    assert to_paise(Decimal("0.005")) == 1  # not 0
    assert to_paise(Decimal("0.004")) == 0
    assert to_paise(Decimal("1.005")) == 101  # not 100


def test_to_paise_accepts_floats_without_reintroducing_the_bug():
    """A float arriving from JSON must not drag its binary error in with it.

    Decimal(100.09) is 100.09000000000000341...; Decimal(str(100.09)) is exactly
    100.09. to_decimal routes through str() for precisely this reason.
    """
    assert to_paise(100.09) == 10009
    assert to_paise(80.07) == 8007
    assert to_decimal(100.09) == Decimal("100.09")


# --- the split that produces the stored values -------------------------------


@pytest.mark.asyncio
async def test_commission_split_is_exact_and_adds_back_up(db_session):
    """The 80/20 split must not invent or lose a paisa.

    A price of ₹100.09 splits into ₹20.02 + ₹80.07. Under floats the payout came
    out a hair under 80.07 and then truncated to 8006 paise at payout time.
    """
    import uuid

    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service
    from app.schemas.booking import CreateBookingItemRequest
    from app.services.booking_service import BookingService
    from tests.factories.user_factory import UserFactory

    suffix = uuid.uuid4().hex[:8]
    category = Category(
        name=f"Split {suffix}", slug=f"split-{suffix}", is_active=True, sort_order=1
    )
    db_session.add(category)
    await db_session.flush()

    service = Service(
        category_id=category.id,
        name=f"Svc {suffix}",
        base_price=Decimal("100.09"),
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)
    consumer = await UserFactory.create(db_session, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="1 Test Rd")
    db_session.add(address)
    await db_session.flush()

    # Post-merge signature: `service_id` was replaced by `items` (multi-service cart,
    # matching-policy (i)); a single service is a 1-item cart. See #76.
    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[CreateBookingItemRequest(service_id=service.id, quantity=1)],
        address_id=address.id,
    )

    assert booking.service_amount == Decimal("100.09")
    assert booking.platform_commission == Decimal("20.02")
    assert booking.provider_payout == Decimal("80.07")
    # The halves reconstruct the whole — no paisa created or destroyed.
    assert booking.platform_commission + booking.provider_payout == booking.service_amount
    # And the payout converts to the full amount owed.
    assert to_paise(booking.provider_payout) == 8007


# --- the API contract must not change ----------------------------------------


@pytest.mark.asyncio
async def test_api_still_returns_money_as_numbers_not_strings(client, db_session, auth_headers):
    """Money stays a JSON number in responses.

    Pydantic serialises Decimal as a quoted string, so "tidying up" the response
    schemas from float to Decimal would silently turn 100.09 into "100.09" and
    break every client. The schemas stay float on purpose; this guards that.
    """
    import json
    import uuid

    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service
    from tests.factories.booking_factory import BookingFactory
    from tests.factories.user_factory import UserFactory

    suffix = uuid.uuid4().hex[:8]
    category = Category(name=f"Api {suffix}", slug=f"api-{suffix}", is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()
    service = Service(
        category_id=category.id,
        name=f"Svc {suffix}",
        base_price=Decimal("100.09"),
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)
    consumer = await UserFactory.create(db_session, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="2 Test Rd")
    db_session.add(address)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        service_id=service.id,
        address_id=address.id,
        service_amount=100.09,
    )
    await db_session.flush()

    resp = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )
    assert resp.status_code == 200

    raw = json.loads(resp.text)
    for field in ("service_amount", "platform_commission", "provider_payout"):
        assert isinstance(raw[field], int | float), (
            f"{field} serialised as {type(raw[field]).__name__} "
            f"({raw[field]!r}) — money must stay a JSON number"
        )
