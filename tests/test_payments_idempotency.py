"""SVC-158 follow-up — Idempotency-Key on POST /payments/orders.

Same wrapper as `POST /bookings/`, but the side effect being prevented
is worse: without idempotency, a double-tapped Pay button creates two
Razorpay orders — both can be paid, and the user is charged twice for
one booking.

`razorpay_client.order.create` is patched throughout so tests don't hit
the live Razorpay API.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.booking import BookingStatus
from app.models.provider_profile import ProviderProfile
from tests.factories.user_factory import UserFactory


async def _seed(db_session):
    from uuid import uuid4

    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service
    from tests.factories.booking_factory import BookingFactory

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    profile = ProviderProfile(
        user_id=provider.id,
        razorpay_linked_account_id="acc_test_link_123",
    )
    db_session.add(profile)
    # Unique suffix — `categories.name` has a UNIQUE constraint, and one test
    # (`test_same_key_different_body_returns_422`) calls _seed twice inside a
    # single session to get two distinct bookings.
    suffix = uuid4().hex[:8]
    category = Category(
        name=f"Cleaning {suffix}", slug=f"cleaning-{suffix}", is_active=True, sort_order=1
    )
    db_session.add(category)
    await db_session.flush()
    service = Service(
        category_id=category.id,
        name=f"Basic Clean {suffix}",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)
    address = Address(user_id=consumer.id, label="home", full_address="1 Test Rd")
    db_session.add(address)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.REQUESTED,
    )
    return consumer, booking


def _fake_razorpay_client(order_id_seed: str = "order_test_"):
    """Mock the razorpay client so `.order.create` returns a fresh order id
    on every call. Test asserts call_count to prove idempotency."""
    counter = {"n": 0}

    def create(data):
        counter["n"] += 1
        return {"id": f"{order_id_seed}{counter['n']}", "amount": data["amount"]}

    mock = MagicMock()
    mock.order.create = MagicMock(side_effect=create)
    return mock


@pytest.mark.asyncio
async def test_same_key_same_body_returns_cached_order(client, auth_headers, db_session):
    """Double-tap Pay with the same idempotency key → one Razorpay order,
    identical response on the second call. This is the whole point of the
    ticket — without this the user is charged twice."""
    consumer, booking = await _seed(db_session)
    key = str(uuid4())
    body = {"booking_id": str(booking.id)}
    headers = {**auth_headers(str(consumer.id), "consumer"), "Idempotency-Key": key}

    fake = _fake_razorpay_client()
    with patch("app.services.payment_service.razorpay_client", fake):
        r1 = await client.post("/api/v1/payments/orders", json=body, headers=headers)
        r2 = await client.post("/api/v1/payments/orders", json=body, headers=headers)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["razorpay_order_id"] == r2.json()["razorpay_order_id"]
    # The point: only ONE Razorpay order was actually created.
    assert fake.order.create.call_count == 1


@pytest.mark.asyncio
async def test_same_key_different_body_returns_422(client, auth_headers, db_session):
    """Reusing a key for a different booking is a client bug — refuse it
    explicitly instead of silently returning the first order (which is for
    the wrong booking)."""
    consumer, booking_a = await _seed(db_session)
    _, booking_b = await _seed(db_session)  # different booking, same consumer
    key = str(uuid4())
    headers = {**auth_headers(str(consumer.id), "consumer"), "Idempotency-Key": key}

    fake = _fake_razorpay_client()
    with patch("app.services.payment_service.razorpay_client", fake):
        r1 = await client.post(
            "/api/v1/payments/orders",
            json={"booking_id": str(booking_a.id)},
            headers=headers,
        )
        r2 = await client.post(
            "/api/v1/payments/orders",
            json={"booking_id": str(booking_b.id)},
            headers=headers,
        )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 422, r2.text
    assert "reused" in r2.json()["message"].lower()


@pytest.mark.asyncio
async def test_no_key_creates_fresh_order_each_time(client, auth_headers, db_session):
    """No Idempotency-Key → pre-existing behaviour. Ensures we didn't
    silently break clients that haven't been updated yet."""
    consumer, booking = await _seed(db_session)
    body = {"booking_id": str(booking.id)}
    headers = auth_headers(str(consumer.id), "consumer")

    fake = _fake_razorpay_client()
    with patch("app.services.payment_service.razorpay_client", fake):
        r1 = await client.post("/api/v1/payments/orders", json=body, headers=headers)
        r2 = await client.post("/api/v1/payments/orders", json=body, headers=headers)

    assert r1.status_code == 200 and r2.status_code == 200
    # Both calls hit Razorpay; distinct order ids come back.
    assert r1.json()["razorpay_order_id"] != r2.json()["razorpay_order_id"]
    assert fake.order.create.call_count == 2


@pytest.mark.asyncio
async def test_razorpay_failure_releases_lock(client, auth_headers, db_session):
    """If Razorpay itself errors, the pending sentinel must be cleared so
    the client's retry can proceed instead of being blocked by a stuck lock."""
    consumer, booking = await _seed(db_session)
    key = str(uuid4())
    body = {"booking_id": str(booking.id)}
    headers = {**auth_headers(str(consumer.id), "consumer"), "Idempotency-Key": key}

    fake_broken = MagicMock()
    fake_broken.order.create = MagicMock(side_effect=RuntimeError("razorpay 500"))
    with patch("app.services.payment_service.razorpay_client", fake_broken):
        r1 = await client.post("/api/v1/payments/orders", json=body, headers=headers)

    assert r1.status_code == 500

    # Immediate retry should NOT be blocked by a lingering pending sentinel.
    fake_ok = _fake_razorpay_client()
    with patch("app.services.payment_service.razorpay_client", fake_ok):
        r2 = await client.post("/api/v1/payments/orders", json=body, headers=headers)

    assert r2.status_code == 200, r2.text
    assert fake_ok.order.create.call_count == 1
