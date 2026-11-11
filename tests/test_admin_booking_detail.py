"""SVC-425 follow-up: the admin booking detail carries is_disputed,
cancellation_reason and the settlement state; nobody else's response does."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus, PaymentStatus, SettlementStatus
from app.models.booking_dispute import BookingDispute
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.user import AdminRole
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

URL = "/api/v1/admin/bookings"
NEW_FIELDS = ("cancellation_reason", "settlement_status", "settled_at")


async def _headers(db, auth_headers, role: str = "admin") -> dict:
    admin = await UserFactory.create(db, role="admin")
    admin.admin_role = AdminRole(role)
    await db.flush()
    return auth_headers(str(admin.id), "admin")


async def _booking(db, status=BookingStatus.COMPLETED, **kw):
    consumer = await UserFactory.create(db)
    provider = await UserFactory.create(db, role="provider")
    addr = Address(user_id=consumer.id, label="home", full_address="Addr")
    db.add(addr)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=status,
        **kw,
    )
    return booking, consumer


async def _payment(db, booking, disputed: bool):
    db.add(
        Payment(
            booking_id=booking.id,
            razorpay_payment_id=f"pay_{uuid.uuid4().hex[:10]}",
            razorpay_order_id=f"order_{uuid.uuid4().hex[:10]}",
            amount=Decimal("100"),
            platform_fee=Decimal("20"),
            provider_settlement=Decimal("80"),
            status=PaymentRecordStatus.CAPTURED,
            method=PaymentRecordMethod.UPI,
            disputed_at=datetime.now(UTC) if disputed else None,
        )
    )
    await db.flush()


@pytest.mark.parametrize("disputed", [True, False])
async def test_detail_is_disputed(client, db_session, auth_headers, disputed):
    headers = await _headers(db_session, auth_headers)
    booking, _ = await _booking(db_session)
    # An undisputed payment alongside, so "any payment" is what's being tested.
    await _payment(db_session, booking, disputed=False)
    if disputed:
        await _payment(db_session, booking, disputed=True)

    r = await client.get(f"{URL}/{booking.id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["is_disputed"] is disputed


async def test_detail_shows_cancellation_reason(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    booking, _ = await _booking(db_session, status=BookingStatus.CANCELLED)
    booking.cancellation_reason = "Customer changed plans"
    await db_session.flush()

    body = (await client.get(f"{URL}/{booking.id}", headers=headers)).json()
    assert body["cancellation_reason"] == "Customer changed plans"
    assert body["settlement_status"] == "pending"
    assert body["settled_at"] is None


async def test_force_cancel_response_carries_reason_and_dispute(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    booking, _ = await _booking(
        db_session, status=BookingStatus.REQUESTED, payment_status=PaymentStatus.PENDING
    )
    await _payment(db_session, booking, disputed=True)

    r = await client.post(
        f"{URL}/{booking.id}/cancel", json={"reason": "Fraud suspected"}, headers=headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancellation_reason"] == "Fraud suspected"
    assert body["is_disputed"] is True


async def test_detail_shows_settlement(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    booking, _ = await _booking(db_session, payment_status=PaymentStatus.PAID)
    settled = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    booking.settlement_status = SettlementStatus.COMPLETED
    booking.settled_at = settled
    await db_session.flush()

    body = (await client.get(f"{URL}/{booking.id}", headers=headers)).json()
    assert body["settlement_status"] == "completed"
    assert datetime.fromisoformat(body["settled_at"]) == settled
    assert body["cancellation_reason"] is None


async def test_consumer_view_leaves_admin_fields_unset(client, db_session, auth_headers):
    booking, consumer = await _booking(
        db_session, status=BookingStatus.CANCELLED, payment_status=PaymentStatus.PAID
    )
    booking.cancellation_reason = "Booked by mistake"
    booking.settlement_status = SettlementStatus.COMPLETED
    booking.settled_at = datetime.now(UTC)
    await _payment(db_session, booking, disputed=True)

    r = await client.get(f"/api/v1/bookings/{booking.id}", headers=auth_headers(str(consumer.id)))
    assert r.status_code == 200, r.text
    body = r.json()
    for field in NEW_FIELDS:
        assert body[field] is None, field
    assert body["is_disputed"] is False


async def test_list_rows_leave_admin_detail_fields_unset(client, db_session, auth_headers):
    """The list is unchanged: only the detail fills these."""
    headers = await _headers(db_session, auth_headers)
    booking, consumer = await _booking(db_session, status=BookingStatus.CANCELLED)
    booking.cancellation_reason = "Booked by mistake"
    await db_session.flush()

    r = await client.get(URL, params={"consumer_phone": consumer.phone}, headers=headers)
    (row,) = r.json()["bookings"]
    for field in NEW_FIELDS:
        assert row[field] is None, field


@pytest.mark.parametrize("role", ["admin", "ops", "support"])
async def test_every_admin_role_reads_detail(client, db_session, auth_headers, role):
    headers = await _headers(db_session, auth_headers, role)
    booking, _ = await _booking(db_session)
    r = await client.get(f"{URL}/{booking.id}", headers=headers)
    assert r.status_code == 200
    assert "settlement_status" in r.json()


async def test_consumer_cannot_read_admin_detail(client, db_session, auth_headers):
    booking, consumer = await _booking(db_session)
    r = await client.get(f"{URL}/{booking.id}", headers=auth_headers(str(consumer.id)))
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("chargeback", "dispute_status", "expected"),
    [
        (True, None, True),
        (False, "RAISED", True),
        (False, "CLOSED", True),
        (False, None, False),
    ],
    ids=["chargeback-only", "open-dispute-only", "closed-dispute-only", "neither"],
)
async def test_detail_is_disputed_counts_customer_disputes(
    client, db_session, auth_headers, chargeback, dispute_status, expected
):
    """SVC-431: a customer dispute (any status) marks the booking disputed too."""
    headers = await _headers(db_session, auth_headers)
    booking, consumer = await _booking(db_session)
    await _payment(db_session, booking, disputed=chargeback)
    if dispute_status:
        db_session.add(
            BookingDispute(
                booking_id=booking.id,
                raised_by_user_id=consumer.id,
                reason_code="QUALITY",
                description="Customer reported an issue",
                status=dispute_status,
                resolution_type="NO_REFUND" if dispute_status == "CLOSED" else None,
            )
        )
        await db_session.flush()

    r = await client.get(f"{URL}/{booking.id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["is_disputed"] is expected
