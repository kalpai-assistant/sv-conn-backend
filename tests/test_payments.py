"""
Payment integration tests.
Covers: signature verification pass/fail, webhook idempotency.
"""

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.exceptions import PaymentError
from app.models.booking import BookingStatus, PaymentStatus
from app.models.payment import Payment
from app.services.payment_service import PaymentService
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

settings = get_settings()


async def _create_booking_with_deps(db_session, consumer, provider, **kwargs):
    """Create a booking with all required FK dependencies."""
    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    category = Category(name="Home Repair", slug="home-repair", is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()

    service = Service(
        category_id=category.id,
        name="AC Service",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)

    address = Address(user_id=consumer.id, label="home", full_address="789 Test Ave")
    db_session.add(address)
    await db_session.flush()

    return await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        **kwargs,
    )


def _make_valid_signature(order_id: str, payment_id: str) -> str:
    return hmac.new(
        key=settings.RAZORPAY_KEY_SECRET.encode(),
        msg=f"{order_id}|{payment_id}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _sign_webhook(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _fake_razorpay_payment(
    razorpay_payment_id: str,
    amount_paise: int,
    method: str = "upi",
    status_: str = "captured",
    currency: str = "INR",
) -> dict:
    """A minimal Razorpay-shape payment entity for verify_payment tests.

    Fields verify_payment (SVC-174 G7 + SVC-179 G11) actually reads: status,
    currency, amount, method. Everything else stripped so a bad key in the
    production code fails fast in the test rather than being papered over.
    """
    return {
        "id": razorpay_payment_id,
        "status": status_,
        "currency": currency,
        "amount": amount_paise,
        "method": method,
    }


