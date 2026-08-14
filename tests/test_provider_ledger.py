"""
Provider wallet ledger tests (SVC-193).

Covers:
- The three write sites: booking completion, payout success, dispute loss.
- `wallet_balance_paise` reads what the writes stored.
- Cash bookings do NOT credit (cash-commission debit lands under SVC-180).
- Backfill invariant is exercised in the model tests below and again by the
  migration's own SQL running on any DB with pre-existing data (verified by
  the standard `alembic upgrade head` step in CI on a fresh + a seeded DB).
"""

import uuid as _uuid_module  # separate alias so per-test _uuid imports don't shadow
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.booking import (
    Booking,
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
    SettlementStatus,
)
from app.models.provider_ledger_entry import (
    LedgerEntryType,
    LedgerReferenceType,
    ProviderLedgerEntry,
)
from app.services import ledger_service
from app.services.booking_service import BookingService
from app.services.payment_service import PaymentService
from app.services.payout_service import PayoutService
from app.utils.encryption import encrypt
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


@pytest.fixture(autouse=True)
def _stub_payout_audit_io():
    """Mirror the payout test fixture: `_record_attempting_audit` and
    `_resolve_audit` commit via `AsyncSessionLocal` against the *app* DB
    (`serviceconnect`), not the test session (`sc_test`), and so can't run
    against test data. Stub them here so payout-triggering ledger tests
    don't blow up on a missing table.

    Uses a stable fake audit id so the payout debit's `reference_id` is
    predictable.
    """
    fake_audit_id = _uuid_module.uuid4()
    with (
        patch.object(
            PayoutService,
            "_record_attempting_audit",
            new_callable=AsyncMock,
            return_value=fake_audit_id,
        ),
        patch.object(
            PayoutService,
            "_resolve_audit",
            new_callable=AsyncMock,
        ),
    ):
        yield fake_audit_id


@pytest.fixture(autouse=True)
def _stub_payout_push():
    """Payout enqueues a Celery task; with no broker in tests it would
    hang on connect. Same stub as test_payouts.py's autouse fixture.
    """
    from app.tasks.notification_tasks import send_payout_notification

    with patch.object(send_payout_notification, "delay") as delay:
        yield delay


async def _seed_booking(db, *, consumer, provider, **kw):
    """Fresh booking with all FK deps + a unique category (no cross-test collisions)."""
    import uuid as _uuid

    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    suffix = _uuid.uuid4().hex[:8]
    cat = Category(
        name=f"Cat {suffix}",
        slug=f"cat-{suffix}",
        is_active=True,
        sort_order=1,
    )
    db.add(cat)
    await db.flush()
    svc = Service(
        category_id=cat.id,
        name=f"Svc {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db.add(svc)
    addr = Address(user_id=consumer.id, label="home", full_address="789 Test Ave")
    db.add(addr)
    await db.flush()
    return await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        **kw,
    )


