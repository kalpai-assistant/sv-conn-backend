"""doorstep_otp reaches the consumer and nobody else (SVC-169).

The PIN gates en_route → in_progress, so the provider is supposed to obtain it
from the customer at the door. ``BookingResponse`` declared it with a
``# Only visible to consumer`` comment that nothing enforced, and both booking
GETs serialised it straight off the ORM object — so the assigned provider's own
API responses carried the PIN and the doorstep check became ceremonial.

Two things are asserted throughout, and the second matters as much as the first:
the serialised body must not carry the PIN, **and** the row must still hold it
afterwards. The obvious fix (``booking.doorstep_otp = None`` on the object
``get_booking`` returned) would satisfy the first and destroy the second —
``get_db`` commits at the end of every successful request, and the column is
nullable since PR #59, so a read endpoint would quietly wipe the row and return
200, leaving ``/start`` with nothing to match. Hence the paired assertions and
``test_provider_read_then_start_still_works``, which is the end-to-end form of
the same trap.

The wipe-after-use behaviour these tests sit next to lives in
``test_doorstep_otp_wipe.py``.
"""

from unittest.mock import patch

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _row_otp(db_session, booking) -> str | None:
    """The PIN as the database would hand it back after the request.

    The flush is load-bearing, and these tests are worthless without it. The
    ``get_db`` override in ``conftest`` is a plain lambda rather than a
    generator, so the real dependency's ``await session.commit()`` never runs
    under test — an ORM mutation made by a handler stays pending in the session
    and a bare ``refresh()`` re-reads the untouched row and reports success.
    Flushing first plays the part production's commit plays: it pushes whatever
    the handler did to the object out to the database, so a read path that
    nulled ``booking.doorstep_otp`` is caught here instead of sailing through.
    """
    await db_session.flush()
    await db_session.refresh(booking)
    return booking.doorstep_otp


async def _booking_with_parties(db_session, status: BookingStatus, doorstep_otp: str = "4242"):
    """One booking, its consumer and its assigned provider."""
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
        status=status,
        doorstep_otp=doorstep_otp,
    )
    return consumer, provider, booking


# ------------------------------------------------------------------
# GET /bookings/{id}
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_get_booking_omits_otp(client, auth_headers, db_session):
    """The assigned provider reads their own booking and gets no PIN."""
    _, provider, booking = await _booking_with_parties(db_session, BookingStatus.EN_ROUTE)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["doorstep_otp"] is None

    # ...and the read did not wipe the row on its way out.
    assert await _row_otp(db_session, booking) == "4242"


@pytest.mark.asyncio
async def test_provider_get_booking_omits_otp_when_accepted(client, auth_headers, db_session):
    """ACCEPTED is the other half of the live-PIN window, before en route."""
    _, provider, booking = await _booking_with_parties(db_session, BookingStatus.ACCEPTED)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["doorstep_otp"] is None

    assert await _row_otp(db_session, booking) == "4242"


@pytest.mark.asyncio
async def test_consumer_get_booking_still_returns_otp(client, auth_headers, db_session):
    """The consumer's tracking screen is the one legitimate reader — it shows
    the customer the PIN to read out at the door."""
    consumer, _, booking = await _booking_with_parties(db_session, BookingStatus.EN_ROUTE)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["doorstep_otp"] == "4242"


# ------------------------------------------------------------------
# GET /bookings/
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_list_bookings_omits_otp_on_every_row(client, auth_headers, db_session):
    """The list route returned attached objects for the whole page, so it
    leaked a PIN per row, not just one."""
    consumer, provider, first = await _booking_with_parties(db_session, BookingStatus.ACCEPTED)
    second = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=first.service_id,
        address_id=first.consumer_address_id,
        status=BookingStatus.EN_ROUTE,
        doorstep_otp="8888",
    )

    response = await client.get(
        "/api/v1/bookings/",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    rows = response.json()["bookings"]
    assert len(rows) == 2
    assert all(row["doorstep_otp"] is None for row in rows)

    for booking, expected in ((first, "4242"), (second, "8888")):
        assert await _row_otp(db_session, booking) == expected


@pytest.mark.asyncio
async def test_consumer_list_bookings_still_returns_otp(client, auth_headers, db_session):
    consumer, _, booking = await _booking_with_parties(db_session, BookingStatus.EN_ROUTE)

    response = await client.get(
        "/api/v1/bookings/",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    rows = response.json()["bookings"]
    assert [row["doorstep_otp"] for row in rows] == ["4242"]


# ------------------------------------------------------------------
# The transition the PIN protects still works
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_read_then_start_still_works(client, auth_headers, db_session):
    """The whole point: hiding the PIN from the provider must not consume it.

    A provider reads the booking (single + list), sees no PIN, then starts the
    job with the PIN the customer read out. If the read path had nulled the
    field on the ORM object instead of on the response, ``get_db``'s commit
    would have erased the row's PIN and this ``/start`` would 400.
    """
    _, provider, booking = await _booking_with_parties(db_session, BookingStatus.EN_ROUTE)
    headers = auth_headers(str(provider.id), "provider")

    assert (await client.get(f"/api/v1/bookings/{booking.id}", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/bookings/", headers=headers)).status_code == 200

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = lambda **kwargs: None
        response = await client.patch(
            f"/api/v1/bookings/{booking.id}/start",
            json={"otp": "4242"},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_progress"
    # Spent, so wiped by transition_status — the one place that assignment is
    # correct. Not the redaction under test here.
    assert body["doorstep_otp"] is None


# ------------------------------------------------------------------
# Provider-driven write paths in the live-PIN window
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_en_route_response_omits_otp(client, auth_headers, db_session):
    """``PATCH /en-route`` answers with the booking too, in the state where the
    PIN is still live — the same leak as the GETs, one route further on."""
    _, provider, booking = await _booking_with_parties(db_session, BookingStatus.ACCEPTED)

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/en-route",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "en_route"
    assert body["doorstep_otp"] is None

    assert await _row_otp(db_session, booking) == "4242"


@pytest.mark.asyncio
async def test_consumer_cancel_response_still_carries_no_stale_otp(
    client, auth_headers, db_session
):
    """Consumer-facing write path: cancel wipes the OTP for real, so the
    consumer sees ``None`` here because it is gone, not because it is hidden."""
    consumer, _, booking = await _booking_with_parties(db_session, BookingStatus.REQUESTED)

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/cancel",
        json={"reason": "changed my mind"},
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["doorstep_otp"] is None

    assert await _row_otp(db_session, booking) is None
