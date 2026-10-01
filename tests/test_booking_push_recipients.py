"""
Who receives each booking lifecycle push (SVC-334).

`accepted` used to go to the provider: the one person who already knew, because
they had just tapped Accept. The consumer heard nothing until "Service Started",
when the provider was already at the door. `en_route` sent nothing at all.

Same failure shape as SVC-278's cancel bug (see test_cancel_notification.py): a
push to the wrong person looks completely normal in logs. So these tests pin the
**recipient** by running the real task against committed rows and capturing the
FCM tokens it sends to.
"""

from unittest.mock import patch

import pytest

from app.models.booking import BookingStatus
from app.tasks.notification_tasks import NOTIFICATION_TEMPLATES, send_booking_notification
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _accept, _booking, _eligible_provider, _service


@pytest.fixture
async def committed(db_session):
    """Commit test rows, then remove them again.

    The Celery task opens its own connection and cannot see an uncommitted
    transaction, so these rows have to be committed, and anything committed is
    cleaned up again. Other suites count bookings table-wide.
    """
    created: list = []
    yield created
    for obj in reversed(created):
        await db_session.delete(obj)
    await db_session.commit()


async def _address(db, user_id):
    """A real address row. BookingFactory only makes up an id, and the FK is real."""
    from app.models.address import Address
    from app.utils.geo import build_point_wkt

    addr = Address(
        user_id=user_id,
        label="home",
        full_address="Somewhere in Pune",
        location=build_point_wkt(18.5204, 73.8567),
    )
    db.add(addr)
    await db.flush()
    return addr


async def _committed_booking(db_session, committed, status):
    """An accepted-onwards booking where both parties have a distinct token."""
    consumer = await UserFactory.create(db_session, role="consumer", fcm_token="consumer-token")
    provider = await UserFactory.create(db_session, role="provider", fcm_token="provider-token")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=status,
    )
    committed.extend([booking, addr, provider, consumer])
    await db_session.commit()
    return booking


def _tokens_pushed_to(booking_id, event):
    """Run the real task and capture the FCM tokens it would send to.

    Only the connection is substituted (the task's ``SyncSessionLocal`` would
    otherwise point at the app DB, not the test DB). Recipient selection is the
    real code.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_url = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url)
    test_factory = sessionmaker(bind=engine)

    tokens = []
    with (
        patch("app.database.SyncSessionLocal", test_factory),
        patch("app.tasks.notification_tasks.send_push_notification") as mock_push,
    ):
        mock_push.side_effect = lambda token, **kwargs: tokens.append(token)
        send_booking_notification(booking_id=str(booking_id), event=event)
    engine.dispose()
    return tokens


# ---------------------------------------------------------------------------
# Task level: the recipient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_goes_to_the_consumer_not_the_provider(db_session, committed):
    """The reported bug. "Booking Confirmed" is news only to the consumer."""
    booking = await _committed_booking(db_session, committed, BookingStatus.ACCEPTED)

    assert _tokens_pushed_to(booking.id, "accepted") == ["consumer-token"]


@pytest.mark.asyncio
async def test_en_route_goes_to_the_consumer(db_session, committed):
    booking = await _committed_booking(db_session, committed, BookingStatus.EN_ROUTE)

    assert _tokens_pushed_to(booking.id, "en_route") == ["consumer-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "status"),
    [
        ("in_progress", BookingStatus.IN_PROGRESS),
        ("completed", BookingStatus.COMPLETED),
    ],
)
async def test_later_lifecycle_events_still_go_to_the_consumer(
    db_session, committed, event, status
):
    """Regression guard: these share the branch the fix rewrote."""
    booking = await _committed_booking(db_session, committed, status)

    assert _tokens_pushed_to(booking.id, event) == ["consumer-token"]


def test_en_route_has_a_template():
    """Without one the task returns early and the push silently never goes."""
    template = NOTIFICATION_TEMPLATES["en_route"]

    assert template["title"]
    assert template["body"]


# ---------------------------------------------------------------------------
# Router level: what gets enqueued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setting_off_enqueues_an_en_route_push(client, auth_headers, db_session):
    """PATCH /en-route used to change the status and tell nobody."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.ACCEPTED,
    )

    with patch("app.routers.bookings.send_booking_notification") as mock_notif:
        sent = []
        mock_notif.delay = lambda **kwargs: sent.append(kwargs)
        response = await client.patch(
            f"/api/v1/bookings/{booking.id}/en-route",
            headers=auth_headers(str(provider.id), "provider"),
        )

    assert response.status_code == 200, response.text
    assert sent == [{"booking_id": str(booking.id), "event": "en_route"}]


@pytest.mark.asyncio
async def test_reaccept_enqueues_no_second_accepted_push(client, auth_headers, db_session):
    """SVC-154 idempotency, checked against the event now aimed at the consumer.

    A retry after a lost response is not a new acceptance. Now that "Booking
    Confirmed" reaches the consumer, a second one would be a visible duplicate.
    """
    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    with patch("app.routers.bookings.send_booking_notification") as mock_notif:
        sent = []
        mock_notif.delay = lambda **kwargs: sent.append(kwargs)
        first = await _accept(client, booking.id, provider, auth_headers)
        second = await _accept(client, booking.id, provider, auth_headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert [s["event"] for s in sent] == ["accepted"]