# ------------------------------------------------------------------
# create_order works without a provider Route account
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_without_provider_linked_account(db_session):
    """
    A booking whose provider has no Razorpay linked account must still be able to
    create a payment order. Payment lands in the platform account — no Route
    transfers — so create_order no longer requires a provider payout account.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    # No ProviderProfile is created for the provider — SVC-130 removed the Route
    # requirement and SVC-183 (G17) dropped the field itself. Kept as a
    # regression against re-introducing a Route-style precondition.
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
    )

    service = PaymentService(db_session)

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.order.create.return_value = {"id": "order_no_route_test"}
        result = await service.create_order(booking.id, consumer.id)

    # Order created successfully and persisted on the booking.
    assert result["razorpay_order_id"] == "order_no_route_test"
    await db_session.refresh(booking)
    assert booking.razorpay_order_id == "order_no_route_test"

    # The order must NOT carry a Route transfers array.
    _, call_kwargs = mock_rzp.order.create.call_args
    assert "transfers" not in call_kwargs["data"]


# ------------------------------------------------------------------
# SVC-39 G22: create_order subtracts discount_amount from the charge
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_subtracts_discount_from_charge(db_session):
    """SVC-39 G22: a coupon booking bills the discounted amount, not the full one.

    Before this the customer was charged ``service_amount`` regardless of any
    coupon, so every coupon booking overcharged by the discount amount while
    the redemption was still recorded. The rule is stated in
    ``app/models/coupon.py``: "Effective charge to customer =
    service_amount − discount_amount + tip_amount" (tip added post-completion).
    """
    from decimal import Decimal

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
    )
    # ``service_amount`` = 100.0 from _create_booking_with_deps' service; layer a
    # 25-rupee coupon on top. Both Numeric after SVC-39 G31, so the arithmetic
    # is exact.
    booking.service_amount = Decimal("100.00")
    booking.discount_amount = Decimal("25.00")
    await db_session.flush()

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.order.create.return_value = {"id": "order_discounted"}
        result = await PaymentService(db_session).create_order(booking.id, consumer.id)

    # ₹100.00 − ₹25.00 = ₹75.00 = 7500 paise.
    assert result["amount"] == 7500
    _, call_kwargs = mock_rzp.order.create.call_args
    assert call_kwargs["data"]["amount"] == 7500


@pytest.mark.asyncio
async def test_create_order_no_discount_charges_full_amount(db_session):
    """A booking with no coupon (discount_amount == 0) is billed the full amount.

    Regression against over-fixing: G22 must only subtract *when there is* a
    discount, not silently reduce a non-coupon order.
    """
    from decimal import Decimal

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
    )
    booking.service_amount = Decimal("200.00")
    # discount_amount defaults to Decimal("0.00") — no override.
    await db_session.flush()

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.order.create.return_value = {"id": "order_no_coupon"}
        result = await PaymentService(db_session).create_order(booking.id, consumer.id)

    assert result["amount"] == 20000
    assert booking.discount_amount == Decimal("0.00")


@pytest.mark.asyncio
async def test_create_order_paisa_exact_after_discount(db_session):
    """The exact-money case that motivated SVC-150 also holds through G22.

    ₹100.09 − ₹10.02 = ₹90.07 must bill 9007 paise, not 9006 or 9008. Float
    arithmetic would truncate here; Decimal doesn't.
    """
    from decimal import Decimal

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
    )
    booking.service_amount = Decimal("100.09")
    booking.discount_amount = Decimal("10.02")
    await db_session.flush()

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.order.create.return_value = {"id": "order_precise"}
        result = await PaymentService(db_session).create_order(booking.id, consumer.id)

    assert result["amount"] == 9007


@pytest.mark.asyncio
async def test_verify_payment_invalid_signature(client, auth_headers, db_session):
    """Tampered signature must return 402."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        razorpay_order_id="order_test_123",
    )

    response = await client.post(
        "/api/v1/payments/verify",
        json={
            "razorpay_order_id": "order_test_123",
            "razorpay_payment_id": "pay_test_456",
            "razorpay_signature": "bad_signature",
        },
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 402
    assert "signature" in response.json()["message"].lower()


# ------------------------------------------------------------------
# SVC-173 (G5): payment endpoints refuse when the caller isn't the booking owner
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_refuses_when_caller_isnt_booking_owner(
    client, auth_headers, db_session
):
    """SVC-173 (G5): a stranger cannot mint a Razorpay order for someone else's booking."""
    consumer = await UserFactory.create(db_session, role="consumer")
    stranger = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session, consumer, provider, status=BookingStatus.COMPLETED
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        response = await client.post(
            "/api/v1/payments/orders",
            json={"booking_id": str(booking.id)},
            headers=auth_headers(str(stranger.id), "consumer"),
        )

    assert response.status_code == 403
    mock_rzp.order.create.assert_not_called()
    await db_session.refresh(booking)
    assert booking.razorpay_order_id is None


@pytest.mark.asyncio
async def test_create_order_owner_still_succeeds(client, auth_headers, db_session):
    """Regression: G5 must not break the happy path — the booking's own consumer
    can still mint an order."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session, consumer, provider, status=BookingStatus.COMPLETED
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.order.create.return_value = {"id": "order_owner_ok"}
        response = await client.post(
            "/api/v1/payments/orders",
            json={"booking_id": str(booking.id)},
            headers=auth_headers(str(consumer.id), "consumer"),
        )

    assert response.status_code == 200
    assert response.json()["razorpay_order_id"] == "order_owner_ok"


@pytest.mark.asyncio
async def test_verify_payment_refuses_when_caller_isnt_booking_owner(
    client, auth_headers, db_session
):
    """SVC-173 (G5): a stranger with a valid signature cannot flip another user's
    booking to PAID."""
    consumer = await UserFactory.create(db_session, role="consumer")
    stranger = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        razorpay_order_id="order_owned_by_someone_else",
    )

    valid_sig = _make_valid_signature("order_owned_by_someone_else", "pay_relay_attempt")
    response = await client.post(
        "/api/v1/payments/verify",
        json={
            "razorpay_order_id": "order_owned_by_someone_else",
            "razorpay_payment_id": "pay_relay_attempt",
            "razorpay_signature": valid_sig,
        },
        headers=auth_headers(str(stranger.id), "consumer"),
    )

    assert response.status_code == 403


# ------------------------------------------------------------------
# SVC-175 (G6): verify_payment is idempotent on razorpay_payment_id
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_payment_replayed_signature_creates_only_one_ledger_row(db_session):
    """SVC-175 (G6): replaying the same order|payment id must not duplicate the ledger.

    The signature is a pure function of (order_id, payment_id) and stays valid
    forever, so before this fix a re-submitted verify wrote a second CAPTURED
    row. The partial unique index (migration 5b293fc63525) + the query-first
    upsert in verify_payment now make it a no-op.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        razorpay_order_id="order_replay_test",
    )

    sig = _make_valid_signature("order_replay_test", "pay_replay_test")
    svc = PaymentService(db_session)

    # SVC-174 (G7) fetches the payment entity for amount/currency/status checks;
    # supply a matching-amount entity so the verify path proceeds normally.
    entity = _fake_razorpay_payment("pay_replay_test", amount_paise=10000)
    with patch("app.services.payment_service.razorpay_client.payment.fetch", return_value=entity):
        first = await svc.verify_payment(
            razorpay_order_id="order_replay_test",
            razorpay_payment_id="pay_replay_test",
            razorpay_signature=sig,
            consumer_id=consumer.id,  # SVC-173 (G5)
        )
        second = await svc.verify_payment(
            razorpay_order_id="order_replay_test",
            razorpay_payment_id="pay_replay_test",
            razorpay_signature=sig,
            consumer_id=consumer.id,
        )

    # Same row returned twice — the second call short-circuits on the existing lookup.
    assert first.id == second.id

    # And only ONE row exists in the ledger.
    count = (
        await db_session.execute(
            select(func.count()).select_from(Payment).where(Payment.booking_id == booking.id)
        )
    ).scalar_one()
    assert count == 1

    # Booking is (still) PAID.
    await db_session.refresh(booking)
    assert booking.payment_status == PaymentStatus.PAID


