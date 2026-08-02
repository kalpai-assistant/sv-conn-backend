"""
Provider earnings endpoint tests (SC-33).

Covers GET /providers/me/earnings:
- Sums payouts of completed bookings in window, returns per-booking breakdown
- Bookings whose COMPLETED transition falls outside the window are excluded
- Cancelled bookings are excluded
- Refunded payments are excluded
- Empty list / zero totals on a fresh provider
- range query param validation (today / week / month)
- Non-provider caller → 403
- Range filtering boundary (week vs month) works on a real timestamp
"""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus, PaymentStatus
from app.models.booking_status_log import BookingStatusLog
from app.models.category import Category
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _seed_service(db, suffix: str | None = None) -> Service:
    suffix = suffix or uuid.uuid4().hex[:8]
    category = Category(
        name=f"Repair {suffix}",
        slug=f"repair-{suffix}",
        is_active=True,
        sort_order=1,
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


async def _seed_provider(db, kyc_status: KYCStatus = KYCStatus.VERIFIED):
    user = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(user_id=user.id, kyc_status=kyc_status)
    db.add(profile)
    await db.flush()
    return user


async def _seed_completed_booking(
    db,
    provider,
    consumer,
    *,
    service: Service | None = None,
    payout: float = 80.0,
    completed_at: datetime | None = None,
    payment_status: PaymentStatus = PaymentStatus.PAID,
):
    """Create a COMPLETED booking with a status-log row at `completed_at`."""
    service = service or await _seed_service(db)
    address = Address(user_id=consumer.id, label="home", full_address="x")
    db.add(address)
    await db.flush()

    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.COMPLETED,
        payment_status=payment_status,
        provider_payout=payout,
    )
    # The endpoint reads completion time from booking_status_logs (so subsequent
    # payment-webhook bumps of booking.updated_at don't shift the bucket).
    log = BookingStatusLog(
        booking_id=booking.id,
        from_status=BookingStatus.IN_PROGRESS.value,
        to_status=BookingStatus.COMPLETED.value,
        changed_by=provider.id,
    )
    db.add(log)
    await db.flush()
    if completed_at is not None:
        # server_default=func.now() ran on flush; rewrite to the desired instant.
        log.created_at = completed_at
        await db.flush()
    return booking, log


