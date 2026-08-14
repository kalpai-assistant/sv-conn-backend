"""
Refund lifecycle tests (SVC-178).

Covers:
- Cancellation policy (§7 + Q3 resolved to flat ₹49, 100% to provider):
  - Pre-acceptance cancel: full refund, no fee, no ledger credit.
  - Post-acceptance cancel: refund (captured − ₹49), ₹49 credited to
    provider wallet.
  - Cash bookings: no refund path, no ledger writes.
  - PENDING (unpaid): pure state change, no refund.
- `PaymentService.initiate_refund`:
  - Flips booking.payment_status → REFUND_PROCESSING, stamps refund_id /
    refund_amount_paise on the payment row.
  - Idempotent on the payment row (second call is no-op).
  - Raises when no CAPTURED payment exists.
- Webhook handlers:
  - refund.processed: flips REFUND_PROCESSING → REFUNDED, sets
    refunded_at, updates Payment.status → REFUNDED.
  - refund.failed: flips REFUND_PROCESSING → REFUND_FAILED.
  - Redelivery is idempotent.
  - Unknown refund id is logged and acked (matches SVC-176 policy).
"""

import uuid as _uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.booking import (
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.provider_ledger_entry import LedgerEntryType, ProviderLedgerEntry
from app.services.booking_service import BookingService
from app.services.payment_service import PaymentService
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _seed_booking_with_payment(
    db,
    *,
    consumer,
    provider,
    status,
    payment_method=PaymentMethod.ONLINE,
    payment_status=PaymentStatus.PAID,
    service_amount=500.0,
    discount_amount=0.0,
    razorpay_payment_id="pay_refund_test",
    with_payment=True,
    payment_status_row=PaymentRecordStatus.CAPTURED,
) -> tuple:
    """Booking + optional CAPTURED payment row + all FK deps."""
    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    suffix = _uuid.uuid4().hex[:8]
    cat = Category(name=f"Cat {suffix}", slug=f"cat-{suffix}", is_active=True, sort_order=1)
    db.add(cat)
    await db.flush()
    svc = Service(
        category_id=cat.id,
        name=f"Svc {suffix}",
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
        status=status,
        payment_status=payment_status,
        payment_method=payment_method,
        service_amount=service_amount,
        razorpay_order_id=f"order_{suffix}",
    )
    if discount_amount:
        booking.discount_amount = Decimal(str(discount_amount))
        await db.flush()

    payment = None
    if with_payment:
        payment = Payment(
            booking_id=booking.id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_order_id=booking.razorpay_order_id,
            amount=Decimal(str(service_amount - discount_amount)),
            platform_fee=Decimal(str((service_amount - discount_amount) * 0.2)),
            provider_settlement=Decimal(str((service_amount - discount_amount) * 0.8)),
            status=payment_status_row,
            method=PaymentRecordMethod.UPI,
        )
        db.add(payment)
        await db.flush()
    return booking, payment


async def _entries(db, provider_id):
    r = await db.execute(
        select(ProviderLedgerEntry)
        .where(ProviderLedgerEntry.provider_id == provider_id)
        .order_by(ProviderLedgerEntry.created_at.asc())
    )
    return list(r.scalars().all())


# ---------------------------------------------------------------------------
# initiate_refund
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initiate_refund_flips_state_and_stamps_payment(db_session):
    """Happy path: Razorpay refund created → payment.refund_id set,
    booking.payment_status = REFUND_PROCESSING. `refunded_at` stays NULL
    until the webhook lands.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, payment = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.ACCEPTED,
        service_amount=500.0,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.payment.refund.return_value = {"id": "rfnd_ok"}
        result = await PaymentService(db_session).initiate_refund(
            booking=booking, amount_paise=45100, reason="cancel test"
        )

    assert result.refund_id == "rfnd_ok"
    assert result.refund_amount_paise == 45100
    assert result.refunded_at is None  # webhook hasn't landed
    await db_session.refresh(booking)
    assert booking.payment_status is PaymentStatus.REFUND_PROCESSING


@pytest.mark.asyncio
async def test_initiate_refund_is_idempotent_on_payment_row(db_session):
    """Second call for a payment that already has a refund_id is a no-op
    and does NOT re-hit Razorpay — protects against duplicate cancel
    clicks or webhook redelivery arriving after a manual retry.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, payment = await _seed_booking_with_payment(
        db_session, consumer=consumer, provider=provider, status=BookingStatus.ACCEPTED
    )
    payment.refund_id = "rfnd_already"
    payment.refund_amount_paise = 40000
    await db_session.flush()

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        result = await PaymentService(db_session).initiate_refund(
            booking=booking, amount_paise=40000
        )

    assert result.refund_id == "rfnd_already"
    mock_rzp.payment.refund.assert_not_called()


@pytest.mark.asyncio
async def test_initiate_refund_raises_without_captured_payment(db_session):
    """Refund requires a CAPTURED payment — a booking without one (never
    paid) should raise, not silently succeed.
    """
    from app.exceptions import PaymentError

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, _ = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.ACCEPTED,
        payment_status=PaymentStatus.PENDING,
        with_payment=False,
    )

    with pytest.raises(PaymentError, match="No CAPTURED payment"):
        await PaymentService(db_session).initiate_refund(booking=booking, amount_paise=1000)


