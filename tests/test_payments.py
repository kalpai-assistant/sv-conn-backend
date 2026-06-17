"""
Payment integration tests.
Covers: signature verification pass/fail, webhook idempotency.
"""
import json
import pytest
import hmac
import hashlib
from unittest.mock import patch
from sqlalchemy import select, func

from tests.factories.user_factory import UserFactory
from tests.factories.booking_factory import BookingFactory
from app.models.booking import BookingStatus, PaymentStatus
from app.models.payment import Payment
from app.services.payment_service import PaymentService
from app.config import get_settings

settings = get_settings()


async def _create_booking_with_deps(db_session, consumer, provider, **kwargs):
    """Create a booking with all required FK dependencies."""
    from app.models.category import Category
    from app.models.service import Service
    from app.models.address import Address

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
        db_session, consumer, provider,
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


@pytest.mark.asyncio
async def test_verify_payment_invalid_signature(client, auth_headers, db_session):
    """Tampered signature must return 402."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _create_booking_with_deps(
        db_session, consumer, provider,
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
        db_session, consumer, provider,
        status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        razorpay_order_id="order_already_paid",
    )

    payment_count_before = (await db_session.execute(
        select(func.count()).select_from(Payment).where(Payment.booking_id == booking.id)
    )).scalar_one()

    webhook_body = json.dumps({
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {"order_id": "order_already_paid"}
            }
        },
    })

    with patch.object(settings, "RAZORPAY_WEBHOOK_SECRET", ""):
        response = await client.post(
            "/api/v1/payments/webhook",
            content=webhook_body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200

    await db_session.refresh(booking)
    assert booking.payment_status == PaymentStatus.PAID

    payment_count_after = (await db_session.execute(
        select(func.count()).select_from(Payment).where(Payment.booking_id == booking.id)
    )).scalar_one()
    assert payment_count_after == payment_count_before