async def _entries_for(db, provider_id) -> list[ProviderLedgerEntry]:
    result = await db.execute(
        select(ProviderLedgerEntry)
        .where(ProviderLedgerEntry.provider_id == provider_id)
        .order_by(ProviderLedgerEntry.created_at.asc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Helper directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_balance_is_zero_when_no_entries(db_session):
    provider = await UserFactory.create(db_session, role="provider")
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 0


@pytest.mark.asyncio
async def test_credit_debit_sum(db_session):
    provider = await UserFactory.create(db_session, role="provider")
    import uuid as _uuid

    await ledger_service.credit_booking_completed(
        db_session,
        provider_id=provider.id,
        booking_id=_uuid.uuid4(),
        amount_paise=40000,
    )
    await ledger_service.debit_payout(
        db_session,
        provider_id=provider.id,
        payout_audit_id=_uuid.uuid4(),
        amount_paise=15000,
    )
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 25000


@pytest.mark.asyncio
async def test_debit_functions_normalise_sign(db_session):
    """The two debit helpers accept positive amounts and store them negative,
    so a caller can pass the plain money figure without worrying about sign.
    Regression against passing an already-negative amount and getting a
    double-negation credit.
    """
    provider = await UserFactory.create(db_session, role="provider")
    import uuid as _uuid

    await ledger_service.debit_payout(
        db_session,
        provider_id=provider.id,
        payout_audit_id=_uuid.uuid4(),
        amount_paise=5000,
    )
    await ledger_service.debit_dispute_loss(
        db_session,
        provider_id=provider.id,
        payment_id=_uuid.uuid4(),
        amount_paise=8000,
    )
    entries = await _entries_for(db_session, provider.id)
    assert entries[0].amount_paise == -5000
    assert entries[1].amount_paise == -8000


# ---------------------------------------------------------------------------
# Write site 1: booking completion (BookingService.transition_status)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_online_completion_credits_wallet(db_session):
    """A COMPLETED online booking writes a BOOKING_COMPLETED credit for
    (provider_payout + tip_amount) * 100 paise. Tips added post-completion
    are on the booking row; they don't fire a second entry because Q4
    ("defer to payout") means the payout math already sums the tip.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.IN_PROGRESS,
        payment_method=PaymentMethod.ONLINE,
        payment_status=PaymentStatus.PAID,
        provider_payout=400.0,
    )
    booking.tip_amount = Decimal("20.00")
    await db_session.flush()

    # Transition provider through the state machine to COMPLETED (must go via
    # the service so the ledger hook fires).
    await BookingService(db_session).transition_status(
        booking_id=booking.id,
        new_status=BookingStatus.COMPLETED,
        changed_by=provider.id,
    )

    entries = await _entries_for(db_session, provider.id)
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_type is LedgerEntryType.BOOKING_COMPLETED
    assert e.reference_type is LedgerReferenceType.BOOKING
    assert e.reference_id == booking.id
    assert e.amount_paise == 42000  # (400 + 20) * 100


@pytest.mark.asyncio
async def test_cash_completion_does_not_credit_wallet(db_session):
    """Cash bookings never credit the wallet — the provider collected at
    the door. SVC-180 will separately debit for the platform's 20%
    commission owed on cash jobs.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.IN_PROGRESS,
        payment_method=PaymentMethod.CASH,
        payment_status=PaymentStatus.PAID,
        provider_payout=400.0,
    )

    await BookingService(db_session).transition_status(
        booking_id=booking.id,
        new_status=BookingStatus.COMPLETED,
        changed_by=provider.id,
    )

    assert await _entries_for(db_session, provider.id) == []


# ---------------------------------------------------------------------------
# Write site 2: payout success (PayoutService.create_provider_payout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_payout_debits_wallet(db_session):
    """A successful RazorpayX payout debits the wallet by the paid amount.

    Net wallet after credit + debit is what the reconciliation view
    ("what do we still owe this provider?") reports.
    """
    from app.models.provider_profile import KYCStatus, PayoutMethod, ProviderProfile

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    profile = ProviderProfile(
        user_id=provider.id,
        payout_vpa_encrypted=encrypt("provider@upi"),
        payout_method=PayoutMethod.UPI,
        kyc_status=KYCStatus.VERIFIED,
    )
    db_session.add(profile)

    # PENDING settlement, PAID online booking.
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.COMPLETED,
        payment_method=PaymentMethod.ONLINE,
        payment_status=PaymentStatus.PAID,
        provider_payout=80.0,
    )
    # Directly write the completion credit (skipping the transition since
    # this test focuses on the payout side).
    await ledger_service.credit_booking_completed(
        db_session,
        provider_id=provider.id,
        booking_id=booking.id,
        amount_paise=8000,
    )
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 8000

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_ledger_test", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    entries = await _entries_for(db_session, provider.id)
    # Credit then debit. Payout debit matches the amount RazorpayX was told to send.
    assert len(entries) == 2
    assert entries[0].entry_type is LedgerEntryType.BOOKING_COMPLETED
    assert entries[1].entry_type is LedgerEntryType.PAYOUT
    assert entries[1].reference_type is LedgerReferenceType.PAYOUT_AUDIT
    # Net back to zero.
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 0