# ---------------------------------------------------------------------------
# Cancellation policy (booking cancel wires refund + ledger)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_from_requested_refunds_full_no_fee(db_session):
    """Pre-acceptance cancel: consumer refunded 100%, no fee, no provider
    credit — the provider never blocked out their time.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, _ = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.REQUESTED,
        service_amount=500.0,
    )
    # Un-assign — REQUESTED means no provider yet.
    booking.provider_id = None
    await db_session.flush()

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.payment.refund.return_value = {"id": "rfnd_full"}
        await BookingService(db_session).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.CANCELLED,
            changed_by=consumer.id,
            cancellation_reason="changed my mind",
        )

    # Full refund — 50000 paise for the ₹500 booking.
    call_args = mock_rzp.payment.refund.call_args
    assert call_args.args[1]["amount"] == 50000
    # No provider — no ledger writes possible.
    assert await _entries(db_session, provider.id) == []
    await db_session.refresh(booking)
    assert booking.payment_status is PaymentStatus.REFUND_PROCESSING


@pytest.mark.asyncio
async def test_cancel_from_accepted_deducts_fee_and_credits_provider(db_session):
    """Post-acceptance cancel: consumer refunded (captured − ₹49), fee
    credits provider wallet in full.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, _ = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.ACCEPTED,
        service_amount=500.0,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.payment.refund.return_value = {"id": "rfnd_partial"}
        await BookingService(db_session).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.CANCELLED,
            changed_by=provider.id,
            cancellation_reason="scheduling conflict",
        )

    # 50000 − 4900 = 45100 paise refunded.
    call_args = mock_rzp.payment.refund.call_args
    assert call_args.args[1]["amount"] == 45100
    # ₹49 credited to provider wallet as compensation.
    entries = await _entries(db_session, provider.id)
    assert len(entries) == 1
    assert entries[0].entry_type is LedgerEntryType.BOOKING_COMPLETED
    assert entries[0].amount_paise == 4900


@pytest.mark.asyncio
async def test_cancel_from_en_route_same_policy_as_accepted(db_session):
    """EN_ROUTE cancel triggers the same partial-refund + fee-credit path
    as ACCEPTED — the provider already committed time.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, _ = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.EN_ROUTE,
        service_amount=500.0,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.payment.refund.return_value = {"id": "rfnd_enroute"}
        await BookingService(db_session).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.CANCELLED,
            changed_by=consumer.id,
            cancellation_reason="ran late",
        )

    assert mock_rzp.payment.refund.call_args.args[1]["amount"] == 45100
    assert (await _entries(db_session, provider.id))[0].amount_paise == 4900


@pytest.mark.asyncio
async def test_cancel_cash_booking_does_not_refund(db_session):
    """Cash booking cancels are a pure state change — no online payment
    to refund, no ledger writes on the provider side (SVC-180 will
    handle cash-commission netting separately).
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, _ = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.ACCEPTED,
        payment_method=PaymentMethod.CASH,
        payment_status=PaymentStatus.PENDING,
        with_payment=False,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        await BookingService(db_session).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.CANCELLED,
            changed_by=consumer.id,
            cancellation_reason="not needed",
        )

    mock_rzp.payment.refund.assert_not_called()
    assert await _entries(db_session, provider.id) == []


