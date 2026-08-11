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
    # No ProviderProfile / razorpay_linked_account_id is created for the provider.
    booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.COMPLETED,
    )

    service = PaymentService(db_session)

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.order.create.return_value = {"id": "order_no_route_test"}
        result = await service.create_order(booking.id)

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
        result = await PaymentService(db_session).create_order(booking.id)

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
        result = await PaymentService(db_session).create_order(booking.id)

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
        result = await PaymentService(db_session).create_order(booking.id)

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
