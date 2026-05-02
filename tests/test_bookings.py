"""
Booking integration tests.
Covers: create booking (commission split), state-machine transitions,
invalid transitions, doorstep OTP verification.
"""
import pytest
from uuid import uuid4
from unittest.mock import patch, AsyncMock

from app.models.booking import BookingStatus, PaymentMethod, PaymentStatus
from tests.factories.user_factory import UserFactory
from tests.factories.booking_factory import BookingFactory


@pytest.mark.asyncio
async def test_create_booking_success(client, auth_headers, db_session):
    """Commission split must be exactly 20% platform / 80% provider."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider_user = await UserFactory.create(db_session, role="provider")

    # Build supporting records directly on db_session
    from app.models.provider_profile import ProviderProfile
    from app.models.category import Category
    from app.models.service import Service
    from app.models.address import Address

    category = Category(name="Plumbing", slug="plumbing", is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()

    profile = ProviderProfile(user_id=provider_user.id)
    db_session.add(profile)
    await db_session.flush()

    service = Service(
        provider_id=profile.id,
        category_id=category.id,
        name="Basic Pipe Fix",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)

    address = Address(user_id=consumer.id, label="home", full_address="123 Main St")
    db_session.add(address)
    await db_session.flush()

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = lambda **kwargs: None
        response = await client.post(
            "/api/v1/bookings/",
            json={
                "service_id": str(service.id),
                "address_id": str(address.id),
                "payment_method": "online",
            },
            headers=auth_headers(str(consumer.id), "consumer"),
        )

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "requested"
    assert data["service_amount"] == 100.0
    assert data["platform_commission"] == 20.0  # 20% of 100
    assert data["provider_payout"] == 80.0       # 80% of 100


@pytest.mark.asyncio
async def test_invalid_state_transition(client, auth_headers, db_session):
    """REQUESTED → COMPLETED must be rejected with 409."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        status=BookingStatus.REQUESTED,
    )

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/complete",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 409
    assert "Cannot move from requested to completed" in response.json()["message"]


@pytest.mark.asyncio
async def test_accept_booking_wrong_provider(client, auth_headers, db_session):
    """A different provider must not be able to accept another provider's booking."""
    consumer = await UserFactory.create(db_session, role="consumer")
    correct_provider = await UserFactory.create(db_session, role="provider")
    wrong_provider = await UserFactory.create(db_session, role="provider")
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=correct_provider.id,
        status=BookingStatus.REQUESTED,
    )

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/accept",
        headers=auth_headers(str(wrong_provider.id), "provider"),
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_start_booking_wrong_otp(client, auth_headers, db_session):
    """Invalid doorstep OTP must return 400."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        status=BookingStatus.EN_ROUTE,
        doorstep_otp="1234",
    )

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/start",
        json={"otp": "9999"},
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 400
