"""
Lifetime earnings summary (SVC-275).

Covers GET /providers/me/earnings/summary — the figure Provider Home shows.

The windowed endpoint next door returns a row per booking, which is fine over a
day and unbounded over a lifetime. This one returns two scalars whatever the
history looks like, so the tests here are mostly about *which bookings count*
rather than about shape.

The case worth having: a **cash** booking is earned and must be included, even
though the payout path deliberately excludes it. Those are two different
questions, and conflating them would understate what a provider made.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus, PaymentMethod, PaymentStatus
from app.models.booking_status_log import BookingStatusLog
from app.models.category import Category
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

SUMMARY_URL = "/api/v1/providers/me/earnings/summary"


async def _seed_service(db) -> Service:
    suffix = uuid.uuid4().hex[:8]
    category = Category(
        name=f"Repair {suffix}", slug=f"repair-{suffix}", is_active=True, sort_order=1
    )
    db.add(category)
    await db.flush()
    service = Service(
        category_id=category.id,
        name=f"AC Service {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db.add(service)
    await db.flush()
    return service


async def _seed_provider(db):
    user = await UserFactory.create(db, role="provider")
    db.add(ProviderProfile(user_id=user.id, kyc_status=KYCStatus.VERIFIED))
    await db.flush()
    return user


async def _seed_booking(
    db,
    provider,
    consumer,
    *,
    payout: float = 80.0,
    status: BookingStatus = BookingStatus.COMPLETED,
    payment_status: PaymentStatus = PaymentStatus.PAID,
    payment_method: PaymentMethod = PaymentMethod.ONLINE,
    completed_at: datetime | None = None,
    log: bool = True,
):
    service = await _seed_service(db)
    address = Address(user_id=consumer.id, label="home", full_address="x")
    db.add(address)
    await db.flush()

    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        status=status,
        payment_status=payment_status,
        payment_method=payment_method,
        provider_payout=payout,
    )
    if log:
        row = BookingStatusLog(
            booking_id=booking.id,
            from_status=BookingStatus.IN_PROGRESS.value,
            to_status=BookingStatus.COMPLETED.value,
            changed_by=provider.id,
        )
        db.add(row)
        await db.flush()
        if completed_at is not None:
            row.created_at = completed_at
            await db.flush()
    return booking


@pytest.mark.asyncio
async def test_sums_every_completed_booking_regardless_of_age(client, auth_headers, db_session):
    """Lifetime means lifetime — a year-old job counts the same as today's."""
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")
    now = datetime.now(UTC)

    await _seed_booking(db_session, provider, consumer, payout=80.0, completed_at=now)
    await _seed_booking(
        db_session, provider, consumer, payout=120.0, completed_at=now - timedelta(days=400)
    )

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))

    assert response.status_code == 200
    body = response.json()
    assert body["lifetime_payout"] == 200.0
    assert body["completed_count"] == 2


@pytest.mark.asyncio
async def test_cash_bookings_are_earned(client, auth_headers, db_session):
    """A cash job counts, even though the payout path excludes it.

    `PayoutService._owed_filter` drops cash because the provider already has the
    money in hand — paying it out again would send it twice. That is a *payout*
    rule, not an *earnings* rule. Reusing it here would understate what the
    provider made, in the platform's favour, with nothing on screen to explain
    the gap.
    """
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(
        db_session, provider, consumer, payout=150.0, payment_method=PaymentMethod.CASH
    )

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))

    assert response.json()["lifetime_payout"] == 150.0


@pytest.mark.asyncio
async def test_refunded_bookings_are_excluded(client, auth_headers, db_session):
    """A refund is no longer money the provider earned."""
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(db_session, provider, consumer, payout=80.0)
    await _seed_booking(
        db_session, provider, consumer, payout=500.0, payment_status=PaymentStatus.REFUNDED
    )

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))

    body = response.json()
    assert body["lifetime_payout"] == 80.0
    assert body["completed_count"] == 1


@pytest.mark.asyncio
async def test_unfinished_bookings_are_excluded(client, auth_headers, db_session):
    """Accepted and in-progress work is not earned yet."""
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(db_session, provider, consumer, payout=80.0)
    await _seed_booking(
        db_session, provider, consumer, payout=999.0, status=BookingStatus.IN_PROGRESS, log=False
    )
    await _seed_booking(
        db_session, provider, consumer, payout=999.0, status=BookingStatus.CANCELLED, log=False
    )

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))

    body = response.json()
    assert body["lifetime_payout"] == 80.0
    assert body["completed_count"] == 1


@pytest.mark.asyncio
async def test_counts_a_booking_once_despite_duplicate_status_logs(
    client, auth_headers, db_session
):
    """Two COMPLETED log rows must not double the money.

    The windowed endpoint joins `booking_status_logs` to bucket a booking by the
    instant it completed, which would emit one row per matching log. This query
    has no such join — the regression this pins is someone adding one back.
    """
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    booking = await _seed_booking(db_session, provider, consumer, payout=80.0)
    db_session.add(
        BookingStatusLog(
            booking_id=booking.id,
            from_status=BookingStatus.IN_PROGRESS.value,
            to_status=BookingStatus.COMPLETED.value,
            changed_by=provider.id,
        )
    )
    await db_session.flush()

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))

    body = response.json()
    assert body["lifetime_payout"] == 80.0
    assert body["completed_count"] == 1


@pytest.mark.asyncio
async def test_another_providers_earnings_are_not_counted(client, auth_headers, db_session):
    """Scoped to the caller."""
    mine = await _seed_provider(db_session)
    theirs = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")

    await _seed_booking(db_session, mine, consumer, payout=80.0)
    await _seed_booking(db_session, theirs, consumer, payout=500.0)

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(mine.id), "provider"))

    assert response.json()["lifetime_payout"] == 80.0


@pytest.mark.asyncio
async def test_fresh_provider_gets_zero_not_null(client, auth_headers, db_session):
    """Zero, so the row on Home reads "₹0" rather than looking like a failed fetch."""
    provider = await _seed_provider(db_session)

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))

    assert response.status_code == 200
    body = response.json()
    assert body["lifetime_payout"] == 0
    assert body["completed_count"] == 0


@pytest.mark.asyncio
async def test_response_carries_no_booking_breakdown(client, auth_headers, db_session):
    """The point of a separate endpoint: constant size, whatever the history.

    If someone later returns the per-booking list from here, Provider Home goes
    back to downloading every job ever completed to render one number.
    """
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")
    await _seed_booking(db_session, provider, consumer, payout=80.0)

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(provider.id), "provider"))

    assert set(response.json()) == {"lifetime_payout", "completed_count"}


@pytest.mark.asyncio
async def test_consumer_forbidden(client, auth_headers, db_session):
    """Earnings are provider-only."""
    consumer = await UserFactory.create(db_session, role="consumer")

    response = await client.get(SUMMARY_URL, headers=auth_headers(str(consumer.id), "consumer"))

    assert response.status_code == 403
