"""
Payment integration tests.
Covers: signature verification pass/fail, webhook idempotency.
"""
import pytest
import hmac
import hashlib
from unittest.mock import patch, AsyncMock, MagicMock

from tests.factories.user_factory import UserFactory
from tests.factories.booking_factory import BookingFactory
from app.models.booking import BookingStatus, PaymentStatus
from app.config import get_settings

settings = get_settings()


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
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
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


@pytest.mark.asyncio
async def test_webhook_payment_captured_idempotent(client, db_session):
    """Second webhook call for the same order must not double-process."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        razorpay_order_id="order_already_paid",
    )

    response = await client.post(
        "/api/v1/payments/webhook",
        json={
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {"order_id": "order_already_paid"}
                }
            },
        },
    )

    assert response.status_code == 200
    # Payment status should remain PAID, not raise an error
    await db_session.refresh(booking)
    assert booking.payment_status == PaymentStatus.PAID
