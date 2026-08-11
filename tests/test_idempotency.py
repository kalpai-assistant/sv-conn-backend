"""SVC-158 — Idempotency-Key on POST /bookings.

Cover:
- Same key + same body twice → one booking created, second returns cached response.
- Same key + different body → 422 (client bug: key reused for a different intent).
- No key → pre-idempotency behaviour (each request creates a new booking).
- Key scoped per user (two users can independently use the same string).
- rollback() clears the pending sentinel on handler failure so a retry works.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.address import Address
from app.models.category import Category
from app.models.service import Service
from app.utils.idempotency import IdempotencyStore, hash_body
from tests.factories.user_factory import UserFactory


async def _seed(db_session):
    consumer = await UserFactory.create(db_session, role="consumer")
    await UserFactory.create(db_session, role="provider")
    category = Category(name="Cleaning", slug="cleaning", is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()
    service = Service(
        category_id=category.id,
        name="Basic Clean",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)
    address = Address(user_id=consumer.id, label="home", full_address="1 Test Rd")
    db_session.add(address)
    await db_session.flush()
    return consumer, service, address


@pytest.mark.asyncio
async def test_same_key_same_body_returns_cached_response(client, auth_headers, db_session):
    """The core case. Double-tap on Book: same key, same body — one booking,
    identical response on the second call."""
    consumer, service, address = await _seed(db_session)
    key = str(uuid4())
    body = {
        "service_id": str(service.id),
        "address_id": str(address.id),
        "payment_method": "online",
    }
    headers = {**auth_headers(str(consumer.id), "consumer"), "Idempotency-Key": key}

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = MagicMock(return_value=None)
        r1 = await client.post("/api/v1/bookings/", json=body, headers=headers)
        r2 = await client.post("/api/v1/bookings/", json=body, headers=headers)

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["id"] == r2.json()["id"]
    # The cached path must not fire another new_request push — otherwise the
    # consumer / provider get double-notified on a client retry, defeating the
    # whole point.
    assert mock_task.delay.call_count == 1


@pytest.mark.asyncio
async def test_same_key_different_body_returns_422(client, auth_headers, db_session):
    """Reusing a key across two distinct requests is a client bug — refuse it
    explicitly so the developer notices, don't silently return the first result."""
    consumer, service, address = await _seed(db_session)
    address2 = Address(user_id=consumer.id, label="office", full_address="2 Other Rd")
    db_session.add(address2)
    await db_session.flush()

    key = str(uuid4())
    headers = {**auth_headers(str(consumer.id), "consumer"), "Idempotency-Key": key}

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = lambda **kwargs: None
        r1 = await client.post(
            "/api/v1/bookings/",
            json={
                "service_id": str(service.id),
                "address_id": str(address.id),
                "payment_method": "online",
            },
            headers=headers,
        )
        r2 = await client.post(
            "/api/v1/bookings/",
            json={
                "service_id": str(service.id),
                "address_id": str(address2.id),
                "payment_method": "online",
            },
            headers=headers,
        )

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 422, r2.text
    assert "reused" in r2.json()["message"].lower()


@pytest.mark.asyncio
async def test_no_key_creates_fresh_booking_each_time(client, auth_headers, db_session):
    """Without an Idempotency-Key we keep the pre-SVC-158 behaviour: each
    POST creates a new booking. Ensures we didn't break clients that haven't
    been updated to send the header."""
    consumer, service, address = await _seed(db_session)
    body = {
        "service_id": str(service.id),
        "address_id": str(address.id),
        "payment_method": "online",
    }
    headers = auth_headers(str(consumer.id), "consumer")

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = lambda **kwargs: None
        r1 = await client.post("/api/v1/bookings/", json=body, headers=headers)
        r2 = await client.post("/api/v1/bookings/", json=body, headers=headers)

    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


@pytest.mark.asyncio
async def test_key_scoped_per_user(client, auth_headers, db_session):
    """User A's key and User B's key must not collide even if the string matches."""
    consumer_a, service, address_a = await _seed(db_session)
    consumer_b = await UserFactory.create(db_session, role="consumer")
    address_b = Address(user_id=consumer_b.id, label="home", full_address="9 Other Rd")
    db_session.add(address_b)
    await db_session.flush()

    key = str(uuid4())

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = lambda **kwargs: None
        r_a = await client.post(
            "/api/v1/bookings/",
            json={
                "service_id": str(service.id),
                "address_id": str(address_a.id),
                "payment_method": "online",
            },
            headers={**auth_headers(str(consumer_a.id), "consumer"), "Idempotency-Key": key},
        )
        r_b = await client.post(
            "/api/v1/bookings/",
            json={
                "service_id": str(service.id),
                "address_id": str(address_b.id),
                "payment_method": "online",
            },
            headers={**auth_headers(str(consumer_b.id), "consumer"), "Idempotency-Key": key},
        )

    assert r_a.status_code == 201, r_a.text
    # If keys weren't user-scoped, B would either hit the 422 (body-hash
    # mismatch) or get A's cached response. Instead B gets its own new booking.
    assert r_b.status_code == 201, r_b.text
    assert r_a.json()["consumer_id"] != r_b.json()["consumer_id"]


@pytest.mark.asyncio
async def test_rollback_releases_lock_on_handler_failure(redis_client):
    """If the handler crashes after reserve(), rollback() must drop the
    sentinel so the client's retry isn't blocked by the (still-valid)
    pending marker."""
    store = IdempotencyStore(redis_client, user_id="user-1", key="key-1")
    body_hash = hash_body({"foo": "bar"})

    assert await store.reserve(body_hash) is True
    # A retry right now would see the pending sentinel and 409.
    from app.exceptions import IdempotencyInProgress

    with pytest.raises(IdempotencyInProgress):
        await store.check(body_hash)

    # Simulate handler failure.
    await store.rollback()

    # Retry works — no cached entry, no pending sentinel.
    assert await store.check(body_hash) is None
