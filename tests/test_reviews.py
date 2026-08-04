"""Reviews router coverage — POST /reviews + GET /reviews/provider/{id}.

The review flow is trust-critical: pins ratings that show up on the
consumer app's provider profile, feeds the provider's avg rating, and
is the ONLY user-generated content in v1. The rules are also non-obvious
(only-own-booking, only-completed, one-per-booking), so tests here also
serve as executable documentation.
"""

from uuid import uuid4

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _seed_completed_booking(db_session):
    """Consumer + provider + a COMPLETED booking between them. Returned as
    (consumer, provider, booking) — most tests only need one or two."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    # Unique suffix — categories.name is UNIQUE and _seed is called by
    # tests that seed multiple bookings in the same session.
    suffix = uuid4().hex[:8]
    cat = Category(
        name=f"Cleaning {suffix}", slug=f"cleaning-{suffix}", is_active=True, sort_order=1
    )
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name=f"Basic Clean {suffix}",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(svc)
    addr = Address(user_id=consumer.id, label="home", full_address="1 Test Rd")
    db_session.add(addr)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.COMPLETED,
    )
    return consumer, provider, booking


# ---------------------------------------------------------------------------
# POST /reviews
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_can_review_own_completed_booking(client, auth_headers, db_session):
    consumer, provider, booking = await _seed_completed_booking(db_session)

    response = await client.post(
        "/api/v1/reviews/",
        json={"booking_id": str(booking.id), "rating": 5, "comment": "great"},
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rating"] == 5
    assert body["comment"] == "great"
    assert body["reviewer_id"] == str(consumer.id)
    assert body["reviewee_id"] == str(provider.id)


@pytest.mark.asyncio
async def test_consumer_cannot_review_someone_elses_booking(client, auth_headers, db_session):
    """Alice reviewing Bob's booking would let her flood a provider with
    fake reviews she wasn't a party to. Must 403."""
    consumer, _, booking = await _seed_completed_booking(db_session)
    stranger = await UserFactory.create(db_session, role="consumer")

    response = await client.post(
        "/api/v1/reviews/",
        json={"booking_id": str(booking.id), "rating": 1, "comment": "spam"},
        headers=auth_headers(str(stranger.id), "consumer"),
    )

    assert response.status_code == 403, response.text
    assert "own bookings" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_cannot_review_before_completion(client, auth_headers, db_session):
    """A consumer trying to review a booking still in REQUESTED / EN_ROUTE
    would ship reviews before the service actually happened. Blocked with 403."""
    consumer, _, booking = await _seed_completed_booking(db_session)
    booking.status = BookingStatus.REQUESTED
    await db_session.flush()

    response = await client.post(
        "/api/v1/reviews/",
        json={"booking_id": str(booking.id), "rating": 5, "comment": ""},
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 403, response.text
    assert "completed" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_cannot_review_same_booking_twice(client, auth_headers, db_session):
    """One review per booking. Second submission → 409, not a silent
    overwrite (which would erase the prior review + skew the provider's
    average)."""
    consumer, _, booking = await _seed_completed_booking(db_session)

    r1 = await client.post(
        "/api/v1/reviews/",
        json={"booking_id": str(booking.id), "rating": 5, "comment": "first"},
        headers=auth_headers(str(consumer.id), "consumer"),
    )
    r2 = await client.post(
        "/api/v1/reviews/",
        json={"booking_id": str(booking.id), "rating": 1, "comment": "second"},
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 409, r2.text


@pytest.mark.asyncio
@pytest.mark.parametrize("rating", [0, 6, -1, 100])
async def test_invalid_rating_rejected(client, auth_headers, db_session, rating):
    """rating is constrained to 1..5. Out-of-range → 422 from Pydantic."""
    consumer, _, booking = await _seed_completed_booking(db_session)

    response = await client.post(
        "/api/v1/reviews/",
        json={"booking_id": str(booking.id), "rating": rating, "comment": ""},
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# GET /reviews/provider/{provider_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_provider_reviews_returns_them_with_avg(client, auth_headers, db_session):
    """Two reviews on the same provider → both listed, avg_rating computed."""
    consumer_a, provider, booking_a = await _seed_completed_booking(db_session)
    # Reuse the same provider by seeding another consumer + completed booking.
    consumer_b = await UserFactory.create(db_session, role="consumer")
    addr = Address(user_id=consumer_b.id, label="home", full_address="2 Other Rd")
    db_session.add(addr)
    await db_session.flush()
    booking_b = await BookingFactory.create(
        db_session,
        consumer_id=consumer_b.id,
        provider_id=provider.id,
        service_id=booking_a.service_id,
        address_id=addr.id,
        status=BookingStatus.COMPLETED,
    )

    for consumer, booking, rating in [
        (consumer_a, booking_a, 5),
        (consumer_b, booking_b, 3),
    ]:
        r = await client.post(
            "/api/v1/reviews/",
            json={"booking_id": str(booking.id), "rating": rating, "comment": ""},
            headers=auth_headers(str(consumer.id), "consumer"),
        )
        assert r.status_code == 200, r.text

    response = await client.get(
        f"/api/v1/reviews/provider/{provider.id}",
        headers=auth_headers(str(consumer_a.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["avg_rating"] == 4.0
    assert len(body["reviews"]) == 2


@pytest.mark.asyncio
async def test_list_provider_reviews_empty_for_new_provider(client, auth_headers, db_session):
    """A provider with no reviews yet returns total=0, empty list, avg=0."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")

    response = await client.get(
        f"/api/v1/reviews/provider/{provider.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert body["reviews"] == []
    assert body["avg_rating"] == 0
