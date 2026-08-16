"""
T+1 settlement gate tests (SVC-177).

Covers:
- `wallet_balance_paise` excludes BOOKING_COMPLETED credits whose backing
  booking has `settled_at IS NULL`.
- Non-credit entries (PAYOUT, CASH_COMMISSION, DISPUTE_LOSS, REFUND) always
  count regardless of settlement — they represent money that either already
  moved or is owed to the platform.
- `settlement.processed` webhook stamps `bookings.settled_at`, unlocking the
  wallet balance for those bookings.
- Idempotency: redelivered webhook preserves the original `settled_at`
  timestamp.
- `AUTO_SETTLE_ON_COMPLETION` config flag: default True (beta) stamps
  `settled_at` at booking completion so the tester runbook works without
  the Razorpay webhook wired; False (prod) waits for the webhook.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.booking import (
    Booking,
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.provider_ledger_entry import LedgerEntryType, ProviderLedgerEntry
from app.services import ledger_service
from app.services.booking_service import BookingService
from app.services.payment_service import PaymentService
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _seed_online_booking_unsettled(db, *, consumer, provider, provider_payout=80.0):
    """COMPLETED+PAID+online booking WITH a wallet credit but WITHOUT settled_at.

    Represents the T+1 window — money captured at Razorpay but not yet in
    our bank account.
    """
    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    suffix = uuid.uuid4().hex[:8]
    cat = Category(name=f"C {suffix}", slug=f"c-{suffix}", is_active=True, sort_order=1)
    db.add(cat)
    await db.flush()
    svc = Service(
        category_id=cat.id,
        name=f"S {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db.add(svc)
    addr = Address(user_id=consumer.id, label="home", full_address="Addr")
    db.add(addr)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        payment_method=PaymentMethod.ONLINE,
        provider_payout=provider_payout,
    )
    # Write the credit — settled_at deliberately NULL.
    from app.utils.money import to_paise

    await ledger_service.credit_booking_completed(
        db,
        provider_id=provider.id,
        booking_id=booking.id,
        amount_paise=to_paise(booking.provider_payout + booking.tip_amount),
    )
    return booking


# ---------------------------------------------------------------------------
# wallet_balance_paise T+1 filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsettled_credits_excluded_from_wallet(db_session):
    """A BOOKING_COMPLETED credit for a booking with settled_at NULL doesn't
    count toward the wallet balance. Guards the RBI PA rule: don't pay a
    provider out of money we haven't received yet.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    await _seed_online_booking_unsettled(
        db_session, consumer=consumer, provider=provider, provider_payout=80.0
    )

    # Credit is on the ledger...
    r = await db_session.execute(
        select(ProviderLedgerEntry).where(ProviderLedgerEntry.provider_id == provider.id)
    )
    assert len(list(r.scalars().all())) == 1
    # ...but wallet balance ignores it because settled_at is NULL.
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 0


@pytest.mark.asyncio
async def test_settled_credits_counted_in_wallet(db_session):
    """Once settled_at is stamped, the credit joins the wallet."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_online_booking_unsettled(
        db_session, consumer=consumer, provider=provider, provider_payout=80.0
    )

    booking.settled_at = datetime.now(UTC)
    await db_session.flush()

    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 8000


@pytest.mark.asyncio
async def test_debits_always_count_regardless_of_settlement(db_session):
    """PAYOUT / CASH_COMMISSION / DISPUTE_LOSS / REFUND debits all count
    immediately — they don't depend on Razorpay settling anything.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")

    # Cash commission debit — no BOOKING credit at all.
    from app.services.ledger_service import sync_debit_cash_commission

    # sync helper can be run against the async session (adds row, doesn't commit).
    entry = ProviderLedgerEntry(
        provider_id=provider.id,
        amount_paise=-5000,
        entry_type=LedgerEntryType.CASH_COMMISSION,
        reference_type=None,
        reference_id=None,
        note="test",
    )
    db_session.add(entry)
    await db_session.flush()

    # No settlement in play, no credits, but debit shows up.
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == -5000
    _ = consumer, sync_debit_cash_commission  # touch imports


