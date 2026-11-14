"""
SVC-243 regression lock (G6, SVC-462).

The ``expire_stale_bookings`` sweep must honour the ``scheduled_at`` guard added in
``app/tasks/cleanup_tasks.py``::

    or_(Booking.scheduled_at.is_(None), Booking.scheduled_at <= now)

Failure mode this test would catch: a refactor drops that ``or_(...)`` clause and
every future-scheduled booking in beta dies silently 120 s after creation (the
original SVC-243 bug). The clause is 1 line of SQL; a routine ``update ... where``
simplification could remove it without anyone noticing, because the three-line
``test_sweep_paid_cycle_end_to_end_and_unpaid_unchanged`` only glances at the
scheduled case alongside four other assertions.

This file isolates the guard into its own focused tests so a regression shows up
as a clearly-named red test, not a blended one.

Styled on ``tests/test_paid_no_taker.py`` — committed rows + the real sync sweep
under the shared ``committed_sweep_world`` fixture pattern. We re-implement a
trimmed fixture here so the two files don't develop a shared-fixture coupling.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.booking import Booking, BookingStatus, PaymentMethod, PaymentStatus
from app.tasks import notification_tasks
from tests.conftest import TEST_DATABASE_URL


def _ago(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def _from_now(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _sync_url() -> str:
    return TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(autouse=True, scope="module")
def _ensure_schema():
    """Create tables once for the module.

    ``conftest.py``'s ``db_session`` is the usual mechanism, but these sync tests
    don't take it (they use ``sync_factory`` directly). Without this fixture, a
    standalone ``pytest tests/test_svc_243_scheduled_sweep_guard.py`` fails with
    ``relation "users" does not exist``.
    """
    import asyncio

    import app.models  # noqa: F401  — imports every model into Base.metadata
    from app.database import Base

    async def _create():
        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())


@pytest.fixture
def sweep_world():
    """Point both session factories the sweep uses at the test DB, with cleanup.

    Mirrors ``committed_sweep_world`` in test_paid_no_taker.py, scoped to this
    file so the two tests never develop a hidden fixture coupling.
    """
    engine = create_engine(_sync_url())
    sync_factory = sessionmaker(bind=engine, expire_on_commit=False)
    made: dict[str, list] = {"users": [], "bookings": []}

    def async_factory():
        return async_sessionmaker(create_async_engine(TEST_DATABASE_URL), expire_on_commit=False)()

    with (
        patch("app.database.SyncSessionLocal", sync_factory),
        patch("app.database.AsyncSessionLocal", async_factory),
    ):
        yield sync_factory, made

    with sync_factory() as db:
        ids = [str(b) for b in made["bookings"]]
        if ids:
            for table in (
                "booking_status_logs",
                "payments",
                "booking_offers",
                "booking_items",
            ):
                db.execute(
                    text(f"DELETE FROM {table} WHERE booking_id::text = ANY(:ids)"),
                    {"ids": ids},
                )
            db.execute(text("DELETE FROM bookings WHERE id::text = ANY(:ids)"), {"ids": ids})
        users = [str(u) for u in made["users"]]
        if users:
            db.execute(
                text("DELETE FROM addresses WHERE user_id::text = ANY(:ids)"), {"ids": users}
            )
            db.execute(text("DELETE FROM users WHERE id::text = ANY(:ids)"), {"ids": users})
        db.commit()
    engine.dispose()


def _seed_unpaid(sync_factory, made, *, scheduled_at: datetime | None, age: int = 130):
    """A committed REQUESTED, unpaid, ONLINE booking at the given age + schedule.

    Unpaid is enough to exercise the ``scheduled_at`` guard — the sweep's
    unpaid branch (line 48 of cleanup_tasks.py) and paid branch (line 79) both
    read the same ``due`` list. Unpaid runs in one synchronous ``UPDATE``, so
    no mocking is needed; this file deliberately avoids the paid branch's
    Razorpay-refund path, which has its own coverage in test_paid_no_taker.py.
    """
    from app.models.address import Address
    from app.models.user import User, UserRole

    with sync_factory() as db:
        consumer = User(
            phone=f"+9199{uuid.uuid4().int % 10**8:08d}",
            role=UserRole.CONSUMER,
            name="SVC-243 tester",
        )
        db.add(consumer)
        db.flush()
        address = Address(user_id=consumer.id, label="home", full_address="1 Test Road")
        db.add(address)
        db.flush()
        booking = Booking(
            consumer_id=consumer.id,
            consumer_address_id=address.id,
            status=BookingStatus.REQUESTED,
            service_amount=Decimal("500"),
            discount_amount=Decimal("0"),
            platform_commission=Decimal("100"),
            provider_payout=Decimal("400"),
            tip_amount=Decimal("0"),
            payment_method=PaymentMethod.ONLINE,
            payment_status=PaymentStatus.PENDING,  # unpaid → the sweep's bulk-expire branch
            doorstep_otp="1234",
            created_at=_ago(age),
            scheduled_at=scheduled_at,
        )
        db.add(booking)
        db.flush()
        made["users"].append(consumer.id)
        made["bookings"].append(booking.id)
        db.commit()
        return booking.id


def _get(sync_factory, booking_id) -> Booking:
    with sync_factory() as db:
        return db.get(Booking, booking_id)


def _run_sweep():
    """Run the real sweep with Razorpay + the push queue stubbed.

    The unpaid branch doesn't call either, but patching keeps the test honest
    if a future refactor pushes the sweep through the paid-branch code path.
    """
    from app.tasks.cleanup_tasks import expire_stale_bookings

    with (
        patch("app.services.payment_service.razorpay_client"),
        patch.object(notification_tasks.send_booking_notification, "delay"),
    ):
        return expire_stale_bookings()


def test_future_scheduled_booking_is_not_expired(sweep_world):
    """A ``scheduled_at > now`` booking must survive the sweep, even when its
    ``offer_expires_at`` has elapsed. This is SVC-243 — the original bug.
    """
    sync_factory, made = sweep_world
    booking_id = _seed_unpaid(
        sync_factory,
        made,
        scheduled_at=_from_now(2 * 3600),  # 2 hours in the future
        age=130,  # offer_expires_at = created_at + 120 s → expired by wall-clock
    )

    _run_sweep()

    survivor = _get(sync_factory, booking_id)
    assert survivor.status is BookingStatus.REQUESTED, (
        "SVC-243 regression: a scheduled-future booking was expired by the sweep. "
        "Check app/tasks/cleanup_tasks.py — the or_(scheduled_at IS NULL, scheduled_at <= now) "
        "guard was removed."
    )


def test_past_scheduled_booking_is_expired(sweep_world):
    """The complementary case: a booking whose ``scheduled_at`` has arrived
    (``scheduled_at <= now``) should still expire when its offer window elapses.

    This guards against the opposite regression: someone "fixes" SVC-243 too
    aggressively and introduces a filter that holds back every scheduled
    booking forever.
    """
    sync_factory, made = sweep_world
    booking_id = _seed_unpaid(
        sync_factory,
        made,
        scheduled_at=_ago(300),  # 5 minutes in the past — slot has arrived
        age=130,  # offer window elapsed
    )

    _run_sweep()

    expired = _get(sync_factory, booking_id)
    assert expired.status is BookingStatus.EXPIRED, (
        "A past-scheduled booking should be expired like any other once its " "offer window closes."
    )


def test_unscheduled_booking_is_expired(sweep_world):
    """Baseline: a plain ``scheduled_at IS NULL`` booking must still expire.

    This asserts the ``or_(Booking.scheduled_at.is_(None), ...)`` arm hasn't
    been dropped — a half-fix of SVC-243 that only handled the "arrived" case
    would miss every on-demand booking and let them accumulate in REQUESTED.
    """
    sync_factory, made = sweep_world
    booking_id = _seed_unpaid(sync_factory, made, scheduled_at=None, age=130)

    _run_sweep()

    expired = _get(sync_factory, booking_id)
    assert expired.status is BookingStatus.EXPIRED
