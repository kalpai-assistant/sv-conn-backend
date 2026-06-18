"""
Provider payout tests.
Covers: happy-path payout, duplicate-payout rejection, and payout failure handling.
"""
import uuid
import pytest
from unittest.mock import patch

from sqlalchemy import select

from tests.factories.user_factory import UserFactory
from tests.factories.booking_factory import BookingFactory
from app.models.booking import Booking, BookingStatus, SettlementStatus
from app.models.provider_profile import ProviderProfile
from app.services.payout_service import PayoutService
from app.exceptions import PaymentError
from app.utils.encryption import encrypt


async def _booking_for(db_session, consumer, provider, **kwargs):
    """Create a completed booking with all FK dependencies for the given provider.

    Uses a unique category/service per call so multiple bookings can be created in
    one test without violating the category name/slug unique constraints.
    """
    from app.models.category import Category
    from app.models.service import Service
    from app.models.address import Address

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

    return await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.COMPLETED,
        provider_payout=80.0,
        **kwargs,
    )


async def _provider_with_payout_vpa(db_session, provider):
    profile = ProviderProfile(
        user_id=provider.id,
        payout_vpa_encrypted=encrypt("provider@upi"),
    )
    db_session.add(profile)
    await db_session.flush()
    return profile


@pytest.mark.asyncio
async def test_payout_happy_path(db_session):
    """Completed + PENDING bookings are paid out and flipped to PROCESSING."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    await _provider_with_payout_vpa(db_session, provider)
    b1 = await _booking_for(db_session, consumer, provider)
    b2 = await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout") as mock_payout:
        mock_payout.return_value = {"id": "pout_test_1", "status": "processing"}
        result = await PayoutService(db_session).create_provider_payout(provider.id)

    # One payout for the summed amount, both bookings now PROCESSING.
    mock_payout.assert_called_once()
    _, kwargs = mock_payout.call_args
    assert kwargs["amount_paise"] == 16000  # (80 + 80) * 100
    assert kwargs["vpa"] == "provider@upi"
    assert result["booking_count"] == 2

    for b in (b1, b2):
        await db_session.refresh(b)
        assert b.settlement_status == SettlementStatus.PROCESSING

    # The idempotency key is derived from the batch's booking ids (stable + unique).
    expected_key = PayoutService._batch_idempotency_key([b1, b2])
    assert kwargs["idempotency_key"] == expected_key
    # Order-independent and distinct from a different batch.
    assert PayoutService._batch_idempotency_key([b2, b1]) == expected_key
    assert PayoutService._batch_idempotency_key([b1]) != expected_key


@pytest.mark.asyncio
async def test_payout_no_duplicate(db_session):
    """A second payout finds nothing PENDING and must not pay out again."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    await _provider_with_payout_vpa(db_session, provider)
    await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout") as mock_payout:
        mock_payout.return_value = {"id": "pout_test_2", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id)

        # No PENDING settlements remain → second call raises, no second payout.
        with pytest.raises(PaymentError):
            await PayoutService(db_session).create_provider_payout(provider.id)

    assert mock_payout.call_count == 1


@pytest.mark.asyncio
async def test_payout_failure_webhook(db_session):
    """payout.failed webhook flips the provider's PROCESSING settlements to FAILED."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    await _provider_with_payout_vpa(db_session, provider)
    booking = await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout") as mock_payout:
        mock_payout.return_value = {"id": "pout_test_3", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id)

    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.PROCESSING
    assert booking.settlement_payout_id == "pout_test_3"

    await PayoutService(db_session).handle_payout_webhook(
        event="payout.failed",
        payload={"payout": {"entity": {"id": "pout_test_3"}}},
    )

    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.FAILED


@pytest.mark.asyncio
async def test_payout_webhook_only_affects_its_own_batch(db_session):
    """A payout webhook must settle only its own payout, not other in-flight batches."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    await _provider_with_payout_vpa(db_session, provider)

    # Batch A: one booking, paid out as pout_A.
    b_a = await _booking_for(db_session, consumer, provider)
    with patch("app.services.payout_service.create_payout") as mock_payout:
        mock_payout.return_value = {"id": "pout_A", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id)

    # Batch B: a later booking, paid out as pout_B (both now PROCESSING).
    b_b = await _booking_for(db_session, consumer, provider)
    with patch("app.services.payout_service.create_payout") as mock_payout:
        mock_payout.return_value = {"id": "pout_B", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id)

    # Batch A succeeds — only A's booking should flip to COMPLETED.
    await PayoutService(db_session).handle_payout_webhook(
        event="payout.processed",
        payload={"payout": {"entity": {"id": "pout_A"}}},
    )

    await db_session.refresh(b_a)
    await db_session.refresh(b_b)
    assert b_a.settlement_status == SettlementStatus.COMPLETED
    assert b_b.settlement_status == SettlementStatus.PROCESSING  # untouched by A