@pytest.mark.asyncio
async def test_cancel_unpaid_booking_does_not_refund(db_session):
    """Booking that was never paid (payment_status = PENDING) cancels
    without a refund call — there's nothing to refund.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, _ = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.REQUESTED,
        payment_status=PaymentStatus.PENDING,
        with_payment=False,
    )
    booking.provider_id = None
    await db_session.flush()

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        await BookingService(db_session).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.CANCELLED,
            changed_by=consumer.id,
            cancellation_reason="not needed",
        )

    mock_rzp.payment.refund.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_with_discount_caps_fee_at_captured(db_session):
    """Regression: a coupon-heavy booking where captured < ₹49 caps the
    fee at the captured amount — we can't refund a negative number.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    # ₹50 service, ₹30 coupon → captured = ₹20 → fee capped at ₹20, refund = 0.
    booking, _ = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.ACCEPTED,
        service_amount=50.0,
        discount_amount=30.0,
        razorpay_payment_id="pay_captured_lt_fee",
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        await BookingService(db_session).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.CANCELLED,
            changed_by=consumer.id,
            cancellation_reason="test cap",
        )

    # Fee is capped at 2000 paise (₹20 captured), refund = 0 → no Razorpay call.
    mock_rzp.payment.refund.assert_not_called()
    entries = await _entries(db_session, provider.id)
    assert entries[0].amount_paise == 2000


# ---------------------------------------------------------------------------
# Webhook handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_processed_webhook_flips_terminal(db_session):
    """refund.processed → booking REFUNDED, payment REFUNDED, refunded_at set."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, payment = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.CANCELLED,
    )
    payment.refund_id = "rfnd_processed_1"
    payment.refund_amount_paise = 50000
    booking.payment_status = PaymentStatus.REFUND_PROCESSING
    await db_session.flush()

    await PaymentService(db_session).handle_webhook(
        event="refund.processed",
        payload={"refund": {"entity": {"id": "rfnd_processed_1"}}},
    )

    await db_session.refresh(booking)
    await db_session.refresh(payment)
    assert booking.payment_status is PaymentStatus.REFUNDED
    assert payment.status is PaymentRecordStatus.REFUNDED
    assert payment.refunded_at is not None


@pytest.mark.asyncio
async def test_refund_processed_redelivery_is_idempotent(db_session):
    """Second refund.processed for the same refund_id keeps the original
    refunded_at instant — Razorpay retries webhooks, and we must not
    overwrite the recorded terminal timestamp.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, payment = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.CANCELLED,
    )
    payment.refund_id = "rfnd_idem"
    booking.payment_status = PaymentStatus.REFUND_PROCESSING
    await db_session.flush()

    svc = PaymentService(db_session)
    await svc.handle_webhook("refund.processed", {"refund": {"entity": {"id": "rfnd_idem"}}})
    await db_session.refresh(payment)
    first_at = payment.refunded_at

    await svc.handle_webhook("refund.processed", {"refund": {"entity": {"id": "rfnd_idem"}}})
    await db_session.refresh(payment)
    assert payment.refunded_at == first_at


@pytest.mark.asyncio
async def test_refund_failed_flips_to_refund_failed(db_session):
    """refund.failed leaves an admin-actionable state on the booking."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, payment = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.CANCELLED,
    )
    payment.refund_id = "rfnd_will_fail"
    booking.payment_status = PaymentStatus.REFUND_PROCESSING
    await db_session.flush()

    await PaymentService(db_session).handle_webhook(
        "refund.failed", {"refund": {"entity": {"id": "rfnd_will_fail"}}}
    )

    await db_session.refresh(booking)
    await db_session.refresh(payment)
    assert booking.payment_status is PaymentStatus.REFUND_FAILED
    # refunded_at stays null on failure — matches its "terminal success" definition.
    assert payment.refunded_at is None


@pytest.mark.asyncio
async def test_refund_webhook_unknown_id_is_acked(db_session):
    """Unknown refund_id logs + acks (matches SVC-176 policy). Razorpay
    would otherwise retry forever on an id we simply don't know.
    """
    await PaymentService(db_session).handle_webhook(
        "refund.processed",
        {"refund": {"entity": {"id": "rfnd_never_saw_this"}}},
    )
    # No raise. No side effects worth asserting.


@pytest.mark.asyncio
async def test_refund_webhook_missing_id_is_noop(db_session):
    """Payload with no refund.entity.id: log + ack, don't crash."""
    await PaymentService(db_session).handle_webhook(
        "refund.processed",
        {"refund": {"entity": {}}},
    )
