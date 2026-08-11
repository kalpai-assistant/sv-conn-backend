"""
Dispute / chargeback webhook tests (SVC-134).

Covers: the payment.dispute.created flag, webhook redelivery idempotency, the
deliberate absence of any automated state change on .won / .lost, the malformed
and unknown-payment paths, the Payments webhook endpoint + signature, and the
admin bookings list's disputed filter/badge.
"""

import hashlib
import hmac
import json
import uuid

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.exceptions import PaymentError
from app.models.address import Address
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.category import Category
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.service import Service
from app.routers import payments as payments_router
from app.services.payment_service import PaymentService
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

settings = get_settings()

_RZP_SECRET = "razorpay_test_secret"


async def _booking_with_payment(db_session, payment_id: str, **payment_kwargs) -> Payment:
    """A captured booking plus its Payment ledger row, with all FK dependencies.

    A unique category/service per call keeps the category name/slug unique
    constraints satisfied when a test creates more than one booking.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")

    suffix = uuid.uuid4().hex[:8]
    category = Category(
        name=f"Home Repair {suffix}", slug=f"home-repair-{suffix}", is_active=True, sort_order=1
    )
    db_session.add(category)
    await db_session.flush()

    service = Service(
        category_id=category.id,
        name=f"AC Service {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)

    address = Address(user_id=consumer.id, label="home", full_address="789 Test Ave")
    db_session.add(address)
    await db_session.flush()

    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        razorpay_order_id=f"order_{suffix}",
    )

    payment = Payment(
        booking_id=booking.id,
        razorpay_payment_id=payment_id,
        razorpay_order_id=booking.razorpay_order_id,
        amount=100.0,
        platform_fee=20.0,
        provider_settlement=80.0,
        status=PaymentRecordStatus.CAPTURED,
        method=PaymentRecordMethod.CARD,
        **payment_kwargs,
    )
    db_session.add(payment)
    await db_session.flush()
    return payment


def _dispute_payload(payment_id: str, dispute_id: str = "disp_test_1") -> dict:
    """A Razorpay dispute payload: the payment id lives at dispute.entity.payment_id."""
    return {
        "payment": {"entity": {"id": payment_id, "order_id": "order_x"}},
        "dispute": {
            "entity": {
                "id": dispute_id,
                "payment_id": payment_id,
                "amount": 10000,
                "currency": "INR",
                "reason_code": "chargeback_fraud",
                "respond_by": 1699999999,
                "status": "open",
                "phase": "chargeback",
            }
        },
    }


def _signed_body(payload: dict, secret: str) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, sig


# ---------------------------------------------------------------------------
# payment.dispute.created — flag the payment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_created_stamps_disputed_at(db_session):
    """A chargeback stamps disputed_at on the payment it names."""
    payment = await _booking_with_payment(db_session, "pay_dispute_1")
    assert payment.disputed_at is None

    await PaymentService(db_session).handle_webhook(
        event="payment.dispute.created",
        payload=_dispute_payload("pay_dispute_1"),
    )

    await db_session.refresh(payment)
    assert payment.disputed_at is not None


@pytest.mark.asyncio
async def test_dispute_created_is_idempotent(db_session):
    """Razorpay retries webhooks — a redelivery keeps the original dispute time."""
    payment = await _booking_with_payment(db_session, "pay_dispute_2")

    svc = PaymentService(db_session)
    await svc.handle_webhook("payment.dispute.created", _dispute_payload("pay_dispute_2"))
    await db_session.refresh(payment)
    first_stamp = payment.disputed_at

    await svc.handle_webhook("payment.dispute.created", _dispute_payload("pay_dispute_2"))
    await db_session.refresh(payment)

    assert payment.disputed_at == first_stamp


@pytest.mark.asyncio
async def test_dispute_does_not_change_payment_or_booking_state(db_session):
    """Beta AC: a dispute flags only — no refund, no settlement move (admin decides)."""
    payment = await _booking_with_payment(db_session, "pay_dispute_3")
    booking = await db_session.get(Booking, payment.booking_id)
    before = (payment.status, booking.payment_status, booking.settlement_status)

    await PaymentService(db_session).handle_webhook(
        "payment.dispute.created", _dispute_payload("pay_dispute_3")
    )

    await db_session.refresh(payment)
    await db_session.refresh(booking)
    assert (payment.status, booking.payment_status, booking.settlement_status) == before


# ---------------------------------------------------------------------------
# .won / .lost — recorded, never acted on automatically
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["payment.dispute.won", "payment.dispute.lost"])
@pytest.mark.asyncio
async def test_dispute_resolution_makes_no_automated_state_change(db_session, event):
    """Resolution events are logged only; an admin handles the money for beta."""
    payment = await _booking_with_payment(db_session, f"pay_res_{event[-4:]}")
    await PaymentService(db_session).handle_webhook(
        "payment.dispute.created", _dispute_payload(payment.razorpay_payment_id)
    )
    await db_session.refresh(payment)
    stamped = payment.disputed_at

    booking = await db_session.get(Booking, payment.booking_id)
    before = (payment.status, booking.payment_status, booking.settlement_status)

    await PaymentService(db_session).handle_webhook(
        event, _dispute_payload(payment.razorpay_payment_id)
    )

    await db_session.refresh(payment)
    await db_session.refresh(booking)
    # Nothing moves — not even the dispute flag, which stays as the audit trail.
    assert payment.disputed_at == stamped
    assert (payment.status, booking.payment_status, booking.settlement_status) == before


# ---------------------------------------------------------------------------
# Malformed / unmappable payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_missing_payment_id_raises(db_session):
    """A dispute with no payment id can't be mapped → raise rather than 200."""
    with pytest.raises(PaymentError):
        await PaymentService(db_session).handle_webhook(
            event="payment.dispute.created",
            payload={"dispute": {"entity": {"id": "disp_no_payment"}}},
        )


