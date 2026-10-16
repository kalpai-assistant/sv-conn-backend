"""
A booking nobody can serve ends now, not in two minutes (SVC-278 issue 5).

Three conditions make a REQUESTED booking undeliverable the moment it is
created: the address has no location, the cart is empty, or no provider is
eligible. Each used to `return` out of the broadcast quietly, leaving the row at
REQUESTED until `expire_stale_bookings` swept it 120s later.

Nothing was broken in a way anyone could see. The consumer simply watched
"Finding nearby partner…" for two minutes and was then told "No partners found
nearby" — a message the backend could have shown at second zero.

The risk in fixing it is the opposite error: expiring a booking that *could*
have been served. `test_a_normal_booking_is_left_alone` is the guard, and it is
the one to keep working — a false expiry cancels a real job.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.booking import BookingStatus
from app.tasks.notification_tasks import send_booking_notification
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

PUNE = (18.5204, 73.8567)


@pytest.fixture
async def committed(db_session):
    """Commit rows the Celery task's own connection has to see, then remove them.

    The task opens its own `SyncSessionLocal`, so the usual rollback-only
    isolation hides these fixtures from it — they have to be really committed.
    That means really cleaning up too: other suites count bookings table-wide
    (`test_coupons` asserts the table is empty), so anything left behind breaks
    them.

    Cleanup is by table in FK order rather than per-object: the broadcast itself
    writes `booking_offers` rows that no test holds a reference to, so deleting
    only the objects a test created is not enough.
    """
    from sqlalchemy import delete

    from app.models.booking import Booking
    from app.models.booking_item import BookingItem
    from app.models.booking_offer import BookingOffer
    from app.models.booking_status_log import BookingStatusLog
    from app.models.provider_profile import ProviderProfile
    from app.models.provider_service import ProviderService
    from app.models.user import User

    created: list = []
    yield created

    booking_ids = [o.id for o in created if isinstance(o, Booking)]
    user_ids = [o.id for o in created if isinstance(o, User)]
    addresses = [o for o in created if type(o).__name__ == "Address"]

    if booking_ids:
        await db_session.execute(
            delete(BookingOffer).where(BookingOffer.booking_id.in_(booking_ids))
        )
        await db_session.execute(delete(BookingItem).where(BookingItem.booking_id.in_(booking_ids)))
        # SVC-373: an unbroadcastable expiry now writes a status row.
        await db_session.execute(
            delete(BookingStatusLog).where(BookingStatusLog.booking_id.in_(booking_ids))
        )
        await db_session.execute(delete(Booking).where(Booking.id.in_(booking_ids)))
    if user_ids:
        await db_session.execute(
            delete(ProviderService).where(ProviderService.provider_id.in_(user_ids))
        )
        await db_session.execute(
            delete(ProviderProfile).where(ProviderProfile.user_id.in_(user_ids))
        )
    for addr in addresses:
        await db_session.delete(addr)
    await db_session.flush()
    if user_ids:
        await db_session.execute(delete(User).where(User.id.in_(user_ids)))
    await db_session.commit()


async def _address(db, user_id, *, with_location=True):
    from app.models.address import Address
    from app.utils.geo import build_point_wkt

    addr = Address(
        user_id=user_id,
        label="home",
        full_address="Somewhere in Pune",
        location=build_point_wkt(*PUNE) if with_location else None,
    )
    db.add(addr)
    await db.flush()
    return addr


def _run_broadcast(booking_id):
    """Run the real task against the test DB, with FCM stubbed out."""
    sync_url = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url)
    test_factory = sessionmaker(bind=engine)
    try:
        with (
            patch("app.database.SyncSessionLocal", test_factory),
            patch("app.tasks.notification_tasks.send_push_notification"),
        ):
            send_booking_notification(booking_id=str(booking_id), event="new_request")
    finally:
        engine.dispose()


async def _status_of(db_session, booking):
    await db_session.refresh(booking)
    return booking.status


# ---------------------------------------------------------------------------
# The three dead ends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_eligible_provider_expires_immediately(db_session, committed):
    """The reported case: nobody to serve it, so stop pretending to search."""
    consumer = await UserFactory.create(db_session, role="consumer")
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

    _run_broadcast(booking.id)

    # Without this the row stays REQUESTED for 120s and the consumer watches a
    # spinner for a decision already made.
    assert await _status_of(db_session, booking) == BookingStatus.EXPIRED


@pytest.mark.asyncio
async def test_an_address_with_no_location_expires_immediately(db_session, committed):
    """The geo-fence needs a point; without one the broadcast cannot even run."""
    consumer = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, consumer.id, with_location=False)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )
    committed.extend([booking, addr, consumer])
    await db_session.commit()

    _run_broadcast(booking.id)

    assert await _status_of(db_session, booking) == BookingStatus.EXPIRED


# ---------------------------------------------------------------------------
# The error that matters more than the bug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_normal_booking_is_left_alone(db_session, committed):
    """A booking with a real eligible provider must NOT be expired.

    This is the test to keep passing. Over-expiring is worse than the original
    two-minute wait: it kills jobs a provider was about to accept, and the
    consumer sees "No partners found nearby" while a partner is sitting right
    there. Any future edit to the dead-end logic that catches this case is a
    regression regardless of what else it fixes.
    """
    from app.models.provider_profile import KYCStatus, ProviderProfile
    from app.models.provider_service import ProviderService
    from app.utils.geo import build_point_wkt

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    addr = await _address(db_session, consumer.id)

    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )

    # Make this provider genuinely eligible: online, verified, subscribed to
    # the booked service, and with the address inside their radius.
    profile = ProviderProfile(
        user_id=provider.id,
        is_online=True,
        kyc_status=KYCStatus.VERIFIED,
        service_area_center=build_point_wkt(*PUNE),
        service_area_radius_km=25,
    )
    db_session.add(profile)
    await db_session.flush()

    await db_session.refresh(booking)
    from sqlalchemy import select

    from app.models.booking_item import BookingItem

    items = (
        (await db_session.execute(select(BookingItem).where(BookingItem.booking_id == booking.id)))
        .scalars()
        .all()
    )
    for item in items:
        db_session.add(ProviderService(provider_id=provider.id, service_id=item.service_id))
    await db_session.flush()

    committed.extend([booking, addr, provider, consumer])
    await db_session.commit()

    _run_broadcast(booking.id)

    assert await _status_of(db_session, booking) == BookingStatus.REQUESTED, (
        "a serviceable booking was expired — the consumer is told nobody is "
        "available while an eligible provider is online"
    )


@pytest.mark.asyncio
async def test_a_missing_booking_does_not_raise(db_session):
    """The task retries three times; a deleted booking must not loop on errors."""
    _run_broadcast(uuid.uuid4())


# ---------------------------------------------------------------------------
# Paid bookings are never expired here (SVC-320 PR 2)
# ---------------------------------------------------------------------------
#
# Expiry doesn't refund, and force_cancel refuses an EXPIRED booking, so a paid
# booking expired by the broadcast would strand the money. It stays REQUESTED
# instead; the consumer's cancel refunds it. Unpaid ones still expire at once
# (test_no_eligible_provider_expires_immediately above is the regression guard).


@pytest.mark.asyncio
async def test_a_paid_booking_with_no_eligible_provider_is_kept(db_session, committed, caplog):
    import logging

    from app.models.booking import PaymentStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
        payment_status=PaymentStatus.PAID,
    )
    committed.extend([booking, addr, consumer])
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger="app.tasks.notification_tasks"):
        _run_broadcast(booking.id)

    assert await _status_of(db_session, booking) == BookingStatus.REQUESTED
    assert any(
        "booking.unbroadcastable_paid_kept" in r.getMessage()
        and "reason=no-eligible-provider" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_reopened_paid_booking_with_no_takers_is_kept(db_session, committed):
    """The case reopen creates: the only eligible SP is the one that gave it back,
    so the re-broadcast has no recipients. Paid, so it must not expire."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.booking import PaymentStatus
    from app.models.booking_item import BookingItem
    from app.models.booking_offer import BookingOffer, OfferResponse
    from app.models.provider_profile import KYCStatus, ProviderProfile
    from app.models.provider_service import ProviderService
    from app.utils.geo import build_point_wkt

    consumer = await UserFactory.create(db_session, role="consumer")
    releaser = await UserFactory.create(db_session, role="provider")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
        payment_status=PaymentStatus.PAID,
    )
    booking.reopened_at = datetime.now(UTC)
    db_session.add(
        ProviderProfile(
            user_id=releaser.id,
            is_online=True,
            kyc_status=KYCStatus.VERIFIED,
            service_area_center=build_point_wkt(*PUNE),
            service_area_radius_km=25,
        )
    )
    await db_session.flush()
    items = (
        await db_session.scalars(select(BookingItem).where(BookingItem.booking_id == booking.id))
    ).all()
    for item in items:
        db_session.add(ProviderService(provider_id=releaser.id, service_id=item.service_id))
    # What reopen leaves behind: the releasing SP's answered offer row.
    db_session.add(
        BookingOffer(
            booking_id=booking.id,
            provider_id=releaser.id,
            response=OfferResponse.ACCEPTED,
            responded_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    committed.extend([booking, addr, releaser, consumer])
    await db_session.commit()

    _run_broadcast(booking.id)

    assert await _status_of(db_session, booking) == BookingStatus.REQUESTED
