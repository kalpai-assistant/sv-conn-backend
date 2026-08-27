"""The bookings list is ordered, newest first.

``list_bookings`` had no ORDER BY. It went straight from the WHERE clause to
``.offset().limit()``, which left two problems.

The visible one: rows came back in whatever order Postgres happened to hold
them — roughly insertion order, until a row is UPDATEd and its new version
lands elsewhere. So a booking the user had just cancelled sank to the bottom
of the "Past" tab instead of rising to the top, and read as missing. Found on
device: the Activity screen sorted client-side and showed it, the Bookings
screen did not and appeared to have lost it.

The quiet one: LIMIT/OFFSET over an unordered query has no defined result at
all. Postgres may return the same row on two pages, or on neither. Paging past
the first 20 could silently drop bookings, and nothing would look wrong.

The second is why this is more than cosmetic, and why the tie-breaker matters:
``created_at`` is ``server_default=func.now()``, which in Postgres is
transaction start time, so bookings created in one transaction share a
timestamp to the microsecond.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _consumer_with_bookings(db_session, count: int):
    """One consumer and ``count`` bookings, oldest first, one minute apart.

    ``created_at`` is stamped explicitly rather than left to the column
    default, and that is not tidiness. ``server_default=func.now()`` is
    transaction start time in Postgres, so every booking a test creates in one
    transaction shares a timestamp to the microsecond — the ordering key would
    be constant and the ``id`` tie-breaker (a random uuid4) would decide.
    Production creates one booking per request, so real rows differ; a test
    that wants to assert on recency has to arrange for that difference itself.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    cat = Category(name="Cleaning", slug="cleaning", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name="Basic Clean",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    address = Address(
        user_id=consumer.id,
        label="home",
        full_address="42 Residency Road, Bengaluru 560025",
    )
    db_session.add_all([svc, address])
    await db_session.flush()

    base = datetime.now(UTC) - timedelta(hours=1)
    created = []
    for i in range(count):
        booking = await BookingFactory.create(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            service_id=svc.id,
            address_id=address.id,
            status=BookingStatus.REQUESTED,
        )
        booking.created_at = base + timedelta(minutes=i)
        created.append(booking)
    await db_session.flush()
    return consumer, created


@pytest.mark.asyncio
async def test_list_is_newest_first(client, auth_headers, db_session):
    """The most recent booking is the first row, not the last."""
    consumer, created = await _consumer_with_bookings(db_session, 5)

    response = await client.get(
        "/api/v1/bookings/",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    returned = [b["id"] for b in response.json()["bookings"]]
    assert returned == [str(b.id) for b in reversed(created)]


@pytest.mark.asyncio
async def test_a_cancelled_booking_stays_at_the_top(client, auth_headers, db_session):
    """Cancelling must not move a booking down the list.

    This is the bug as the user met it. An UPDATE writes a new row version,
    and without an ORDER BY that version's physical position drives where it
    appears — so the booking someone had just acted on sank out of view.
    Ordering on ``created_at`` makes the row's position independent of how many
    times it has been written.
    """
    consumer, created = await _consumer_with_bookings(db_session, 3)
    newest = created[-1]
    newest.status = BookingStatus.CANCELLED
    await db_session.flush()

    response = await client.get(
        "/api/v1/bookings/",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["bookings"][0]["id"] == str(newest.id)


@pytest.mark.asyncio
async def test_paging_neither_drops_nor_repeats(client, auth_headers, db_session):
    """Every booking appears exactly once across the pages.

    The assertion that would have caught the original defect: it fails on an
    unordered query without ever asserting *what* the order should be.
    """
    consumer, created = await _consumer_with_bookings(db_session, 5)
    headers = auth_headers(str(consumer.id), "consumer")

    seen: list[str] = []
    for page in (1, 2, 3):
        response = await client.get(
            f"/api/v1/bookings/?page={page}&per_page=2",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        seen.extend(b["id"] for b in response.json()["bookings"])

    assert sorted(seen) == sorted(str(b.id) for b in created)
    assert len(seen) == len(set(seen)), "a booking was returned on more than one page"