@pytest.mark.asyncio
async def test_verify_payment_leaves_failed_rows_alone(db_session):
    """A booking with a prior failed attempt (payment_id NULL) can still be verified.

    Regression against over-tightening: the partial unique index is
    `WHERE razorpay_payment_id IS NOT NULL`, so multiple failed attempts
    (which have NULL payment_id) still coexist per booking. A subsequent
    successful verify writes exactly one CAPTURED row alongside them.
    """
    from app.models.payment import Payment as PaymentModel
    from app.models.payment import PaymentRecordMethod, PaymentRecordStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        razorpay_order_id="order_after_failure",
    )
    # Two prior failed rows with NULL payment id — the WHERE-NOT-NULL partial
    # constraint must permit this.
    for _ in range(2):
        db_session.add(
            PaymentModel(
                booking_id=booking.id,
                razorpay_payment_id=None,
                razorpay_order_id="order_after_failure",
                amount=booking.service_amount,
                platform_fee=booking.platform_commission,
                provider_settlement=booking.provider_payout,
                status=PaymentRecordStatus.FAILED,
                method=PaymentRecordMethod.UPI,
            )
        )
    await db_session.flush()

    sig = _make_valid_signature("order_after_failure", "pay_finally_ok")
    entity = _fake_razorpay_payment("pay_finally_ok", amount_paise=10000)
    with patch("app.services.payment_service.razorpay_client.payment.fetch", return_value=entity):
        payment = await PaymentService(db_session).verify_payment(
            razorpay_order_id="order_after_failure",
            razorpay_payment_id="pay_finally_ok",
            razorpay_signature=sig,
            consumer_id=consumer.id,  # SVC-173 (G5)
        )

    assert payment.status == PaymentRecordStatus.CAPTURED
    # Three rows total: two FAILED + one CAPTURED.
    count = (
        await db_session.execute(
            select(func.count()).select_from(Payment).where(Payment.booking_id == booking.id)
        )
    ).scalar_one()
    assert count == 3