@pytest.mark.asyncio
async def test_dispute_for_unknown_payment_is_acked(db_session):
    """An unknown payment is an anomaly a retry can't fix — log and acknowledge."""
    await PaymentService(db_session).handle_webhook(
        event="payment.dispute.created",
        payload=_dispute_payload("pay_never_seen"),
    )  # must not raise

    assert (
        await db_session.execute(
            select(Payment).where(Payment.razorpay_payment_id == "pay_never_seen")
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_unhandled_dispute_subevent_is_noop(db_session):
    """Dispute sub-events we don't handle must not raise or flag anything."""
    payment = await _booking_with_payment(db_session, "pay_dispute_4")

    await PaymentService(db_session).handle_webhook(
        event="payment.dispute.under_review",
        payload=_dispute_payload("pay_dispute_4"),
    )

    await db_session.refresh(payment)
    assert payment.disputed_at is None


# ---------------------------------------------------------------------------
# Payments webhook endpoint (disputes share the Payments secret, not RazorpayX's)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_webhook_endpoint_flags_payment(client, db_session):
    """A correctly-signed dispute on /payments/webhook flags the payment."""
    payment = await _booking_with_payment(db_session, "pay_signed")
    body = {"event": "payment.dispute.created", "payload": _dispute_payload("pay_signed")}
    raw, sig = _signed_body(body, _RZP_SECRET)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(payments_router.settings, "RAZORPAY_WEBHOOK_SECRET", _RZP_SECRET)
        resp = await client.post(
            f"{settings.API_V1_PREFIX}/payments/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
        )
    assert resp.status_code == 200

    await db_session.refresh(payment)
    assert payment.disputed_at is not None


@pytest.mark.asyncio
async def test_dispute_webhook_malformed_payload_is_rejected(client, db_session):
    """A malformed dispute surfaces as a 4xx (402, per PaymentError), not a 200.

    Razorpay retries any non-2xx, and a payload with no payment id can never
    succeed — so this documents that a broken integration stays loud rather than
    being silently acknowledged. Mirrors the payout webhook's missing-id behaviour.
    """
    body = {
        "event": "payment.dispute.created",
        "payload": {"dispute": {"entity": {"id": "disp_malformed"}}},
    }
    raw, sig = _signed_body(body, _RZP_SECRET)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(payments_router.settings, "RAZORPAY_WEBHOOK_SECRET", _RZP_SECRET)
        resp = await client.post(
            f"{settings.API_V1_PREFIX}/payments/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
        )
    assert resp.status_code == 402
    assert 400 <= resp.status_code < 500


@pytest.mark.asyncio
async def test_dispute_webhook_rejects_invalid_signature(client, db_session):
    """A bad signature is rejected (400) and flags nothing."""
    payment = await _booking_with_payment(db_session, "pay_badsig")
    body = {"event": "payment.dispute.created", "payload": _dispute_payload("pay_badsig")}
    raw, _ = _signed_body(body, _RZP_SECRET)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(payments_router.settings, "RAZORPAY_WEBHOOK_SECRET", _RZP_SECRET)
        resp = await client.post(
            f"{settings.API_V1_PREFIX}/payments/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": "wrong"},
        )
    assert resp.status_code == 400

    await db_session.refresh(payment)
    assert payment.disputed_at is None


# ---------------------------------------------------------------------------
# Admin visibility — the badge and its filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_bookings_exposes_and_filters_disputed(client, db_session, auth_headers):
    """Admin list badges disputed bookings and filters on them both ways."""
    disputed = await _booking_with_payment(db_session, "pay_admin_disputed")
    clean = await _booking_with_payment(db_session, "pay_admin_clean")
    await PaymentService(db_session).handle_webhook(
        "payment.dispute.created", _dispute_payload("pay_admin_disputed")
    )

    admin = await UserFactory.create(db_session, role="admin")
    headers = auth_headers(str(admin.id), "admin")
    base = f"{settings.API_V1_PREFIX}/admin/bookings"

    # disputed=true returns only the charged-back booking, badged.
    resp = await client.get(f"{base}?disputed=true&per_page=200", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = {b["id"] for b in resp.json()["bookings"]}
    assert str(disputed.booking_id) in ids
    assert str(clean.booking_id) not in ids
    assert all(b["is_disputed"] for b in resp.json()["bookings"])

    # disputed=false excludes it.
    resp = await client.get(f"{base}?disputed=false&per_page=200", headers=headers)
    ids = {b["id"] for b in resp.json()["bookings"]}
    assert str(disputed.booking_id) not in ids
    assert str(clean.booking_id) in ids

    # Unfiltered returns both, with the badge distinguishing them.
    resp = await client.get(f"{base}?per_page=200", headers=headers)
    flags = {b["id"]: b["is_disputed"] for b in resp.json()["bookings"]}
    assert flags[str(disputed.booking_id)] is True
    assert flags[str(clean.booking_id)] is False
