"""
Booking integration tests.
Covers: create booking (commission split), state-machine transitions,
invalid transitions, doorstep OTP verification, single booking fetch.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.booking import BookingStatus, PaymentMethod, PaymentStatus
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _create_booking_with_deps(db_session, consumer, provider, **kwargs):
    """Create a booking with all required FK dependencies (service, address)."""
    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    category = Category(name="Cleaning", slug="cleaning", is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()

    service = Service(
        category_id=category.id,
        name="Deep Clean",
        base_price=200.0,
        duration_minutes=90,
        is_active=True,
    )
    db_session.add(service)

    address = Address(user_id=consumer.id, label="home", full_address="456 Test St")
    db_session.add(address)
    await db_session.flush()

    return (
        service,
        address,
        await BookingFactory.create(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            service_id=service.id,
            address_id=address.id,
            **kwargs,
        ),
    )


@pytest.mark.asyncio
async def test_create_booking_success(client, auth_headers, db_session):
    """Commission split must be exactly 20% platform / 80% provider."""
    consumer = await UserFactory.create(db_session, role="consumer")
    await UserFactory.create(db_session, role="provider")

    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    category = Category(name="Plumbing", slug="plumbing", is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()

    service = Service(
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
    assert data["provider_payout"] == 80.0  # 80% of 100


@pytest.mark.asyncio
async def test_invalid_state_transition(client, auth_headers, db_session):
    """REQUESTED → COMPLETED must be rejected with 409."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    _, _, booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
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
    _, _, booking = await _create_booking_with_deps(
        db_session,
        consumer,
        correct_provider,
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
    _, _, booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.EN_ROUTE,
        doorstep_otp="1234",
    )

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/start",
        json={"otp": "9999"},
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 400


# ------------------------------------------------------------------
# SVC-29: En-Route Transition
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_en_route_transition(client, auth_headers, db_session):
    """Provider can transition an ACCEPTED booking to EN_ROUTE."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    _, _, booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.ACCEPTED,
    )

    response = await client.patch(
        f"/api/v1/bookings/{booking.id}/en-route",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "en_route"
    assert data["id"] == str(booking.id)


# ------------------------------------------------------------------
# SVC-30: Cash Payment Task Triggered on Completion
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_cash_booking_triggers_payment_task(client, auth_headers, db_session):
    """Completing a cash booking must trigger process_cash_payment Celery task."""
    from app.models.provider_profile import ProviderProfile

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")

    profile = ProviderProfile(user_id=provider.id)
    db_session.add(profile)
    await db_session.flush()

    _, _, booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.IN_PROGRESS,
        payment_method=PaymentMethod.CASH,
    )

    with (
        patch("app.routers.bookings.send_booking_notification") as mock_notif,
        patch("app.routers.bookings.process_cash_payment") as mock_payment,
    ):
        mock_notif.delay = lambda **kwargs: None

        response = await client.patch(
            f"/api/v1/bookings/{booking.id}/complete",
            headers=auth_headers(str(provider.id), "provider"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    mock_payment.delay.assert_called_once_with(booking_id=str(booking.id))


@pytest.mark.asyncio
async def test_completing_online_booking_preserves_paid_status(client, auth_headers, db_session):
    """SVC-39 G9 regression: an online-paid booking must keep PAID on completion.

    Before this fix the COMPLETED transition unconditionally reset
    ``payment_status`` to PENDING, wiping the record that the consumer paid at
    the exact moment the booking became payout-eligible. Now scoped to cash.
    """
    from app.models.provider_profile import ProviderProfile

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    db_session.add(ProviderProfile(user_id=provider.id))
    await db_session.flush()

    _, _, booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.IN_PROGRESS,
        payment_method=PaymentMethod.ONLINE,
        payment_status=PaymentStatus.PAID,
    )

    with patch("app.routers.bookings.send_booking_notification") as mock_notif:
        mock_notif.delay = lambda **kwargs: None
        response = await client.patch(
            f"/api/v1/bookings/{booking.id}/complete",
            headers=auth_headers(str(provider.id), "provider"),
        )

    assert response.status_code == 200
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.COMPLETED
    # The load-bearing assertion: PAID must survive the transition.
    assert booking.payment_status is PaymentStatus.PAID


@pytest.mark.asyncio
async def test_completing_cash_booking_still_resets_payment_status(
    client, auth_headers, db_session
):
    """The cash path still needs the reset — `process_cash_payment` early-returns
    on PAID, so it depends on completion leaving the booking PENDING for the
    task to move it to PAID. The fix scopes the reset to cash; it doesn't
    remove it.
    """
    from app.models.provider_profile import ProviderProfile

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    db_session.add(ProviderProfile(user_id=provider.id))
    await db_session.flush()

    _, _, booking = await _create_booking_with_deps(
        db_session,
        consumer,
        provider,
        status=BookingStatus.IN_PROGRESS,
        payment_method=PaymentMethod.CASH,
        # Artificial — a cash booking normally reaches completion PENDING. Using
        # PAID here proves the reset still fires for cash rather than merely
        # no-op'ing on already-PENDING rows.
        payment_status=PaymentStatus.PAID,
    )

    with (
        patch("app.routers.bookings.send_booking_notification") as mock_notif,
        patch("app.routers.bookings.process_cash_payment") as mock_payment,
    ):
        mock_notif.delay = lambda **kwargs: None
        response = await client.patch(
            f"/api/v1/bookings/{booking.id}/complete",
            headers=auth_headers(str(provider.id), "provider"),
        )

    assert response.status_code == 200
    await db_session.refresh(booking)
    assert booking.payment_status is PaymentStatus.PENDING
    mock_payment.delay.assert_called_once_with(booking_id=str(booking.id))


# ------------------------------------------------------------------
# SVC-21: GET /bookings/{booking_id}
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_booking_success(client, auth_headers, db_session):
    """Consumer can fetch their own booking and gets 200."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    _, _, booking = await _create_booking_with_deps(db_session, consumer, provider)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(booking.id)
    assert data["consumer_id"] == str(consumer.id)