# ------------------------------------------------------------------
# SVC-174 (G7) + SVC-179 (G11): verify_payment fetches the entity, checks it, resolves method
# ------------------------------------------------------------------


async def _booking_for_verify(db_session, order_id: str):
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    return (
        consumer,
        provider,
        await _create_booking_with_deps(
            db_session,
            consumer,
            provider,
            status=BookingStatus.COMPLETED,
            razorpay_order_id=order_id,
        ),
    )


@pytest.mark.asyncio
async def test_create_order_sets_partial_payment_zero(db_session):
    """SVC-174 (G7): create_order must set partial_payment=0 so Razorpay refuses
    a short payment for this order — closes the door on the mismatch verify_payment
    is (now) primed to catch."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session, consumer, provider, status=BookingStatus.COMPLETED
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.order.create.return_value = {"id": "order_no_partial"}
        await PaymentService(db_session).create_order(booking.id, consumer.id)

    _, call_kwargs = mock_rzp.order.create.call_args
    assert call_kwargs["data"]["partial_payment"] == 0


@pytest.mark.asyncio
async def test_verify_payment_refuses_short_amount(db_session):
    """SVC-174 (G7): a partial payment (amount < what the booking expects) is refused."""
    consumer, _, booking = await _booking_for_verify(db_session, "order_short")
    sig = _make_valid_signature("order_short", "pay_short")
    # Booking is 100.00 rupees → 10000 paise. Razorpay reports 5000. Refuse.
    entity = _fake_razorpay_payment("pay_short", amount_paise=5000)
    svc = PaymentService(db_session)

    with patch("app.services.payment_service.razorpay_client.payment.fetch", return_value=entity):
        with pytest.raises(PaymentError, match="amount=5000"):
            await svc.verify_payment(
                razorpay_order_id="order_short",
                razorpay_payment_id="pay_short",
                razorpay_signature=sig,
                consumer_id=consumer.id,
            )

    # No ledger row was written.
    count = (
        await db_session.execute(
            select(func.count()).select_from(Payment).where(Payment.booking_id == booking.id)
        )
    ).scalar_one()
    assert count == 0
    # And the booking is not flipped to PAID.
    await db_session.refresh(booking)
    assert booking.payment_status == PaymentStatus.PENDING


@pytest.mark.asyncio
async def test_verify_payment_refuses_wrong_currency(db_session):
    """SVC-174 (G7): a USD-currency payment is refused even if signed correctly."""
    consumer, _, _ = await _booking_for_verify(db_session, "order_usd")
    sig = _make_valid_signature("order_usd", "pay_usd")
    entity = _fake_razorpay_payment("pay_usd", amount_paise=10000, currency="USD")

    with patch("app.services.payment_service.razorpay_client.payment.fetch", return_value=entity):
        with pytest.raises(PaymentError, match="currency='USD'"):
            await PaymentService(db_session).verify_payment(
                razorpay_order_id="order_usd",
                razorpay_payment_id="pay_usd",
                razorpay_signature=sig,
                consumer_id=consumer.id,
            )


@pytest.mark.asyncio
async def test_verify_payment_refuses_authorized_but_not_captured(db_session):
    """SVC-174 (G7): a payment stuck in 'authorized' has not moved the money.
    The signature alone would have flipped the booking to PAID."""
    consumer, _, _ = await _booking_for_verify(db_session, "order_auth")
    sig = _make_valid_signature("order_auth", "pay_auth")
    entity = _fake_razorpay_payment("pay_auth", amount_paise=10000, status_="authorized")

    with patch("app.services.payment_service.razorpay_client.payment.fetch", return_value=entity):
        with pytest.raises(PaymentError, match="status='authorized'"):
            await PaymentService(db_session).verify_payment(
                razorpay_order_id="order_auth",
                razorpay_payment_id="pay_auth",
                razorpay_signature=sig,
                consumer_id=consumer.id,
            )


@pytest.mark.asyncio
async def test_verify_payment_records_card_method(db_session):
    """SVC-179 (G11): a card payment is recorded as CARD, not the old hard-coded UPI."""
    from app.models.payment import PaymentRecordMethod

    consumer, _, _ = await _booking_for_verify(db_session, "order_card")
    sig = _make_valid_signature("order_card", "pay_card")
    entity = _fake_razorpay_payment("pay_card", amount_paise=10000, method="card")

    with patch("app.services.payment_service.razorpay_client.payment.fetch", return_value=entity):
        payment = await PaymentService(db_session).verify_payment(
            razorpay_order_id="order_card",
            razorpay_payment_id="pay_card",
            razorpay_signature=sig,
            consumer_id=consumer.id,
        )
    assert payment.method == PaymentRecordMethod.CARD


@pytest.mark.asyncio
async def test_verify_payment_records_netbanking_method(db_session):
    """SVC-179 (G11): netbanking maps to NETBANKING."""
    from app.models.payment import PaymentRecordMethod

    consumer, _, _ = await _booking_for_verify(db_session, "order_nb")
    sig = _make_valid_signature("order_nb", "pay_nb")
    entity = _fake_razorpay_payment("pay_nb", amount_paise=10000, method="netbanking")

    with patch("app.services.payment_service.razorpay_client.payment.fetch", return_value=entity):
        payment = await PaymentService(db_session).verify_payment(
            razorpay_order_id="order_nb",
            razorpay_payment_id="pay_nb",
            razorpay_signature=sig,
            consumer_id=consumer.id,
        )
    assert payment.method == PaymentRecordMethod.NETBANKING


@pytest.mark.asyncio
async def test_verify_payment_unknown_method_falls_back_to_upi(db_session):
    """Regression guard: an unrecognized Razorpay method (e.g. wallet, emi) is
    recorded as UPI with a log warning — the row still writes."""
    from app.models.payment import PaymentRecordMethod

    consumer, _, _ = await _booking_for_verify(db_session, "order_wallet")
    sig = _make_valid_signature("order_wallet", "pay_wallet")
    entity = _fake_razorpay_payment("pay_wallet", amount_paise=10000, method="wallet")

    with patch("app.services.payment_service.razorpay_client.payment.fetch", return_value=entity):
        payment = await PaymentService(db_session).verify_payment(
            razorpay_order_id="order_wallet",
            razorpay_payment_id="pay_wallet",
            razorpay_signature=sig,
            consumer_id=consumer.id,
        )
    assert payment.method == PaymentRecordMethod.UPI


# ------------------------------------------------------------------
# SVC-31: Webhook Idempotency
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_payment_captured_idempotent(client, db_session):
    """Second webhook call for an already-paid booking must not double-process."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        razorpay_order_id="order_already_paid",
    )

    payment_count_before = (
        await db_session.execute(
            select(func.count()).select_from(Payment).where(Payment.booking_id == booking.id)
        )
    ).scalar_one()

    webhook_body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"order_id": "order_already_paid"}}},
        }
    ).encode()

    response = await client.post(
        "/api/v1/payments/webhook",
        content=webhook_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": _sign_webhook(webhook_body, settings.RAZORPAY_WEBHOOK_SECRET),
        },
    )

    assert response.status_code == 200

    await db_session.refresh(booking)
    assert booking.payment_status == PaymentStatus.PAID

    payment_count_after = (
        await db_session.execute(
            select(func.count()).select_from(Payment).where(Payment.booking_id == booking.id)
        )
    ).scalar_one()
    assert payment_count_after == payment_count_before


