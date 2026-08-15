"""
Cash-commission netting + settlement statement tests (SVC-180).

Covers:
- `process_cash_payment` writes a CASH_COMMISSION debit on cash booking completion.
- `create_provider_payout` auto-nets cash commission via the wallet:
  - Wallet positive → payout the wallet (< booking sum when commission netted).
  - Wallet negative → no RazorpayX call, statement generated with owed amount.
- Settlement statements are generated on every payout attempt (paid or owed).
- Statement columns reflect the ledger window since the last statement.
- `estimate_pending_payout` returns netted amount (0 when provider owes).
"""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.booking import Booking, BookingStatus, PaymentMethod, PaymentStatus
from app.models.provider_ledger_entry import (
    LedgerEntryType,
    ProviderLedgerEntry,
)
from app.models.provider_profile import KYCStatus, PayoutMethod, ProviderProfile
from app.models.settlement_statement import SettlementStatement
from app.services import ledger_service
from app.services.payout_service import PayoutService
from app.utils.encryption import encrypt
from app.utils.money import to_paise
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


# Reuse the same fixture pattern as test_provider_ledger.py — the payout
# audit helpers commit through AsyncSessionLocal against the app DB, not the
# test DB, so stub them here.
@pytest.fixture(autouse=True)
def _stub_payout_audit_io():
    fake_audit_id = uuid.uuid4()
    with (
        patch.object(
            PayoutService,
            "_record_attempting_audit",
            new_callable=AsyncMock,
            return_value=fake_audit_id,
        ),
        patch.object(PayoutService, "_resolve_audit", new_callable=AsyncMock),
    ):
        yield fake_audit_id


@pytest.fixture(autouse=True)
def _stub_payout_push():
    from app.tasks.notification_tasks import send_payout_notification

    with patch.object(send_payout_notification, "delay") as delay:
        yield delay


async def _seed_online_booking(db, *, consumer, provider, provider_payout=80.0):
    """Fresh online COMPLETED+PAID booking with the ledger credit already
    written (mirrors what test_payouts.py's _booking_for now does)."""
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
    await ledger_service.credit_booking_completed(
        db,
        provider_id=provider.id,
        booking_id=booking.id,
        amount_paise=to_paise(booking.provider_payout + booking.tip_amount),
    )
    return booking


async def _seed_cash_booking(
    db, *, consumer, provider, platform_commission=20.0, service_amount=100.0
):
    """Fresh cash COMPLETED+PAID booking with the CASH_COMMISSION debit
    already written (mirrors what process_cash_payment does in production).
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
        base_price=service_amount,
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
        payment_method=PaymentMethod.CASH,
        service_amount=service_amount,
        platform_commission=platform_commission,
        provider_payout=service_amount - platform_commission,
    )
    entry = ProviderLedgerEntry(
        provider_id=provider.id,
        amount_paise=-to_paise(booking.platform_commission),
        entry_type=LedgerEntryType.CASH_COMMISSION,
        reference_type=None,
        reference_id=booking.id,
        note="test seed",
    )
    db.add(entry)
    await db.flush()
    return booking


async def _seed_provider(db, *, kyc=KYCStatus.VERIFIED):
    provider = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(
        user_id=provider.id,
        payout_vpa_encrypted=encrypt("provider@upi"),
        payout_method=PayoutMethod.UPI,
        kyc_status=kyc,
    )
    db.add(profile)
    await db.flush()
    return provider


# ---------------------------------------------------------------------------
# Cash-commission debit at cash booking completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cash_booking_write_debits_wallet(db_session):
    """Baseline: a cash booking's CASH_COMMISSION debit lands on the ledger
    with a negative amount equal to platform_commission * 100.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await _seed_provider(db_session)
    await _seed_cash_booking(
        db_session, consumer=consumer, provider=provider, platform_commission=20.0
    )

    r = await db_session.execute(
        select(ProviderLedgerEntry)
        .where(ProviderLedgerEntry.provider_id == provider.id)
        .where(ProviderLedgerEntry.entry_type == LedgerEntryType.CASH_COMMISSION)
    )
    entries = list(r.scalars().all())
    assert len(entries) == 1
    assert entries[0].amount_paise == -2000


