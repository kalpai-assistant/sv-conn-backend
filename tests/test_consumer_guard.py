"""
The consumer may not move their own job en route / start / complete.

A pre-existing hole closed alongside SVC-320 PR 3: transition_status admitted
the booking's consumer for those steps, so a consumer could mark their own
booking complete, crediting the provider's payout and firing the cash-payment
task. Only the provider (SP) or the assigned Pro may now; cancel is unchanged.
"""

from unittest.mock import patch

import pytest

from app.models.booking import BookingStatus
from app.tasks import notification_tasks
from tests.test_pro_surface import _as, _assigned_job, _team


@pytest.fixture
def pushes():
    sent: list[str] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append(kw["event"]),
    ):
        yield sent


@pytest.fixture
def cash_task():
    with patch("app.routers.bookings.process_cash_payment") as task:
        yield task


@pytest.mark.asyncio
async def test_a_consumer_cannot_move_their_own_job(
    client, db_session, auth_headers, pushes, cash_task
):
    service, sp, _, member, _ = await _team(db_session)
    consumer, booking = await _assigned_job(db_session, service, sp, member, cash=True)
    headers = _as(auth_headers, consumer)
    pushes.clear()

    resp = await client.patch(f"/api/v1/bookings/{booking.id}/en-route", headers=headers)
    assert resp.status_code == 403
    booking.status = BookingStatus.EN_ROUTE
    await db_session.flush()
    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/start", json={"otp": booking.doorstep_otp}, headers=headers
    )
    assert resp.status_code == 403, "even with the right OTP"
    booking.status = BookingStatus.IN_PROGRESS
    await db_session.flush()
    resp = await client.patch(f"/api/v1/bookings/{booking.id}/complete", headers=headers)
    assert resp.status_code == 403

    await db_session.refresh(booking)
    assert booking.status is BookingStatus.IN_PROGRESS
    assert pushes == []
    cash_task.delay.assert_not_called()


@pytest.mark.asyncio
async def test_the_consumer_can_still_cancel(client, db_session, auth_headers, pushes):
    service, sp, _, member, _ = await _team(db_session)
    consumer, booking = await _assigned_job(db_session, service, sp, member)
    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/cancel",
        json={"reason": "changed my mind"},
        headers=_as(auth_headers, consumer),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
