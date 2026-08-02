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
    )

    with patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""):
        response = await client.post(
            "/api/v1/payments/webhook",
            content=webhook_body,
            headers={"Content-Type": "application/json"},
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