# ---------------------------------------------------------------------------
# settlement.processed webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_processed_stamps_bookings(db_session):
    """Webhook stamps settled_at on every booking whose payment_id was in
    the settlement batch. Wallet balance for those providers then
    reflects the newly-unlocked credit.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_online_booking_unsettled(
        db_session, consumer=consumer, provider=provider, provider_payout=100.0
    )
    # Attach a CAPTURED payment row so the webhook can look up the booking
    # by razorpay_payment_id → booking_id.
    payment = Payment(
        booking_id=booking.id,
        razorpay_payment_id="pay_settle_1",
        razorpay_order_id=booking.razorpay_order_id or "order_settle_1",
        amount=Decimal("500.00"),
        platform_fee=Decimal("100.00"),
        provider_settlement=Decimal("400.00"),
        status=PaymentRecordStatus.CAPTURED,
        method=PaymentRecordMethod.UPI,
    )
    db_session.add(payment)
    await db_session.flush()

    assert booking.settled_at is None
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 0

    await PaymentService(db_session).handle_webhook(
        event="settlement.processed",
        payload={
            "settlement": {
                "entity": {
                    "id": "setl_test_1",
                    "payment_ids": ["pay_settle_1"],
                }
            }
        },
    )

    await db_session.refresh(booking)
    assert booking.settled_at is not None
    # Wallet now sees the credit.
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 10000


@pytest.mark.asyncio
async def test_settlement_processed_is_idempotent(db_session):
    """Redelivered webhook preserves the original settled_at instant —
    Razorpay retries, and we shouldn't rewrite the settlement timestamp.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_online_booking_unsettled(db_session, consumer=consumer, provider=provider)
    payment = Payment(
        booking_id=booking.id,
        razorpay_payment_id="pay_settle_idem",
        razorpay_order_id="order_settle_idem",
        amount=Decimal("500.00"),
        platform_fee=Decimal("100.00"),
        provider_settlement=Decimal("400.00"),
        status=PaymentRecordStatus.CAPTURED,
        method=PaymentRecordMethod.UPI,
    )
    db_session.add(payment)
    await db_session.flush()

    payload = {"settlement": {"entity": {"id": "setl_idem", "payment_ids": ["pay_settle_idem"]}}}
    svc = PaymentService(db_session)
    await svc.handle_webhook("settlement.processed", payload)
    await db_session.refresh(booking)
    first_at = booking.settled_at
    assert first_at is not None

    await svc.handle_webhook("settlement.processed", payload)
    await db_session.refresh(booking)
    assert booking.settled_at == first_at


@pytest.mark.asyncio
async def test_settlement_processed_acks_unknown_payment_ids(db_session):
    """Payment ids we don't recognise are logged + acked (SVC-176 policy).
    Doesn't crash, doesn't retry-storm Razorpay.
    """
    await PaymentService(db_session).handle_webhook(
        event="settlement.processed",
        payload={
            "settlement": {
                "entity": {
                    "id": "setl_unknown",
                    "payment_ids": ["pay_never_seen_here"],
                }
            }
        },
    )
    # No raise, no side effects worth asserting beyond "didn't blow up".


@pytest.mark.asyncio
async def test_settlement_processed_missing_payment_ids_is_noop(db_session):
    """Payload without `payment_ids` is a no-op (defensive)."""
    await PaymentService(db_session).handle_webhook(
        event="settlement.processed",
        payload={"settlement": {"entity": {"id": "setl_empty"}}},
    )


# ---------------------------------------------------------------------------
# AUTO_SETTLE_ON_COMPLETION dev-mode escape hatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_auto_stamps_settled_when_flag_on(db_session):
    """Beta default (AUTO_SETTLE_ON_COMPLETION=True): booking transition
    to COMPLETED stamps settled_at inline so the wallet credit counts
    immediately. Keeps the tester runbook working without the webhook.
    """
    from app.services import booking_service as booking_service_mod

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    suffix = uuid.uuid4().hex[:8]
    cat = Category(name=f"C {suffix}", slug=f"c-{suffix}", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name=f"S {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(svc)
    addr = Address(user_id=consumer.id, label="home", full_address="Addr")
    db_session.add(addr)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.IN_PROGRESS,
        payment_status=PaymentStatus.PAID,
        payment_method=PaymentMethod.ONLINE,
        provider_payout=80.0,
    )

    assert booking_service_mod.settings.AUTO_SETTLE_ON_COMPLETION is True

    await BookingService(db_session).transition_status(
        booking_id=booking.id,
        new_status=BookingStatus.COMPLETED,
        changed_by=provider.id,
    )

    await db_session.refresh(booking)
    assert booking.settled_at is not None


@pytest.mark.asyncio
async def test_completion_leaves_unsettled_when_flag_off(db_session, monkeypatch):
    """Prod behaviour (AUTO_SETTLE_ON_COMPLETION=False): completion writes
    the wallet credit but leaves settled_at NULL. The Razorpay
    settlement webhook is the only path that stamps it.
    """
    from app.services import booking_service as booking_service_mod

    monkeypatch.setattr(booking_service_mod.settings, "AUTO_SETTLE_ON_COMPLETION", False)

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    suffix = uuid.uuid4().hex[:8]
    cat = Category(name=f"C {suffix}", slug=f"c-{suffix}", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name=f"S {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(svc)
    addr = Address(user_id=consumer.id, label="home", full_address="Addr")
    db_session.add(addr)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.IN_PROGRESS,
        payment_status=PaymentStatus.PAID,
        payment_method=PaymentMethod.ONLINE,
        provider_payout=80.0,
    )

    await BookingService(db_session).transition_status(
        booking_id=booking.id,
        new_status=BookingStatus.COMPLETED,
        changed_by=provider.id,
    )

    await db_session.refresh(booking)
    # Credit was written, but no auto-stamp — settled_at stays NULL.
    assert booking.settled_at is None
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 0
    # Sanity: credit IS in the ledger, it just doesn't count.
    r = await db_session.execute(
        select(ProviderLedgerEntry).where(ProviderLedgerEntry.provider_id == provider.id)
    )
    assert len(list(r.scalars().all())) == 1
    _ = AsyncMock, patch  # touch imports
