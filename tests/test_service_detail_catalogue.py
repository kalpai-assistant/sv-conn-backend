"""Coverage for the SVC-400 catalogue fields on ``GET /services/{id}``.

Content fields (hero_image_url, included_items, faqs, before_after_photos) are
nullable columns shown only on the detail page; they read back as stored and
are present-and-null when unset. ``steps`` accepts both the SVC-340 string list
and the SVC-392 object list.

The computed fields follow Vipul's rules (2026-10-29):

- rating_average / rating_count: the consumer's own reviews on bookings whose
  only service is this one. Fewer than ``MIN_REVIEWS_FOR_RATING`` → None + 0.
- bookings_this_week: distinct bookings with this service, created within
  ``BOOKINGS_THIS_WEEK_WINDOW``, excluding cancelled and expired.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.review import Review
from app.services.catalog_service import MIN_REVIEWS_FOR_RATING
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import ADDRESS_POINT, _service

DETAIL = "/api/v1/services/{}"
NEW_FIELDS = (
    "hero_image_url",
    "included_items",
    "faqs",
    "before_after_photos",
    "rating_average",
    "rating_count",
    "bookings_this_week",
)


async def _booking(db, service_ids, *, status=BookingStatus.COMPLETED, provider=None):
    consumer = await UserFactory.create(db, role="consumer")
    provider = provider or await UserFactory.create(db, role="provider")
    address = Address(
        user_id=consumer.id, label="home", full_address="12 MG Road", location=ADDRESS_POINT
    )
    db.add(address)
    await db.flush()
    return await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        item_service_ids=service_ids,
        address_id=address.id,
        status=status,
    )


async def _review(db, booking, rating, *, reviewer_id=None):
    db.add(
        Review(
            booking_id=booking.id,
            reviewer_id=reviewer_id or booking.consumer_id,
            reviewee_id=booking.provider_id,
            rating=rating,
        )
    )
    await db.flush()


async def _reviewed_bookings(db, service, ratings):
    for rating in ratings:
        await _review(db, await _booking(db, [service.id]), rating)


async def _detail(client, service) -> dict:
    response = await client.get(DETAIL.format(service.id))
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Content fields + steps shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bare_service_returns_every_new_field_present_and_empty(client, db_session):
    service = await _service(db_session)

    body = await _detail(client, service)

    for field in NEW_FIELDS:
        assert field in body, field
    assert body["hero_image_url"] is None
    assert body["included_items"] is None
    assert body["faqs"] is None
    assert body["before_after_photos"] is None
    assert body["rating_average"] is None
    assert body["rating_count"] == 0
    assert body["bookings_this_week"] == 0


@pytest.mark.asyncio
async def test_stored_content_reads_back_as_written(client, db_session):
    service = await _service(db_session)
    service.hero_image_url = "https://cdn.example.com/hero.webp"
    service.included_items = ["Kitchen", "Bathrooms"]
    service.faqs = [{"question": "Bring supplies?", "answer": "Yes."}]
    service.before_after_photos = [{"before_url": "https://b", "after_url": "https://a"}]
    await db_session.flush()

    body = await _detail(client, service)

    assert body["hero_image_url"] == "https://cdn.example.com/hero.webp"
    assert body["included_items"] == ["Kitchen", "Bathrooms"]
    assert body["faqs"] == [{"question": "Bring supplies?", "answer": "Yes."}]
    assert body["before_after_photos"] == [{"before_url": "https://b", "after_url": "https://a"}]


@pytest.mark.asyncio
async def test_steps_as_strings_still_read_back_as_strings(client, db_session):
    service = await _service(db_session)
    service.steps = ["Arrives", "Cleans"]
    await db_session.flush()

    assert (await _detail(client, service))["steps"] == ["Arrives", "Cleans"]


@pytest.mark.asyncio
async def test_steps_as_objects_read_back_with_optional_fields_null(client, db_session):
    service = await _service(db_session)
    service.steps = [
        {"title": "Inspect", "duration_minutes": 10, "icon": "search"},
        {"title": "Clean"},
    ]
    await db_session.flush()

    assert (await _detail(client, service))["steps"] == [
        {"title": "Inspect", "duration_minutes": 10, "icon": "search"},
        {"title": "Clean", "duration_minutes": None, "icon": None},
    ]


@pytest.mark.asyncio
async def test_list_services_leaves_out_the_new_fields(client, db_session):
    service = await _service(db_session)

    response = await client.get(f"/api/v1/services?category_id={service.category_id}")

    assert response.status_code == 200, response.text
    [row] = [s for s in response.json() if s["id"] == str(service.id)]
    for field in NEW_FIELDS:
        assert field not in row, field


# ---------------------------------------------------------------------------
# Rating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_the_threshold_the_rating_is_null_and_zero(client, db_session):
    service = await _service(db_session)
    await _reviewed_bookings(db_session, service, [5] * (MIN_REVIEWS_FOR_RATING - 1))

    body = await _detail(client, service)

    assert body["rating_average"] is None
    assert body["rating_count"] == 0, "not the real small count"


@pytest.mark.asyncio
async def test_at_the_threshold_the_real_average_and_count_show(client, db_session):
    assert MIN_REVIEWS_FOR_RATING == 10
    service = await _service(db_session)
    await _reviewed_bookings(db_session, service, [5] * 7 + [4] * 2 + [3])  # 46 / 10

    body = await _detail(client, service)

    assert body["rating_average"] == 4.6
    assert body["rating_count"] == 10


@pytest.mark.asyncio
async def test_the_average_is_rounded_to_one_place(client, db_session):
    service = await _service(db_session)
    await _reviewed_bookings(db_session, service, [5] * 10 + [4])  # 54 / 11 = 4.909…

    assert (await _detail(client, service))["rating_average"] == 4.9


@pytest.mark.asyncio
async def test_a_multi_service_bookings_review_does_not_count(client, db_session):
    service = await _service(db_session)
    other = await _service(db_session)
    await _reviewed_bookings(db_session, service, [5] * (MIN_REVIEWS_FOR_RATING - 1))
    # The 10th review is on a cart with another service, so it can't tip the
    # count over the threshold — for either service.
    await _review(db_session, await _booking(db_session, [service.id, other.id]), 1)

    body = await _detail(client, service)
    assert body["rating_average"] is None
    assert body["rating_count"] == 0

    # And once a real 10th lands, the 1-star cart review still isn't in it.
    await _reviewed_bookings(db_session, service, [5])
    body = await _detail(client, service)
    assert body["rating_average"] == 5.0
    assert body["rating_count"] == 10


@pytest.mark.asyncio
async def test_a_review_not_written_by_the_consumer_does_not_count(client, db_session):
    service = await _service(db_session)
    await _reviewed_bookings(db_session, service, [5] * (MIN_REVIEWS_FOR_RATING - 1))
    booking = await _booking(db_session, [service.id])
    await _review(db_session, booking, 1, reviewer_id=booking.provider_id)

    body = await _detail(client, service)

    assert body["rating_average"] is None
    assert body["rating_count"] == 0


@pytest.mark.asyncio
async def test_another_services_reviews_do_not_count(client, db_session):
    service = await _service(db_session)
    other = await _service(db_session)
    await _reviewed_bookings(db_session, other, [5] * MIN_REVIEWS_FOR_RATING)

    body = await _detail(client, service)

    assert body["rating_average"] is None
    assert body["rating_count"] == 0


# ---------------------------------------------------------------------------
# bookings_this_week
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bookings_this_week_counts_the_window_and_skips_cancelled_and_expired(
    client, db_session
):
    service = await _service(db_session)
    other = await _service(db_session)
    await _booking(db_session, [service.id], status=BookingStatus.REQUESTED)
    await _booking(db_session, [service.id], status=BookingStatus.COMPLETED)
    await _booking(db_session, [service.id, other.id], status=BookingStatus.ACCEPTED)
    await _booking(db_session, [service.id], status=BookingStatus.CANCELLED)
    await _booking(db_session, [service.id], status=BookingStatus.EXPIRED)
    await _booking(db_session, [other.id], status=BookingStatus.COMPLETED)
    old = await _booking(db_session, [service.id], status=BookingStatus.COMPLETED)
    old.created_at = datetime.now(UTC) - timedelta(days=8)
    recent = await _booking(db_session, [service.id], status=BookingStatus.COMPLETED)
    recent.created_at = datetime.now(UTC) - timedelta(days=6)
    await db_session.flush()

    # requested + completed + the multi-service cart + 6 days ago.
    assert (await _detail(client, service))["bookings_this_week"] == 4