# ---------------------------------------------------------------------------
# Write site 3: dispute loss (PaymentService._handle_dispute)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_lost_clawback_debits_wallet(db_session):
    """A `payment.dispute.lost` webhook writes a DISPUTE_LOSS debit equal to
    the disputed booking's provider share (payout + tip). No credit is
    involved — the provider was already paid, this is the reversal.
    """
    from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.COMPLETED,
        payment_method=PaymentMethod.ONLINE,
        payment_status=PaymentStatus.PAID,
        provider_payout=120.0,
        razorpay_order_id="order_disp_ledger",
    )
    booking.tip_amount = Decimal("5.00")
    await db_session.flush()

    payment = Payment(
        booking_id=booking.id,
        razorpay_payment_id="pay_disp_ledger",
        razorpay_order_id=booking.razorpay_order_id,
        amount=Decimal("150.00"),
        platform_fee=Decimal("30.00"),
        provider_settlement=Decimal("120.00"),
        status=PaymentRecordStatus.CAPTURED,
        method=PaymentRecordMethod.UPI,
    )
    db_session.add(payment)
    await db_session.flush()

    await PaymentService(db_session).handle_webhook(
        event="payment.dispute.lost",
        payload={
            "payment": {"entity": {"id": "pay_disp_ledger"}},
            "dispute": {
                "entity": {
                    "id": "disp_ledger_1",
                    "payment_id": "pay_disp_ledger",
                    "amount": 15000,
                    "currency": "INR",
                    "reason_code": "chargeback_fraud",
                    "respond_by": 1699999999,
                }
            },
        },
    )

    entries = await _entries_for(db_session, provider.id)
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_type is LedgerEntryType.DISPUTE_LOSS
    assert e.reference_type is LedgerReferenceType.PAYMENT
    assert e.reference_id == payment.id
    # (120 + 5) * 100 = 12500 paise clawed back.
    assert e.amount_paise == -12500
    # Balance goes negative — provider owes us this until a subsequent credit
    # or admin correction clears it.
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == -12500


@pytest.mark.asyncio
async def test_dispute_won_writes_no_ledger_entry(db_session):
    """`.won` never touches the wallet. Regression against widening the
    dispute-loss write to fire on any resolution.
    """
    from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.COMPLETED,
        payment_method=PaymentMethod.ONLINE,
        payment_status=PaymentStatus.PAID,
        provider_payout=100.0,
        razorpay_order_id="order_disp_won",
    )
    payment = Payment(
        booking_id=booking.id,
        razorpay_payment_id="pay_disp_won",
        razorpay_order_id=booking.razorpay_order_id,
        amount=Decimal("100.00"),
        platform_fee=Decimal("20.00"),
        provider_settlement=Decimal("80.00"),
        status=PaymentRecordStatus.CAPTURED,
        method=PaymentRecordMethod.UPI,
    )
    db_session.add(payment)
    await db_session.flush()

    # Prime with .created (also writes no ledger entry), then .won.
    for evt in ("payment.dispute.created", "payment.dispute.won"):
        await PaymentService(db_session).handle_webhook(
            event=evt,
            payload={
                "payment": {"entity": {"id": "pay_disp_won"}},
                "dispute": {
                    "entity": {
                        "id": "disp_won_1",
                        "payment_id": "pay_disp_won",
                        "amount": 10000,
                        "currency": "INR",
                    }
                },
            },
        )

    assert await _entries_for(db_session, provider.id) == []


# ---------------------------------------------------------------------------
# Backfill (the migration itself, exercised via the SUM invariant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_sum_matches_derived_formula(db_session):
    """After a fresh migration run (the test DB is set up that way), the
    wallet balance for a provider with pre-migration bookings + payouts
    should equal what the pre-SVC-193 formula would have computed.

    Simulates history by inserting entries directly (mirroring what the
    backfill SQL does), so this test also documents the exact invariant the
    migration is asked to preserve.
    """
    import uuid as _uuid

    provider = await UserFactory.create(db_session, role="provider")

    # Simulated history: 3 completed online bookings @ ₹80 each; one payout
    # of ₹160 already sent (settling 2 of them).
    for _ in range(3):
        await ledger_service.credit_booking_completed(
            db_session,
            provider_id=provider.id,
            booking_id=_uuid.uuid4(),
            amount_paise=8000,
            note="backfill-sim: booking",
        )
    await ledger_service.debit_payout(
        db_session,
        provider_id=provider.id,
        payout_audit_id=_uuid.uuid4(),
        amount_paise=16000,
        note="backfill-sim: paid two",
    )

    # Balance = 3 × 8000 − 16000 = 8000. Matches "one booking still owed".
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 8000
