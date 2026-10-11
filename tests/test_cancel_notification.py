"""
Cancellation notifies the other party (SVC-278 issue 4).

Cancel was the only lifecycle transition that told nobody. `new_request`,
`accepted`, `in_progress` and `completed` all fire `send_booking_notification`
from the same router; cancel just returned. So a provider who had accepted a
job found out it was called off by opening the app — potentially after
travelling to the address.

The thing worth testing is not *that* a push happens but **who receives it**.
Either party can cancel, so the recipient is whoever did not. Get that backwards
and you tell someone their own action occurred while the person actually waiting
on the news hears nothing — a failure that looks completely normal in logs and
shows up only on a device.
"""

import uuid
from unittest.mock import patch

import pytest

from app.models.booking import BookingStatus
from app.tasks.notification_tasks import NOTIFICATION_TEMPLATES, send_booking_notification
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


@pytest.fixture
async def committed(db_session):
    """Commit test rows, then remove them again.

    These tests have to commit: the Celery task opens its own connection and
    cannot see an uncommitted transaction, so the usual rollback-only isolation
    does not work here. Committing leaks rows into `sc_test` though, and other
    suites count bookings table-wide (`test_coupons` asserts there are none), so
    what this commits it also cleans up.
    """
    created: list = []
    yield created
    for obj in reversed(created):
        await db_session.delete(obj)
    await db_session.commit()


async def _address(db, user_id):
    """A real address row — BookingFactory only invents an id, and the FK is real."""
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


@pytest.mark.asyncio
async def test_cancelling_fires_a_notification_at_all(client, auth_headers, db_session):
    """The regression itself: this route used to dispatch nothing."""
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
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "changed my mind"},
            headers=auth_headers(str(consumer.id), "consumer"),
        )

    assert response.status_code == 200
    assert sent, "cancelling dispatched no notification — the other party is never told"
    assert sent[0]["event"] == "cancelled"


@pytest.mark.asyncio
async def test_the_canceller_is_passed_through_so_the_task_can_pick_a_recipient(
    client, auth_headers, db_session
):
    """Without `actor_id` the task cannot tell which direction this cancel went."""
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
        await client.patch(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "changed my mind"},
            headers=auth_headers(str(consumer.id), "consumer"),
        )

    assert sent[0]["actor_id"] == str(consumer.id)


# ---------------------------------------------------------------------------
# Who actually gets the push — the part that fails silently when wrong
# ---------------------------------------------------------------------------


def _tokens_pushed_to(booking_id, event, actor_id):
    """Run the real task, capturing the FCM tokens it would send to.

    The task opens its own ``SyncSessionLocal``, which resolves to the app's
    configured database rather than ``sc_test`` — so it is pointed at the test
    DB here. This runs the real recipient-selection code; only the connection
    is substituted.
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
        send_booking_notification(booking_id=str(booking_id), event=event, actor_id=actor_id)
    engine.dispose()
    return tokens


@pytest.mark.asyncio
async def test_consumer_cancels_so_the_provider_is_told(
    client, auth_headers, db_session, committed
):
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    consumer.fcm_token = "consumer-token"
    provider.fcm_token = "provider-token"
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.ACCEPTED,
    )
    committed.extend([booking, addr, provider, consumer])
    await db_session.commit()

    tokens = _tokens_pushed_to(booking.id, "cancelled", str(consumer.id))

    # The reported bug. Telling the consumer instead would be worse than
    # silence: they already know, and the provider still travels to the job.
    assert tokens == ["provider-token"]


@pytest.mark.asyncio
async def test_provider_cancels_so_the_consumer_is_told(
    client, auth_headers, db_session, committed
):
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    consumer.fcm_token = "consumer-token"
    provider.fcm_token = "provider-token"
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.ACCEPTED,
    )
    committed.extend([booking, addr, provider, consumer])
    await db_session.commit()

    tokens = _tokens_pushed_to(booking.id, "cancelled", str(provider.id))

    # The mirror case. A hardcoded recipient passes one of these two tests and
    # fails the other, which is the point of having both.
    assert tokens == ["consumer-token"]


@pytest.mark.asyncio
async def test_cancelling_before_anyone_accepts_pushes_to_nobody(
    client, auth_headers, db_session, committed
):
    """REQUESTED → CANCELLED is legal and there is no provider yet.

    `provider_id` is only set on acceptance, so this must not raise and must
    not push. Correct rather than a gap: nobody has committed to the job.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    consumer.fcm_token = "consumer-token"
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )
    committed.extend([booking, addr, consumer])
    await db_session.commit()

    tokens = _tokens_pushed_to(booking.id, "cancelled", str(consumer.id))

    assert tokens == []


@pytest.mark.asyncio
async def test_a_third_party_actor_reaches_both_parties(
    client, auth_headers, db_session, committed
):
    """An actor who is neither party (an admin force-cancel) tells both (SVC-348).

    Before, this fell to the provider only, so a consumer whose booking support
    cancelled heard nothing. The provider must still be told: they may be
    travelling.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    consumer.fcm_token = "consumer-token"
    provider.fcm_token = "provider-token"
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.ACCEPTED,
    )
    committed.extend([booking, addr, provider, consumer])
    await db_session.commit()

    tokens = _tokens_pushed_to(booking.id, "cancelled", str(uuid.uuid4()))

    assert tokens == ["consumer-token", "provider-token"]


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


def test_the_wording_works_whoever_receives_it():
    """One template serves both directions, so it must not name a role.

    "Your provider cancelled" sent to a provider is nonsense, and this is the
    only event whose recipient is not fixed.
    """
    body = NOTIFICATION_TEMPLATES["cancelled"]["body"].lower()

    assert "provider" not in body
    assert "customer" not in body
    assert "consumer" not in body
