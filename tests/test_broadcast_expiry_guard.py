"""
The broadcast never moves a booking that is no longer REQUESTED (SVC-373).

Seen on beta: the ``new_request`` task failed on a push and Celery retried it
5 s later. In between, an SP accepted. The retry found nobody left to offer
the job to and expired it: an accepted job gone, and no status row to say so.

Two guards, each tested on its own: the broadcast skips a booking that isn't
REQUESTED, and ``_expire_unbroadcastable`` expires only while it still is.
"""

import logging
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.booking_offer import BookingOffer
from app.models.booking_status_log import BookingStatusLog
from app.tasks.notification_tasks import _expire_unbroadcastable, send_booking_notification
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_unbroadcastable_booking import _address, committed  # noqa: F401 (fixture)


def _sync_factory():
    sync_url = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url)
    return engine, sessionmaker(bind=engine)


def _run_broadcast(booking_id):
    """The real task on the test DB; returns the stubbed push."""
    engine, factory = _sync_factory()
    try:
        with (
            patch("app.database.SyncSessionLocal", factory),
            patch("app.tasks.notification_tasks.send_push_notification") as push,
        ):
            send_booking_notification(booking_id=str(booking_id), event="new_request")
        return push
    finally:
        engine.dispose()


async def _booking(db, committed, *, status, provider=False, paid=False):  # noqa: F811
    consumer = await UserFactory.create(db, role="consumer")
    addr = await _address(db, consumer.id)
    sp = await UserFactory.create(db, role="provider") if provider else None
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=sp.id if sp else None,
        address_id=addr.id,
        status=status,
        payment_status=PaymentStatus.PAID if paid else PaymentStatus.PENDING,
    )
    committed.extend([booking, addr, consumer] + ([sp] if sp else []))
    await db.commit()
    return booking


async def _logs(db, booking):
    return (
        await db.scalars(select(BookingStatusLog).where(BookingStatusLog.booking_id == booking.id))
    ).all()


async def _offers(db, booking):
    return (
        await db.scalars(select(BookingOffer).where(BookingOffer.booking_id == booking.id))
    ).all()


# --- The broadcast skips a booking someone already took ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.CANCELLED]
)
async def test_broadcast_after_the_booking_moved_on_changes_nothing(
    db_session,
    committed,  # noqa: F811
    caplog,
    status,
):
    """The beta case: the retry runs after the accept. Nobody is eligible any
    more, which used to expire the accepted job."""
    booking = await _booking(db_session, committed, status=status, provider=True)

    with caplog.at_level(logging.INFO, logger="app.tasks.notification_tasks"):
        push = _run_broadcast(booking.id)

    await db_session.refresh(booking)
    assert booking.status is status
    assert await _offers(db_session, booking) == []
    assert await _logs(db_session, booking) == []
    push.assert_not_called()
    assert f"broadcast.skipped_not_requested booking_id={booking.id}" in caplog.text


# --- The expiry itself is guarded ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_unbroadcastable_booking_expires_with_a_status_row(
    db_session,
    committed,  # noqa: F811
):
    """Also proves the guarded UPDATE's WHERE matches a real REQUESTED row: a
    silent no-match would leave it REQUESTED."""
    booking = await _booking(db_session, committed, status=BookingStatus.REQUESTED)

    _run_broadcast(booking.id)

    await db_session.refresh(booking)
    assert booking.status is BookingStatus.EXPIRED
    [row] = await _logs(db_session, booking)
    assert (row.from_status, row.to_status, row.changed_by) == ("requested", "expired", None)
    assert row.metadata_ == {"reason": "no-eligible-provider"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "paid"),
    [
        (BookingStatus.ACCEPTED, False),
        (BookingStatus.IN_PROGRESS, False),
        (BookingStatus.REQUESTED, True),
    ],
)
async def test_expire_refuses_anything_but_an_unpaid_requested_booking(
    db_session,
    committed,  # noqa: F811
    status,
    paid,
):
    """Called directly, past the broadcast's own check: the window between a
    read and the commit. A PAID booking stays REQUESTED, as before."""
    booking = await _booking(db_session, committed, status=status, provider=True, paid=paid)

    engine, factory = _sync_factory()
    try:
        with factory() as db:
            _expire_unbroadcastable(db, db.get(Booking, booking.id), "no-eligible-provider")
    finally:
        engine.dispose()

    await db_session.refresh(booking)
    assert booking.status is status
    assert await _logs(db_session, booking) == []