@pytest.mark.asyncio
async def test_get_booking_wrong_user(client, auth_headers, db_session):
    """A user who is neither consumer nor provider on the booking gets 403."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    stranger = await UserFactory.create(db_session, role="consumer")
    _, _, booking = await _create_booking_with_deps(db_session, consumer, provider)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(stranger.id), "consumer"),
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_get_booking_as_provider(client, auth_headers, db_session):
    """Assigned provider on the booking can fetch it."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    _, _, booking = await _create_booking_with_deps(db_session, consumer, provider)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(booking.id)


@pytest.mark.asyncio
async def test_get_booking_not_found(client, auth_headers, db_session):
    """Non-existent booking returns 404."""
    consumer = await UserFactory.create(db_session, role="consumer")

    response = await client.get(
        f"/api/v1/bookings/{uuid4()}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 404


# ------------------------------------------------------------------
# SVC-186 (G29): Booking.payments is plural (multiple attempts + captured)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_booking_payments_relationship_returns_all_rows(db_session):
    """SVC-186 (G29): a booking with several payment rows must return the whole list.

    Before this fix `Booking.payment` was scalar (``uselist=False``), which would
    raise ``MultipleResultsFound`` on exactly the bookings that had a retry —
    the ones most likely to be under investigation for the refund flow (SVC-178).
    The refactor made it a list keyed on ``back_populates="payments"``.
    """
    from sqlalchemy.orm import selectinload

    from app.models.booking import Booking
    from app.models.payment import Payment as PaymentModel
    from app.models.payment import PaymentRecordMethod, PaymentRecordStatus

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    _, _, booking = await _create_booking_with_deps(
        db_session, consumer, provider, status=BookingStatus.COMPLETED
    )

    # One failed attempt (razorpay_payment_id NULL — permitted by the partial
    # unique index from SVC-175) + one captured row.
    db_session.add(
        PaymentModel(
            booking_id=booking.id,
            razorpay_payment_id=None,
            razorpay_order_id="order_regression",
            amount=booking.service_amount,
            platform_fee=booking.platform_commission,
            provider_settlement=booking.provider_payout,
            status=PaymentRecordStatus.FAILED,
            method=PaymentRecordMethod.UPI,
        )
    )
    db_session.add(
        PaymentModel(
            booking_id=booking.id,
            razorpay_payment_id="pay_regression_ok",
            razorpay_order_id="order_regression",
            amount=booking.service_amount,
            platform_fee=booking.platform_commission,
            provider_settlement=booking.provider_payout,
            status=PaymentRecordStatus.CAPTURED,
            method=PaymentRecordMethod.CARD,
        )
    )
    await db_session.flush()

    from sqlalchemy import select as sa_select

    reloaded = (
        await db_session.execute(
            sa_select(Booking)
            .where(Booking.id == booking.id)
            .options(selectinload(Booking.payments))
        )
    ).scalar_one()

    # Two rows returned — no MultipleResultsFound. Callers filter by status
    # themselves (the CAPTURED row is the authoritative one).
    assert len(reloaded.payments) == 2
    statuses = {p.status for p in reloaded.payments}
    assert statuses == {PaymentRecordStatus.FAILED, PaymentRecordStatus.CAPTURED}


def test_booking_relationship_is_declared_list_not_scalar():
    """Structural pin: a future refactor tempted to make the relationship scalar
    again would fail this first. The name change (payment → payments) is the
    surface signal; the shape check is what actually guards the invariant."""
    from app.models.booking import Booking

    rel = Booking.__mapper__.relationships["payments"]
    assert rel.uselist is True, (
        "Booking.payments must be a list (SVC-186 G29). Reverting to scalar "
        "would re-introduce MultipleResultsFound on any booking with a retry."
    )