# ------------------------------------------------------------------
# SVC-176 (G8) — captured webhook writes ledger; unknown order acks; failed logs
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captured_webhook_writes_ledger_row_when_verify_didnt(client, db_session):
    """SVC-176 (G8): the load-bearing case — consumer paid, then killed the app
    before verify_payment ran. The webhook is the only signal left. Before this
    fix the booking flipped to PAID with **no ledger row at all** — no amount,
    no payment_id, no method — and a later chargeback could not be matched.
    Now the webhook upserts the same row verify would have written.
    """
    from app.models.payment import PaymentRecordMethod, PaymentRecordStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        razorpay_order_id="order_webhook_writes",
    )

    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_webhook_writes",
                        "order_id": "order_webhook_writes",
                        "method": "card",
                    }
                }
            },
        }
    ).encode()

    response = await client.post(
        "/api/v1/payments/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": _sign_webhook(body, settings.RAZORPAY_WEBHOOK_SECRET),
        },
    )
    assert response.status_code == 200

    # A CAPTURED ledger row exists, keyed on the payment id, with resolved method.
    payment_row = (
        await db_session.execute(
            select(Payment).where(Payment.razorpay_payment_id == "pay_webhook_writes")
        )
    ).scalar_one()
    assert payment_row.booking_id == booking.id
    assert payment_row.status == PaymentRecordStatus.CAPTURED
    assert payment_row.method == PaymentRecordMethod.CARD

    await db_session.refresh(booking)
    assert booking.payment_status == PaymentStatus.PAID