# ---------------------------------------------------------------------------
# Auto-net in create_provider_payout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payout_nets_cash_commission_against_online_earnings(db_session):
    """Online ₹80 credit + cash commission ₹20 debit → wallet ₹60 → payout
    ₹60 (not ₹80). Statement records the split.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await _seed_provider(db_session)
    admin = await UserFactory.create(db_session, role="admin")

    await _seed_online_booking(
        db_session, consumer=consumer, provider=provider, provider_payout=80.0
    )
    await _seed_cash_booking(
        db_session, consumer=consumer, provider=provider, platform_commission=20.0
    )

    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 6000

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_net", "status": "processing"}
        result = await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    # Wire amount = netted total (₹60), not booking sum (₹80).
    _, call_kwargs = mock_payout.call_args
    assert call_kwargs["amount_paise"] == 6000
    assert result["amount"] == 6000

    # Statement generated with the split.
    stmt = (
        await db_session.execute(
            select(SettlementStatement).where(SettlementStatement.provider_id == provider.id)
        )
    ).scalar_one()
    assert stmt.gross_credit_paise == 8000
    assert stmt.cash_commission_deducted_paise == 2000
    assert stmt.net_paid_paise == 6000
    assert stmt.platform_balance_owed_paise == 0

    # Wallet at zero after the payout debit.
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == 0


@pytest.mark.asyncio
async def test_payout_refused_when_cash_commission_exceeds_earnings(db_session):
    """Wallet ≤ 0 → no RazorpayX call. Statement generated showing amount
    owed to platform. Raises PaymentError so the admin sees it.
    """
    from app.exceptions import PaymentError

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await _seed_provider(db_session)
    admin = await UserFactory.create(db_session, role="admin")

    # Small online (₹10), large cash commission (₹50). Wallet = -₹40.
    await _seed_online_booking(
        db_session, consumer=consumer, provider=provider, provider_payout=10.0
    )
    await _seed_cash_booking(
        db_session, consumer=consumer, provider=provider, platform_commission=50.0
    )
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == -4000

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        with pytest.raises(PaymentError, match="owes platform"):
            await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    mock_payout.assert_not_called()

    # Statement still generated — the invoice for the owed amount.
    stmt = (
        await db_session.execute(
            select(SettlementStatement).where(SettlementStatement.provider_id == provider.id)
        )
    ).scalar_one()
    assert stmt.payout_audit_id is None
    assert stmt.net_paid_paise == 0
    assert stmt.platform_balance_owed_paise == 4000

    # Wallet unchanged — nothing paid, nothing debited.
    assert await ledger_service.wallet_balance_paise(db_session, provider.id) == -4000


@pytest.mark.asyncio
async def test_payout_happy_path_generates_statement_no_deduction(db_session):
    """Provider with no cash bookings — statement shows gross = net, zero
    deduction. Regression against the statement rendering deduction when
    there is none.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await _seed_provider(db_session)
    admin = await UserFactory.create(db_session, role="admin")

    await _seed_online_booking(
        db_session, consumer=consumer, provider=provider, provider_payout=100.0
    )

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_clean", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    stmt = (
        await db_session.execute(
            select(SettlementStatement).where(SettlementStatement.provider_id == provider.id)
        )
    ).scalar_one()
    assert stmt.gross_credit_paise == 10000
    assert stmt.cash_commission_deducted_paise == 0
    assert stmt.net_paid_paise == 10000


# ---------------------------------------------------------------------------
# Statement windowing across cycles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_statement_windows_from_previous(db_session):
    """A second payout for the same provider windows its statement fields
    from the first statement's created_at. Entries in the first period
    don't get counted in the second.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await _seed_provider(db_session)
    admin = await UserFactory.create(db_session, role="admin")

    # Cycle 1: ₹80 online, ₹20 cash commission. Wallet ₹60.
    await _seed_online_booking(
        db_session, consumer=consumer, provider=provider, provider_payout=80.0
    )
    await _seed_cash_booking(
        db_session, consumer=consumer, provider=provider, platform_commission=20.0
    )
    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_c1", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    # Cycle 2: another ₹80 online. Cash commission cleared, wallet ₹80.
    await _seed_online_booking(
        db_session, consumer=consumer, provider=provider, provider_payout=80.0
    )
    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_c2", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    statements = list(
        (
            await db_session.execute(
                select(SettlementStatement)
                .where(SettlementStatement.provider_id == provider.id)
                .order_by(SettlementStatement.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(statements) == 2
    # Cycle 2's window excludes cycle 1's entries — no cash commission in period 2.
    assert statements[1].cash_commission_deducted_paise == 0
    assert statements[1].gross_credit_paise == 8000
    assert statements[1].net_paid_paise == 8000


# ---------------------------------------------------------------------------
# estimate_pending_payout reflects the netted amount
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estimate_returns_wallet_balance_not_booking_sum(db_session):
    """Endpoint's estimate is the netted amount so the admin sees the real
    payout total, not the pre-netting booking sum.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await _seed_provider(db_session)

    await _seed_online_booking(
        db_session, consumer=consumer, provider=provider, provider_payout=80.0
    )
    await _seed_cash_booking(
        db_session, consumer=consumer, provider=provider, platform_commission=20.0
    )

    est = await PayoutService(db_session).estimate_pending_payout(provider.id)
    assert est["amount_paise"] == 6000  # netted, not 8000
    assert est["booking_count"] == 1  # only the online booking is PENDING


@pytest.mark.asyncio
async def test_estimate_returns_zero_when_provider_owes_platform(db_session):
    """Wallet ≤ 0 shows amount_paise = 0 (not negative), matching what the
    UI expects and avoiding a scary-looking negative on the admin screen.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await _seed_provider(db_session)

    await _seed_online_booking(
        db_session, consumer=consumer, provider=provider, provider_payout=10.0
    )
    await _seed_cash_booking(
        db_session, consumer=consumer, provider=provider, platform_commission=50.0
    )

    est = await PayoutService(db_session).estimate_pending_payout(provider.id)
    assert est["amount_paise"] == 0
