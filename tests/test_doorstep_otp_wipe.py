"""bookings.doorstep_otp is cleared once the OTP-gated transition has run.

Motivation: the OTP has served its purpose after en_route → in_progress,
but the plaintext used to stay in the row for the life of the booking. A
DB dump would then expose every consumer's completed doorstep OTP. New
behaviour: wipe on IN_PROGRESS / COMPLETED / CANCELLED.

Assertions read `response.json()` rather than `db_session.refresh(booking)`
— matches the pattern used by every other transition test in this suite
(e.g. test_bookings.py::test_complete_cash_booking_triggers_payment_task).
The refresh path doesn't see the endpoint's mutations reliably because
the API request uses the same async session but its ORM writes aren't
guaranteed to be visible via a subsequent .refresh() from the test.
"""

from unittest.mock import patch

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _make_booking(db_session, status: BookingStatus, doorstep_otp: str = "1234"):
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


@pytest.mark.asyncio
async def test_otp_wiped_on_transition_to_in_progress(client, auth_headers, db_session):
    """Successful start → OTP disappears from the row so a later DB read
    can't recover it."""
    _, provider, booking = await _make_booking(db_session, BookingStatus.EN_ROUTE, "4242")

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = lambda **kwargs: None
        response = await client.patch(
            f"/api/v1/bookings/{booking.id}/start",
            json={"otp": "4242"},
            headers=auth_headers(str(provider.id), "provider"),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["doorstep_otp"] is None


@pytest.mark.asyncio
async def test_otp_wiped_on_transition_to_completed(client, auth_headers, db_session):
    """Belt-and-braces: if a booking somehow reaches COMPLETED with the
    OTP still set (skipped IN_PROGRESS wipe for any reason), the completion
    transition clears it too."""
    _, provider, booking = await _make_booking(db_session, BookingStatus.IN_PROGRESS, "9999")

    with (
        patch("app.routers.bookings.send_booking_notification") as mock_task,
        patch("app.routers.bookings.process_cash_payment") as mock_pay,
    ):
        mock_task.delay = lambda **kwargs: None
        mock_pay.delay = lambda **kwargs: None
        response = await client.patch(
            f"/api/v1/bookings/{booking.id}/complete",
            headers=auth_headers(str(provider.id), "provider"),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["doorstep_otp"] is None


@pytest.mark.asyncio
async def test_otp_wiped_on_cancellation(client, auth_headers, db_session):
    """Cancelled bookings shouldn't leak the OTP either — after cancel it
    means nothing operationally but is still a live secret in the row."""
    consumer, _, booking = await _make_booking(db_session, BookingStatus.REQUESTED, "5555")

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/cancel",
        json={"reason": "changed my mind"},
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["doorstep_otp"] is None


@pytest.mark.asyncio
async def test_otp_still_present_on_en_route_transition(client, auth_headers, db_session):
    """Sanity check: the OTP must survive up to and including the en_route
    state, or `/start` has nothing left to match. Only transitions PAST that
    point wipe it.

    Asserted on the row rather than the response body: this request is the
    provider's, and since SVC-169 a provider never receives the PIN in a
    response. That redaction is what the body assertion here used to catch —
    it read `body["doorstep_otp"] == "7777"`, which was the leak, not the
    behaviour. Survival and visibility are separate questions, and only the
    row answers the first one.
    """
    consumer, provider, booking = await _make_booking(db_session, BookingStatus.ACCEPTED, "7777")

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/en-route",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "en_route"

    # flush before refresh: the conftest `get_db` override never commits, so
    # without it the refresh re-reads a row the request could not have changed
    # and proves nothing. See `_row_otp` in test_doorstep_otp_visibility.py.
    await db_session.flush()
    await db_session.refresh(booking)
    assert booking.doorstep_otp == "7777"

    # And the consumer can still read it out at the door.
    consumer_view = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )
    assert consumer_view.json()["doorstep_otp"] == "7777"
