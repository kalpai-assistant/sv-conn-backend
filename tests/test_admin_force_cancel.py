"""SVC-299: admin force-cancel booking coverage.

The consumer cancel path (``PATCH /bookings/{id}/cancel``) is covered by
``test_refunds.py``; this file pins the admin-only variant added in SVC-299:

- POST /admin/bookings/{id}/cancel
  * happy paths from REQUESTED / ACCEPTED / EN_ROUTE / IN_PROGRESS
  * refund inherits the consumer policy — PAID online triggers Razorpay,
    PENDING and cash do not, IN_PROGRESS deliberately does not auto-refund
  * booking.cancellation_reason gets stamped with the reason
  * booking_status_logs gets a row with changed_by = admin.id + note = reason
  * 409 on already-terminal states (CANCELLED / COMPLETED / EXPIRED)
  * 404 on unknown id
  * 403 for non-admin (consumer + provider)
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.booking import (
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.models.booking_status_log import BookingStatusLog
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _seed_booking(
    db,
    *,
    consumer,
    provider,
    status,
    payment_method=PaymentMethod.ONLINE,
    payment_status=PaymentStatus.PAID,
    service_amount=500.0,
    with_captured_payment=True,
    razorpay_payment_id="pay_admin_cancel",
):
    """Booking + optional CAPTURED payment row + Category/Service/Address deps.

    Duplicated shape from test_refunds._seed_booking_with_payment because those
    two files exercise different services (BookingService.force_cancel vs.
    transition_status) and share no fixtures; inlining keeps each file readable
    without a helper that grows a swiss-army-knife signature.
    """
    from app.models.address import Address
    from app.models.category import Category
    from app.models.service import Service

    suffix = uuid.uuid4().hex[:8]
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
        payment_method=payment_method,
        payment_status=payment_status,
        service_amount=service_amount,
        razorpay_order_id=f"order_{suffix}",
    )
    if with_captured_payment:
        payment = Payment(
            booking_id=booking.id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_order_id=booking.razorpay_order_id,
            amount=Decimal(str(service_amount)),
            platform_fee=Decimal(str(service_amount * 0.2)),
            provider_settlement=Decimal(str(service_amount * 0.8)),
            status=PaymentRecordStatus.CAPTURED,
            method=PaymentRecordMethod.UPI,
        )
        db.add(payment)
        await db.flush()
    return booking


async def _fetch_status_logs(db, booking_id):
    rows = await db.execute(
        select(BookingStatusLog)
        .where(BookingStatusLog.booking_id == booking_id)
        .order_by(BookingStatusLog.created_at)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Happy paths — one per legal from-state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_cancel_from_requested_paid_refunds_full(client, auth_headers, db_session):
    """REQUESTED + PAID + online → full refund, no fee (inherited from
    consumer policy). Row goes to CANCELLED, log carries admin id + reason."""
    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.REQUESTED,
    )
    # REQUESTED bookings have no assigned provider — mirror the real shape.
    booking.provider_id = None
    await db_session.flush()

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.payment.refund.return_value = {"id": "rfnd_admin_full"}
        resp = await client.post(
            f"/api/v1/admin/bookings/{booking.id}/cancel",
            headers=auth_headers(str(admin.id), "admin"),
            json={"reason": "duplicate booking flagged by ops"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["payment_status"] == "refund_processing"
    # Full ₹500 → 50000 paise (REQUESTED → no fee).
    assert mock_rzp.payment.refund.call_args.args[1]["amount"] == 50000

    await db_session.refresh(booking)
    assert booking.cancellation_reason == "duplicate booking flagged by ops"

    logs = await _fetch_status_logs(db_session, booking.id)
    assert logs, "force-cancel must write a status log row"
    admin_log = logs[-1]
    assert admin_log.to_status == "cancelled"
    assert admin_log.changed_by == admin.id
    assert admin_log.note == "duplicate booking flagged by ops"
    assert (admin_log.metadata_ or {}).get("admin_override") is True


@pytest.mark.asyncio
async def test_force_cancel_from_accepted_paid_refunds_minus_fee(client, auth_headers, db_session):
    """ACCEPTED + PAID → refund (captured − ₹49), ₹49 credited to provider —
    same policy the consumer cancel path applies."""
    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.ACCEPTED,
        service_amount=500.0,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.payment.refund.return_value = {"id": "rfnd_admin_partial"}
        resp = await client.post(
            f"/api/v1/admin/bookings/{booking.id}/cancel",
            headers=auth_headers(str(admin.id), "admin"),
            json={"reason": "provider unresponsive"},
        )

    assert resp.status_code == 200, resp.text
    # 50000 − 4900 = 45100 (₹500 captured − ₹49 fee).
    assert mock_rzp.payment.refund.call_args.args[1]["amount"] == 45100


@pytest.mark.asyncio
async def test_force_cancel_from_en_route_paid_refunds_minus_fee(client, auth_headers, db_session):
    """EN_ROUTE + PAID → same partial-refund policy as ACCEPTED."""
    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.EN_ROUTE,
        service_amount=500.0,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        mock_rzp.payment.refund.return_value = {"id": "rfnd_admin_enroute"}
        resp = await client.post(
            f"/api/v1/admin/bookings/{booking.id}/cancel",
            headers=auth_headers(str(admin.id), "admin"),
            json={"reason": "provider ghosted"},
        )

    assert resp.status_code == 200, resp.text
    assert mock_rzp.payment.refund.call_args.args[1]["amount"] == 45100


@pytest.mark.asyncio
async def test_force_cancel_from_in_progress_succeeds_without_auto_refund(
    client, auth_headers, db_session
):
    """IN_PROGRESS + PAID → succeed at the state transition (VALID_TRANSITIONS
    does not permit this move via the consumer path, but the admin override
    does). The auto-refund is deliberately skipped — the job has started, so
    ``_apply_cancellation_policy`` falls into its defensive "no auto-refund"
    branch and support refunds manually via the payments surface.
    """
    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.IN_PROGRESS,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        resp = await client.post(
            f"/api/v1/admin/bookings/{booking.id}/cancel",
            headers=auth_headers(str(admin.id), "admin"),
            json={"reason": "customer requested mid-service"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    # payment_status stays PAID because no auto-refund fired.
    assert resp.json()["payment_status"] == "paid"
    mock_rzp.payment.refund.assert_not_called()


# ---------------------------------------------------------------------------
# No-refund cases — payment shape doesn't warrant one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_cancel_pending_online_booking_no_refund_call(client, auth_headers, db_session):
    """PENDING (never captured) online booking cancels cleanly — no Razorpay
    call fires because there's nothing to refund. Matches the ticket's
    "do NOT auto-refund if payment_status is authorized-only or pending"."""
    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.ACCEPTED,
        payment_status=PaymentStatus.PENDING,
        with_captured_payment=False,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        resp = await client.post(
            f"/api/v1/admin/bookings/{booking.id}/cancel",
            headers=auth_headers(str(admin.id), "admin"),
            json={"reason": "unpaid, closing"},
        )

    assert resp.status_code == 200, resp.text
    mock_rzp.payment.refund.assert_not_called()


@pytest.mark.asyncio
async def test_force_cancel_cash_booking_no_refund_call(client, auth_headers, db_session):
    """Cash bookings never had money on our side to refund; force-cancel is
    a pure state change plus the audit row."""
    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.ACCEPTED,
        payment_method=PaymentMethod.CASH,
        payment_status=PaymentStatus.PENDING,
        with_captured_payment=False,
    )

    with patch("app.services.payment_service.razorpay_client") as mock_rzp:
        resp = await client.post(
            f"/api/v1/admin/bookings/{booking.id}/cancel",
            headers=auth_headers(str(admin.id), "admin"),
            json={"reason": "cash booking closed by support"},
        )

    assert resp.status_code == 200, resp.text
    mock_rzp.payment.refund.assert_not_called()


# ---------------------------------------------------------------------------
# Terminal-state refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.EXPIRED],
)
async def test_force_cancel_terminal_state_returns_409(
    client, auth_headers, db_session, terminal_status
):
    """Already-terminal bookings are a 409 with a clear message. Silent
    no-op would let support think they closed a row that was already
    closed — worse than the error."""
    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _seed_booking(
        db_session,
        consumer=consumer,
        provider=provider,
        status=terminal_status,
        with_captured_payment=False,
    )

    resp = await client.post(
        f"/api/v1/admin/bookings/{booking.id}/cancel",
        headers=auth_headers(str(admin.id), "admin"),
        json={"reason": "test terminal"},
    )

    assert resp.status_code == 409, resp.text
    assert terminal_status.value in resp.json()["message"].lower()


# ---------------------------------------------------------------------------
# 404 + RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_cancel_unknown_booking_returns_404(client, auth_headers, db_session):
    admin = await UserFactory.create(db_session, role="admin")
    resp = await client.post(
        f"/api/v1/admin/bookings/{uuid.uuid4()}/cancel",
        headers=auth_headers(str(admin.id), "admin"),
        json={"reason": "test 404"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_force_cancel_rejects_non_admin(client, auth_headers, db_session):
    """Consumer + provider tokens both 403. Same insurance as every other
    admin route — this endpoint is a refund lever, so the gate matters."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")

    r_c = await client.post(
        f"/api/v1/admin/bookings/{uuid.uuid4()}/cancel",
        headers=auth_headers(str(consumer.id), "consumer"),
        json={"reason": "unauthorised"},
    )
    r_p = await client.post(
        f"/api/v1/admin/bookings/{uuid.uuid4()}/cancel",
        headers=auth_headers(str(provider.id), "provider"),
        json={"reason": "unauthorised"},
    )

    assert r_c.status_code == 403, r_c.text
    assert r_p.status_code == 403, r_p.text


@pytest.mark.asyncio
async def test_force_cancel_empty_reason_returns_422(client, auth_headers, db_session):
    """``reason`` is required (min_length=1). An empty payload must fail at
    schema validation, not at the service — otherwise the audit row has a
    blank note and nobody knows why the booking was cancelled."""
    admin = await UserFactory.create(db_session, role="admin")

    resp = await client.post(
        f"/api/v1/admin/bookings/{uuid.uuid4()}/cancel",
        headers=auth_headers(str(admin.id), "admin"),
        json={"reason": ""},
    )

    assert resp.status_code == 422, resp.text