@pytest.mark.asyncio
async def test_captured_webhook_is_idempotent_on_replay(client, db_session):
    """Razorpay retries webhooks; a redelivery must not write a second row."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        razorpay_order_id="order_webhook_replay",
    )

    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_webhook_replay",
                        "order_id": "order_webhook_replay",
                        "method": "upi",
                    }
                }
            },
        }
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": _sign_webhook(body, settings.RAZORPAY_WEBHOOK_SECRET),
    }

    for _ in range(2):
        response = await client.post("/api/v1/payments/webhook", content=body, headers=headers)
        assert response.status_code == 200

    count = (
        await db_session.execute(
            select(func.count()).select_from(Payment).where(Payment.booking_id == booking.id)
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_captured_webhook_unknown_order_acks_no_retry_storm(client, db_session):
    """SVC-176 (G8): an unknown order_id must return 200 so Razorpay stops
    retrying — a stray 402 kicks off the full retry schedule for a delivery
    that can never succeed. Matches the dispute handler's policy."""
    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {"entity": {"id": "pay_unknown", "order_id": "order_does_not_exist"}}
            },
        }
    ).encode()

    response = await client.post(
        "/api/v1/payments/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": _sign_webhook(body, settings.RAZORPAY_WEBHOOK_SECRET),
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_failed_webhook_unknown_order_acks_no_retry_storm(client, db_session):
    """Same policy for `payment.failed`: unknown order → 200, log, drop."""
    body = json.dumps(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {"entity": {"id": "pay_lost", "order_id": "order_does_not_exist"}}
            },
        }
    ).encode()

    response = await client.post(
        "/api/v1/payments/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": _sign_webhook(body, settings.RAZORPAY_WEBHOOK_SECRET),
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_failed_webhook_writes_failed_ledger_row(client, db_session):
    """`payment.failed` writes a FAILED audit row for the booking (unchanged
    behaviour; SVC-176 only added logging for exceptions, not new semantics)."""
    from app.models.payment import PaymentRecordStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        razorpay_order_id="order_failed_webhook",
    )

    body = json.dumps(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_failed_1",
                        "order_id": "order_failed_webhook",
                        "method": "card",
                    }
                }
            },
        }
    ).encode()

    response = await client.post(
        "/api/v1/payments/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": _sign_webhook(body, settings.RAZORPAY_WEBHOOK_SECRET),
        },
    )
    assert response.status_code == 200

    row = (
        await db_session.execute(
            select(Payment).where(Payment.razorpay_payment_id == "pay_failed_1")
        )
    ).scalar_one()
    assert row.booking_id == booking.id
    assert row.status == PaymentRecordStatus.FAILED