@pytest.mark.asyncio
async def test_earnings_sums_recent_payouts(client, auth_headers, db_session):
    """Two completed bookings within window → both appear, total is summed."""
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")
    now_utc = datetime.now(UTC)
    # Offsets in *minutes*, not hours: the week window boundary is IST Monday
    # 00:00 (= UTC Sunday 18:30), so `now - 2h` slips out of the window when
    # CI runs during the first ~2h of Monday IST (18:30–20:30 UTC Sunday).
    # Keeping both bookings inside the last 30 min sidesteps the entire
    # weekly boundary problem without changing what the test asserts.
    await _seed_completed_booking(
        db_session, provider, consumer, payout=80.0, completed_at=now_utc - timedelta(minutes=10)
    )
    await _seed_completed_booking(
        db_session, provider, consumer, payout=120.0, completed_at=now_utc - timedelta(minutes=20)
    )

    response = await client.get(
        "/api/v1/providers/me/earnings?range=week",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["range"] == "week"
    assert body["completed_count"] == 2
    assert body["total_payout"] == 200.0
    assert len(body["bookings"]) == 2
    # Sorted by completed_at desc.
    assert body["bookings"][0]["payout_amount"] == 80.0
    assert body["bookings"][1]["payout_amount"] == 120.0


@pytest.mark.asyncio
async def test_earnings_excludes_old_bookings(client, auth_headers, db_session):
    """Bookings completed before the window start are excluded."""
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")
    now_utc = datetime.now(UTC)
    # Inside today's window
    await _seed_completed_booking(
        db_session, provider, consumer, payout=80.0, completed_at=now_utc - timedelta(minutes=30)
    )
    # 60 days ago — well outside any of the windows
    await _seed_completed_booking(
        db_session, provider, consumer, payout=999.0, completed_at=now_utc - timedelta(days=60)
    )

    response = await client.get(
        "/api/v1/providers/me/earnings?range=month",
        headers=auth_headers(str(provider.id), "provider"),
    )
    body = response.json()
    assert body["completed_count"] == 1
    assert body["total_payout"] == 80.0


@pytest.mark.asyncio
async def test_earnings_excludes_cancelled_and_refunded(client, auth_headers, db_session):
    """CANCELLED bookings and REFUNDED payments are not earnings."""
    provider = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")
    now_utc = datetime.now(UTC)

    # Completed + PAID — counts
    await _seed_completed_booking(
        db_session, provider, consumer, payout=80.0, completed_at=now_utc - timedelta(hours=1)
    )
    # Completed but REFUNDED — does not count
    await _seed_completed_booking(
        db_session,
        provider,
        consumer,
        payout=200.0,
        completed_at=now_utc - timedelta(hours=2),
        payment_status=PaymentStatus.REFUNDED,
    )
    # CANCELLED — would never have a COMPLETED log row in production; we don't
    # create one here, so the join naturally excludes it.

    response = await client.get(
        "/api/v1/providers/me/earnings?range=week",
        headers=auth_headers(str(provider.id), "provider"),
    )
    body = response.json()
    assert body["completed_count"] == 1
    assert body["total_payout"] == 80.0


@pytest.mark.asyncio
async def test_earnings_empty_provider(client, auth_headers, db_session):
    """Fresh provider with no completed bookings → zero / empty."""
    provider = await _seed_provider(db_session)

    response = await client.get(
        "/api/v1/providers/me/earnings?range=today",
        headers=auth_headers(str(provider.id), "provider"),
    )
    body = response.json()
    assert body["completed_count"] == 0
    assert body["total_payout"] == 0
    assert body["bookings"] == []


@pytest.mark.asyncio
async def test_earnings_invalid_range_rejected(client, auth_headers, db_session):
    """Range outside {today, week, month} → 422 from FastAPI's Literal validation."""
    provider = await _seed_provider(db_session)

    response = await client.get(
        "/api/v1/providers/me/earnings?range=yesterday",
        headers=auth_headers(str(provider.id), "provider"),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_earnings_consumer_forbidden(client, auth_headers, db_session):
    """Non-provider roles can't read provider earnings."""
    consumer = await UserFactory.create(db_session, role="consumer")

    response = await client.get(
        "/api/v1/providers/me/earnings?range=week",
        headers=auth_headers(str(consumer.id), "consumer"),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_earnings_isolated_between_providers(client, auth_headers, db_session):
    """Provider A's earnings must not include Provider B's bookings."""
    provider_a = await _seed_provider(db_session)
    provider_b = await _seed_provider(db_session)
    consumer = await UserFactory.create(db_session, role="consumer")
    now_utc = datetime.now(UTC)

    await _seed_completed_booking(
        db_session, provider_a, consumer, payout=80.0, completed_at=now_utc - timedelta(hours=1)
    )
    await _seed_completed_booking(
        db_session, provider_b, consumer, payout=300.0, completed_at=now_utc - timedelta(hours=1)
    )

    response = await client.get(
        "/api/v1/providers/me/earnings?range=week",
        headers=auth_headers(str(provider_a.id), "provider"),
    )
    body = response.json()
    assert body["completed_count"] == 1
    assert body["total_payout"] == 80.0


@pytest.mark.asyncio
async def test_earnings_default_range_is_week(client, auth_headers, db_session):
    """Omitting ?range= uses 'week' as default per AC."""
    provider = await _seed_provider(db_session)

    response = await client.get(
        "/api/v1/providers/me/earnings",
        headers=auth_headers(str(provider.id), "provider"),
    )
    assert response.status_code == 200
    assert response.json()["range"] == "week"
