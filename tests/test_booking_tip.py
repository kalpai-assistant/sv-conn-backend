"""Tip on a completed booking — set + update + access + state guards."""

import pytest
from sqlalchemy import select

from app.exceptions import InvalidStateTransition, UnauthorizedAction
from app.models.address import Address
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.services.booking_service import BookingService
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _completed_booking(db_session, consumer=None, provider=None):
    """A COMPLETED booking wired to a real Address row so its FK holds."""
    consumer = consumer or await UserFactory.create(db_session, role="consumer")
    provider = provider or await UserFactory.create(db_session, role="provider")
    addr = Address(user_id=consumer.id, label="home", full_address="Test, Pune")
    db_session.add(addr)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.COMPLETED,
    )
    return booking, consumer, provider


@pytest.mark.asyncio
async def test_consumer_sets_a_tip_on_their_completed_booking(db_session):
    booking, consumer, _ = await _completed_booking(db_session)

    updated = await BookingService(db_session).set_tip(
        booking_id=booking.id, consumer_id=consumer.id, amount=50
    )
    assert updated.tip_amount == 50.0

    # Persisted.
    fresh = (await db_session.execute(select(Booking).where(Booking.id == booking.id))).scalar_one()
    assert fresh.tip_amount == 50.0


@pytest.mark.asyncio
async def test_updating_tip_replaces_rather_than_accumulates(db_session):
    booking, consumer, _ = await _completed_booking(db_session)
    svc = BookingService(db_session)

    await svc.set_tip(booking.id, consumer.id, 100)
    updated = await svc.set_tip(booking.id, consumer.id, 50)
    assert updated.tip_amount == 50.0  # replaced, not 150


@pytest.mark.asyncio
async def test_amount_zero_removes_the_tip(db_session):
    booking, consumer, _ = await _completed_booking(db_session)
    svc = BookingService(db_session)

    await svc.set_tip(booking.id, consumer.id, 50)
    updated = await svc.set_tip(booking.id, consumer.id, 0)
    assert updated.tip_amount == 0.0


@pytest.mark.asyncio
async def test_only_the_booking_consumer_can_tip(db_session):
    booking, _, _ = await _completed_booking(db_session)
    stranger = await UserFactory.create(db_session, role="consumer")

    with pytest.raises(UnauthorizedAction):
        await BookingService(db_session).set_tip(
            booking_id=booking.id, consumer_id=stranger.id, amount=50
        )


@pytest.mark.asyncio
async def test_tipping_a_non_completed_booking_is_refused(db_session):
    """Tips are a post-completion action — the whole point is the customer
    saw the job done and chose to add to it.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    addr = Address(user_id=consumer.id, label="home", full_address="Test, Pune")
    db_session.add(addr)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.EN_ROUTE,
    )

    with pytest.raises(InvalidStateTransition):
        await BookingService(db_session).set_tip(
            booking_id=booking.id, consumer_id=consumer.id, amount=50
        )


@pytest.mark.asyncio
async def test_refunded_booking_refuses_further_tips(db_session):
    """Once the money has left the platform we cannot honour a new tip.

    Enforced in the service so the endpoint doesn't need its own
    duplicated check.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    addr = Address(user_id=consumer.id, label="home", full_address="Test, Pune")
    db_session.add(addr)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.REFUNDED,
    )

    with pytest.raises(InvalidStateTransition):
        await BookingService(db_session).set_tip(
            booking_id=booking.id, consumer_id=consumer.id, amount=50
        )