# ------------------------------------------------------------------
# SVC-39 G3: webhook secret must not silently disable verification in prod
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_refuses_when_secret_missing_in_non_dev(client):
    """SVC-39 G3: empty secret + non-dev must 500, not silently accept.

    The endpoint is unauthenticated by design (Razorpay initiates), so an
    unset secret used to leave the route open to forged webhooks. Dev keeps
    the log-warn-and-accept behaviour so local `.env` files without secrets
    still work; staging and production hard-refuse.
    """
    body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
    with (
        patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""),
        patch.object(settings, "ENVIRONMENT", "production"),
    ):
        response = await client.post(
            "/api/v1/payments/webhook",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 500
    # FastAPI's default HTTPException handler returns {"detail": "..."} — the
    # app-level generic handler in exceptions.py only runs for uncaught
    # exceptions, not for a raised HTTPException.
    assert "Webhook secret not configured" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_accepts_empty_secret_in_dev(client, db_session):
    """Dev keeps the shortcut so a local `.env` without secrets still works,
    just log-warns loudly. Regression against the fix over-tightening."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        razorpay_order_id="order_dev_bypass",
    )

    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"order_id": "order_dev_bypass"}}},
        }
    ).encode()
    with (
        patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""),
        patch.object(settings, "ENVIRONMENT", "dev"),
    ):
        response = await client.post(
            "/api/v1/payments/webhook",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# SVC-190 (G14) — abandoned-payment expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_includes_expire_by(db_session):
    """`create_order` hands Razorpay an `expire_by` Unix second, so a client
    that cached the order id can't submit it days later.

    Razorpay refuses payment after that instant, so this is the belt half of
    the abandoned-payment defence; the local sweep is the braces.
    """
    import time as _time

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(db_session, consumer, provider)

    before = int(_time.time())

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.order.create.return_value = {"id": "order_expiry_test"}
        await PaymentService(db_session).create_order(booking.id, consumer.id)

    _, call_kwargs = mock_rzp.order.create.call_args
    order_data = call_kwargs["data"]
    assert "expire_by" in order_data
    # The value is `now + PAYMENT_ORDER_EXPIRY_SECONDS`; allow a couple of
    # seconds of test-scheduling jitter on either side.
    expected = before + settings.PAYMENT_ORDER_EXPIRY_SECONDS
    assert expected - 2 <= order_data["expire_by"] <= expected + 3


@pytest.mark.asyncio
async def test_expire_abandoned_payments_flips_stale_pending(db_session):
    """A booking with a Razorpay order older than expiry+grace, still PENDING,
    gets its payment_status flipped to EXPIRED.

    Simulates the abandoned-modal flow: order created, no capture, wall time
    passed the window. Runs the task's query directly against the async test
    session (bypasses the sync Celery wiring) so the test can inspect the
    result without spinning up a worker.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.models.booking import Booking, PaymentStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        payment_status=PaymentStatus.PENDING,
        razorpay_order_id="order_stale_abandoned",
    )
    # Backdate created_at past the window + grace.
    stale_at = datetime.now(UTC) - timedelta(
        seconds=settings.PAYMENT_ORDER_EXPIRY_SECONDS
        + settings.PAYMENT_ORDER_EXPIRY_GRACE_SECONDS
        + 60,
    )
    booking.created_at = stale_at
    await db_session.flush()

    # Same query the Celery task runs.
    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.PAYMENT_ORDER_EXPIRY_SECONDS + settings.PAYMENT_ORDER_EXPIRY_GRACE_SECONDS,
    )
    result = await db_session.execute(
        update(Booking)
        .where(Booking.razorpay_order_id.is_not(None))
        .where(Booking.payment_status == PaymentStatus.PENDING)
        .where(Booking.created_at < cutoff)
        .values(payment_status=PaymentStatus.EXPIRED)
    )
    assert result.rowcount == 1

    await db_session.refresh(booking)
    assert booking.payment_status is PaymentStatus.EXPIRED


@pytest.mark.asyncio
async def test_expire_abandoned_payments_leaves_fresh_pending_alone(db_session):
    """A booking whose order is still inside the window is not touched — the
    consumer might still be on the modal. Regression against an over-broad
    filter that would flip live checkouts to EXPIRED.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.models.booking import Booking, PaymentStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        payment_status=PaymentStatus.PENDING,
        razorpay_order_id="order_still_live",
    )
    # created_at defaults to "now" — well inside the window.

    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.PAYMENT_ORDER_EXPIRY_SECONDS + settings.PAYMENT_ORDER_EXPIRY_GRACE_SECONDS,
    )
    result = await db_session.execute(
        update(Booking)
        .where(Booking.razorpay_order_id.is_not(None))
        .where(Booking.payment_status == PaymentStatus.PENDING)
        .where(Booking.created_at < cutoff)
        .values(payment_status=PaymentStatus.EXPIRED)
    )
    assert result.rowcount == 0

    await db_session.refresh(booking)
    assert booking.payment_status is PaymentStatus.PENDING


@pytest.mark.asyncio
async def test_expire_abandoned_payments_ignores_paid_and_cash(db_session):
    """PAID bookings (captured before expiry) and cash bookings (no order id)
    are never touched. The two negative filters are the ones a bug would
    accidentally omit — this test pins both.

    Uses two consumers so each booking gets its own address (and each
    ``_create_booking_with_deps`` call seeds its own service/category with
    unique names to avoid the shared-slug collision).
    """
    import uuid as _uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.models.address import Address
    from app.models.booking import Booking, PaymentStatus
    from app.models.category import Category
    from app.models.service import Service

    provider = await UserFactory.create(db_session, role="provider")

    async def _seed_booking(**booking_kwargs):
        consumer = await UserFactory.create(db_session, role="consumer")
        suffix = _uuid.uuid4().hex[:8]
        cat = Category(
            name=f"Home Repair {suffix}",
            slug=f"home-repair-{suffix}",
            is_active=True,
            sort_order=1,
        )
        db_session.add(cat)
        await db_session.flush()
        svc = Service(
            category_id=cat.id,
            name=f"AC Service {suffix}",
            base_price=500.0,
            duration_minutes=60,
            is_active=True,
        )
        db_session.add(svc)
        addr = Address(user_id=consumer.id, label="home", full_address="789 Test Ave")
        db_session.add(addr)
        await db_session.flush()
        return await BookingFactory.create(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            service_id=svc.id,
            address_id=addr.id,
            **booking_kwargs,
        )

    # (a) Old + PAID — must not flip.
    paid = await _seed_booking(
        payment_status=PaymentStatus.PAID,
        razorpay_order_id="order_old_paid",
    )
    paid.created_at = datetime.now(UTC) - timedelta(days=7)

    # (b) Old + cash (no order id) — must not flip.
    cash = await _seed_booking(
        payment_status=PaymentStatus.PENDING,
        razorpay_order_id=None,
    )
    cash.created_at = datetime.now(UTC) - timedelta(days=7)

    await db_session.flush()

    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.PAYMENT_ORDER_EXPIRY_SECONDS + settings.PAYMENT_ORDER_EXPIRY_GRACE_SECONDS,
    )
    result = await db_session.execute(
        update(Booking)
        .where(Booking.razorpay_order_id.is_not(None))
        .where(Booking.payment_status == PaymentStatus.PENDING)
        .where(Booking.created_at < cutoff)
        .values(payment_status=PaymentStatus.EXPIRED)
    )
    assert result.rowcount == 0

    await db_session.refresh(paid)
    await db_session.refresh(cash)
    assert paid.payment_status is PaymentStatus.PAID
    assert cash.payment_status is PaymentStatus.PENDING


def test_expire_abandoned_payments_task_registered_on_beat():
    """SVC-190: the sweep task is on the Celery Beat schedule.

    Structural pin — a Beat entry that never got registered would silently
    never run. Keeps the wiring honest without spinning up a worker.
    """
    from app.tasks.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "expire-abandoned-payments" in schedule
    entry = schedule["expire-abandoned-payments"]
    assert entry["task"] == "app.tasks.cleanup_tasks.expire_abandoned_payments"
