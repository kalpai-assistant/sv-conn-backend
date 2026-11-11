"""
SVC-431: disputes + refunds for the admin console (/admin/disputes/*).

Pins:

- the ticket's AC: resolve writes the audit row, calls Razorpay with the paise
  amount, and marks the dispute RESOLVED;
- Razorpay refuses (422) or doesn't answer (502): nothing saved; a retry adopts
  the refund already made for this dispute instead of refunding again;
- a second resolve is a 409 and Razorpay is called once;
- support's cap from ``pricing_config.support_refund_limit``; admin uncapped;
- cash: a manual refund, no Razorpay call, notes required, closes at once;
- after a cancel-fee refund only the fee is left, and ``payments.refund_id`` is
  untouched; nothing left → only NO_REFUND;
- one open dispute per booking; disputes only on finished bookings;
- **provider invariant**: a dispute refund (and its webhook) leaves the booking
  in ``_owed_filter`` with the same payout amount;
- the refund webhooks close / fail the dispute, found by ``notes.dispute_id``
  or by refund id, and never touch the cancel-refund fields;
- the customer push, enqueued only after commit.

Razorpay is always mocked (``app.services.payment_service.razorpay_client``).
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from razorpay.errors import BadRequestError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import _run_after_commit
from app.models.admin_action import ADMIN_ACTION_TARGET_TYPES, ADMIN_ACTIONS, AdminAction
from app.models.booking import Booking, BookingStatus, PaymentMethod, PaymentStatus
from app.models.booking_dispute import BookingDispute
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.pricing_config import PricingConfig
from app.models.provider_ledger_entry import ProviderLedgerEntry
from app.models.user import AdminRole
from app.services.payment_service import PaymentService
from app.services.payout_service import PayoutService
from app.tasks import notification_tasks
from tests.factories.user_factory import UserFactory
from tests.test_admin_console_auth import _admin, _session
from tests.test_refunds import _seed_booking_with_payment

A = "/api/v1/admin/disputes"
RZP = "app.services.payment_service.razorpay_client"

pytestmark = pytest.mark.asyncio


# -- fixtures -------------------------------------------------------------------


@pytest.fixture
async def admins(db_session):
    """Made first: ``_admin`` commits, and nothing else in a test should be."""
    return SimpleNamespace(
        admin=await _admin(db_session, AdminRole.ADMIN),
        ops=await _admin(db_session, AdminRole.OPS),
        support=await _admin(db_session, AdminRole.SUPPORT),
    )


@pytest.fixture
async def limit_500(db_session, admins):
    await db_session.merge(PricingConfig(key="support_refund_limit", value_json=500))
    await db_session.flush()


@pytest.fixture
def rzp():
    with patch(RZP) as mock:
        mock.payment.fetch_multiple_refund.return_value = {"count": 0, "items": []}
        mock.payment.refund.side_effect = lambda pay_id, data: {
            "id": f"rfnd_{uuid.uuid4().hex[:12]}",
            "amount": data["amount"],
            "status": "pending",
            "notes": data["notes"],
        }
        yield mock


async def _online(db, *, status=BookingStatus.COMPLETED, service_amount=500.0, **kw):
    consumer = await UserFactory.create(db, role="consumer")
    provider = await UserFactory.create(db, role="provider")
    booking, payment = await _seed_booking_with_payment(
        db,
        consumer=consumer,
        provider=provider,
        status=status,
        service_amount=service_amount,
        razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
        **kw,
    )
    return booking, payment


async def _cancelled_after_accept(db, service_amount=500.0, fee=49.0):
    """Cancelled from ACCEPTED: the cancel refunded everything but the fee, and
    its webhook landed (payment REFUNDED, booking REFUNDED)."""
    booking, payment = await _online(
        db,
        status=BookingStatus.CANCELLED,
        service_amount=service_amount,
        payment_status=PaymentStatus.REFUNDED,
        payment_status_row=PaymentRecordStatus.REFUNDED,
    )
    payment.refund_id = "rfnd_cancel_" + uuid.uuid4().hex[:8]
    payment.refund_amount_paise = int((service_amount - fee) * 100)
    payment.refunded_at = datetime.now(UTC)
    await db.flush()
    return booking, payment


async def _cash(db, service_amount=400.0):
    consumer = await UserFactory.create(db, role="consumer")
    provider = await UserFactory.create(db, role="provider")
    booking, _ = await _seed_booking_with_payment(
        db,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.COMPLETED,
        payment_method=PaymentMethod.CASH,
        service_amount=service_amount,
        with_payment=False,
    )
    payment = Payment(
        booking_id=booking.id,
        amount=Decimal(str(service_amount)),
        platform_fee=Decimal("0"),
        provider_settlement=Decimal("0"),
        status=PaymentRecordStatus.CAPTURED,
        method=PaymentRecordMethod.CASH,
    )
    db.add(payment)
    await db.flush()
    return booking, payment


async def _raise(client, admin, booking, reason="NO_SHOW"):
    r = await client.post(
        A,
        json={"booking_id": str(booking.id), "reason_code": reason, "description": "Nobody came"},
        headers=_session(admin),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _resolve(client, admin, dispute_id, resolution_type, amount=None, notes="Checked"):
    body = {"resolution_type": resolution_type, "notes": notes}
    if amount is not None:
        body["refund_amount"] = amount
    return await client.post(f"{A}/{dispute_id}/resolve", json=body, headers=_session(admin))


async def _dispute(db, dispute_id) -> BookingDispute:
    await db.flush()
    row = await db.get(BookingDispute, uuid.UUID(str(dispute_id)))
    await db.refresh(row)
    return row


async def _audit(db, dispute_id) -> list[AdminAction]:
    await db.flush()
    rows = await db.scalars(
        select(AdminAction).where(
            AdminAction.target_type == "dispute", AdminAction.target_id == str(dispute_id)
        )
    )
    # One test transaction stamps every row with the same now(), so sort by the
    # lifecycle instead.
    order = ("dispute.create", "dispute.review", "dispute.resolve")
    return sorted(rows, key=lambda a: order.index(a.action))


def _assert_error(r, status_code, code):
    assert r.status_code == status_code, r.text
    assert r.json()["code"] == code, r.text


def _webhook(event, refund_id, dispute_id=None, **entity):
    notes = {"dispute_id": str(dispute_id)} if dispute_id else {}
    return {"refund": {"entity": {"id": refund_id, "notes": notes, **entity}}}


# -- the AC ---------------------------------------------------------------------


async def test_resolve_refunds_on_razorpay_audits_and_marks_resolved(
    client, db_session, admins, rzp
):
    """The ticket's AC: audit + Razorpay with the paise amount + RESOLVED."""
    booking, payment = await _online(db_session, service_amount=500.0)
    dispute = await _raise(client, admins.ops, booking)

    r = await _resolve(client, admins.ops, dispute["id"], "FULL_REFUND", notes="No-show confirmed")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "RESOLVED"
    assert body["resolution_type"] == "FULL_REFUND"
    assert body["refund_amount"] == 500.0
    assert body["refund_channel"] == "RAZORPAY"
    assert body["refund_status"] == "PENDING"
    assert body["razorpay_refund_id"].startswith("rfnd_")
    assert body["resolved_by"] == str(admins.ops.id)

    rzp.payment.refund.assert_called_once()
    pay_id, data = rzp.payment.refund.call_args.args
    assert pay_id == payment.razorpay_payment_id
    assert data["amount"] == 50000
    assert data["notes"]["dispute_id"] == dispute["id"]

    actions = [a.action for a in await _audit(db_session, dispute["id"])]
    assert actions == ["dispute.create", "dispute.resolve"]
    [resolve] = [
        a for a in await _audit(db_session, dispute["id"]) if a.action == "dispute.resolve"
    ]
    assert resolve.admin_user_id == admins.ops.id
    assert resolve.after_json["status"] == "RESOLVED"
    assert resolve.after_json["refund_amount"] == 500.0
    assert resolve.details["refund_amount_paise"] == 50000


async def test_partial_refund_sends_the_typed_amount(client, db_session, admins, rzp):
    booking, _ = await _online(db_session, service_amount=500.0)
    dispute = await _raise(client, admins.admin, booking, reason="QUALITY")
    r = await _resolve(client, admins.admin, dispute["id"], "PARTIAL_REFUND", amount=120.5)
    assert r.status_code == 200, r.text
    assert r.json()["refund_amount"] == 120.5
    assert rzp.payment.refund.call_args.args[1]["amount"] == 12050


async def test_full_refund_with_a_stale_amount_is_refused(client, db_session, admins, rzp):
    booking, _ = await _online(db_session, service_amount=500.0)
    dispute = await _raise(client, admins.admin, booking)
    r = await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND", amount=450)
    _assert_error(r, 422, "DISPUTE_INVALID")
    assert r.json()["details"] == {"field": "refund_amount", "refundable": 500.0}
    rzp.payment.refund.assert_not_called()


@pytest.mark.parametrize(
    ("resolution_type", "amount"),
    [("PARTIAL_REFUND", None), ("PARTIAL_REFUND", 0), ("PARTIAL_REFUND", -5),
     ("PARTIAL_REFUND", 10.005), ("NO_REFUND", 10)],
)  # fmt: skip
async def test_bad_amounts_are_refused(client, db_session, admins, rzp, resolution_type, amount):
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    r = await _resolve(client, admins.admin, dispute["id"], resolution_type, amount=amount)
    _assert_error(r, 422, "DISPUTE_INVALID")
    assert r.json()["details"]["field"] == "refund_amount"
    rzp.payment.refund.assert_not_called()
    assert (await _dispute(db_session, dispute["id"])).status == "RAISED"


async def test_notes_are_required(client, db_session, admins, rzp):
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    r = await _resolve(client, admins.admin, dispute["id"], "NO_REFUND", notes="   ")
    assert r.status_code == 422, r.text
    rzp.payment.refund.assert_not_called()


# -- Razorpay refusals, retries, double clicks ----------------------------------


async def test_razorpay_rejection_is_a_422_and_nothing_changes(client, db_session, admins, rzp):
    booking, payment = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    rzp.payment.refund.side_effect = BadRequestError("The refund amount is invalid")

    r = await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")
    _assert_error(r, 422, "REFUND_REJECTED")
    assert "The refund amount is invalid" in r.json()["details"]["razorpay_error"]

    row = await _dispute(db_session, dispute["id"])
    assert (row.status, row.resolution_type, row.refund_amount, row.razorpay_refund_id) == (
        "RAISED",
        None,
        None,
        None,
    )
    assert [a.action for a in await _audit(db_session, dispute["id"])] == ["dispute.create"]
    await db_session.refresh(payment)
    assert payment.refund_id is None


async def test_a_timeout_is_a_502_and_the_retry_adopts_the_refund(client, db_session, admins, rzp):
    """Razorpay made the refund but the answer never came back. The retry finds
    it by notes.dispute_id and records it, without a second refund call."""
    booking, _ = await _online(db_session, service_amount=500.0)
    dispute = await _raise(client, admins.admin, booking)
    rzp.payment.refund.side_effect = TimeoutError("read timed out")

    r = await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")
    _assert_error(r, 502, "REFUND_GATEWAY_ERROR")
    assert (await _dispute(db_session, dispute["id"])).status == "RAISED"

    rzp.payment.refund.reset_mock()
    rzp.payment.fetch_multiple_refund.return_value = {
        "count": 2,
        "items": [
            {"id": "rfnd_other", "amount": 4900, "notes": {"reason": "cancellation"}},
            {
                "id": "rfnd_made_before",
                "amount": 50000,
                "status": "pending",
                "notes": {"dispute_id": dispute["id"]},
            },
        ],
    }
    r = await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")
    assert r.status_code == 200, r.text
    assert r.json()["razorpay_refund_id"] == "rfnd_made_before"
    assert r.json()["refund_amount"] == 500.0
    rzp.payment.refund.assert_not_called()


async def test_a_second_resolve_is_refused_and_razorpay_is_called_once(
    client, db_session, admins, rzp
):
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    assert (await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")).status_code == 200

    r = await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")
    _assert_error(r, 409, "DISPUTE_INVALID_STATE")
    assert rzp.payment.refund.call_count == 1


async def test_resolve_locks_the_dispute_and_the_payment(client, db_session, admins, rzp):
    """The double-click guard is the row locks; pin that both are taken."""
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    statements: list[str] = []

    from sqlalchemy import event

    def capture(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert (await _resolve(client, admins.admin, dispute["id"], "NO_REFUND")).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    locked = [s for s in statements if "FOR UPDATE" in s]
    assert any("FROM booking_disputes" in s for s in locked)
    assert any("FROM payments" in s for s in locked)


# -- support cap ----------------------------------------------------------------


async def test_support_can_refund_up_to_the_limit(client, db_session, admins, limit_500, rzp):
    booking, _ = await _online(db_session, service_amount=800.0)
    dispute = await _raise(client, admins.support, booking)
    r = await _resolve(client, admins.support, dispute["id"], "PARTIAL_REFUND", amount=500)
    assert r.status_code == 200, r.text


async def test_support_above_the_limit_is_a_403(client, db_session, admins, limit_500, rzp):
    booking, _ = await _online(db_session, service_amount=800.0)
    dispute = await _raise(client, admins.support, booking)
    r = await _resolve(client, admins.support, dispute["id"], "PARTIAL_REFUND", amount=500.01)
    _assert_error(r, 403, "REFUND_OVER_SUPPORT_LIMIT")
    assert r.json()["details"]["limit"] == 500.0
    # A full refund counts too.
    r = await _resolve(client, admins.support, dispute["id"], "FULL_REFUND")
    _assert_error(r, 403, "REFUND_OVER_SUPPORT_LIMIT")
    rzp.payment.refund.assert_not_called()
    # Support can still close it without a refund.
    assert (await _resolve(client, admins.support, dispute["id"], "NO_REFUND")).status_code == 200


async def test_the_limit_comes_from_pricing_config(client, db_session, admins, rzp):
    await db_session.merge(PricingConfig(key="support_refund_limit", value_json=100))
    await db_session.flush()
    booking, _ = await _online(db_session, service_amount=800.0)
    dispute = await _raise(client, admins.support, booking)
    r = await _resolve(client, admins.support, dispute["id"], "PARTIAL_REFUND", amount=150)
    _assert_error(r, 403, "REFUND_OVER_SUPPORT_LIMIT")
    assert r.json()["details"]["limit"] == 100.0


@pytest.mark.parametrize("role", ["admin", "ops"])
async def test_admin_and_ops_are_uncapped(client, db_session, admins, limit_500, rzp, role):
    booking, _ = await _online(db_session, service_amount=5000.0)
    dispute = await _raise(client, getattr(admins, role), booking)
    r = await _resolve(client, getattr(admins, role), dispute["id"], "FULL_REFUND")
    assert r.status_code == 200, r.text
    assert rzp.payment.refund.call_args.args[1]["amount"] == 500000


# -- cash -----------------------------------------------------------------------


async def test_cash_refund_is_manual_and_never_calls_razorpay(client, db_session, admins, rzp):
    booking, _ = await _cash(db_session, service_amount=400.0)
    dispute = await _raise(client, admins.admin, booking, reason="BILLING")
    r = await _resolve(
        client,
        admins.admin,
        dispute["id"],
        "PARTIAL_REFUND",
        amount=150,
        notes="Paid back by UPI to 98xxxxxx01, ref 4521",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["refund_channel"], body["refund_status"]) == (
        "CLOSED",
        "MANUAL",
        None,
    )
    assert body["refund_amount"] == 150.0
    assert body["resolution_notes"] == "Paid back by UPI to 98xxxxxx01, ref 4521"
    assert body["closed_at"] is not None
    rzp.payment.refund.assert_not_called()
    rzp.payment.fetch_multiple_refund.assert_not_called()

    # 250 is left; a second dispute can't take more.
    second = await _raise(client, admins.admin, booking)
    r = await _resolve(client, admins.admin, second["id"], "PARTIAL_REFUND", amount=250.01)
    _assert_error(r, 422, "DISPUTE_INVALID")
    assert r.json()["details"]["refundable"] == 250.0


async def test_cash_refundable_is_what_the_customer_was_billed(client, db_session, admins, rzp):
    """A coupon booking: billed service − discount, though the cash row says
    service_amount."""
    booking, _ = await _cash(db_session, service_amount=400.0)
    booking.discount_amount = Decimal("50.00")
    await db_session.flush()
    r = await client.get(f"{A}/bookings/{booking.id}", headers=_session(admins.support))
    assert r.json()["refunds"]["refundable"] == 350.0


async def test_cash_booking_not_yet_paid_has_nothing_to_refund(client, db_session, admins, rzp):
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking, _ = await _seed_booking_with_payment(
        db_session,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.CANCELLED,
        payment_method=PaymentMethod.CASH,
        payment_status=PaymentStatus.PENDING,
        with_payment=False,
    )
    dispute = await _raise(client, admins.admin, booking)
    r = await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")
    _assert_error(r, 409, "NOTHING_TO_REFUND")


# -- refunds on top of a cancel refund --------------------------------------------


async def test_after_a_cancel_fee_only_the_fee_is_refundable(client, db_session, admins, rzp):
    """The common case: cancelled after acceptance, ₹49 kept, then disputed as a
    no-show. ₹49 is OK, ₹49.01 isn't, and the cancel refund's slot is untouched."""
    booking, payment = await _cancelled_after_accept(db_session, service_amount=500.0, fee=49.0)
    cancel_refund_id = payment.refund_id
    dispute = await _raise(client, admins.support, booking)

    r = await _resolve(client, admins.support, dispute["id"], "PARTIAL_REFUND", amount=49.01)
    _assert_error(r, 422, "DISPUTE_INVALID")
    assert r.json()["details"]["refundable"] == 49.0

    r = await _resolve(client, admins.support, dispute["id"], "PARTIAL_REFUND", amount=49)
    assert r.status_code == 200, r.text
    assert rzp.payment.refund.call_args.args == (
        payment.razorpay_payment_id,
        rzp.payment.refund.call_args.args[1],
    )
    assert rzp.payment.refund.call_args.args[1]["amount"] == 4900

    await db_session.flush()
    await db_session.refresh(payment)
    await db_session.refresh(booking)
    assert payment.refund_id == cancel_refund_id
    assert payment.refund_amount_paise == 45100
    assert payment.status is PaymentRecordStatus.REFUNDED
    assert booking.payment_status is PaymentStatus.REFUNDED


async def test_a_cancel_refund_still_processing_counts(client, db_session, admins, rzp):
    """Between the cancel and its webhook the payment row is still CAPTURED."""
    booking, payment = await _online(
        db_session,
        status=BookingStatus.CANCELLED,
        payment_status=PaymentStatus.REFUND_PROCESSING,
    )
    payment.refund_id = "rfnd_cancel_pending"
    payment.refund_amount_paise = 45100
    await db_session.flush()
    r = await client.get(f"{A}/bookings/{booking.id}", headers=_session(admins.admin))
    assert r.json()["refunds"]["refundable"] == 49.0


async def test_nothing_left_allows_only_no_refund(client, db_session, admins, rzp):
    booking, _ = await _cancelled_after_accept(db_session, service_amount=500.0, fee=0.0)
    dispute = await _raise(client, admins.admin, booking)
    for kind, amount in (("FULL_REFUND", None), ("PARTIAL_REFUND", 1)):
        r = await _resolve(client, admins.admin, dispute["id"], kind, amount=amount)
        _assert_error(r, 409, "NOTHING_TO_REFUND")
    r = await _resolve(client, admins.admin, dispute["id"], "NO_REFUND", notes="Already refunded")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CLOSED"
    rzp.payment.refund.assert_not_called()


async def test_earlier_dispute_refunds_count_and_failed_ones_dont(client, db_session, admins, rzp):
    booking, _ = await _online(db_session, service_amount=500.0)
    first = await _raise(client, admins.admin, booking)
    assert (
        await _resolve(client, admins.admin, first["id"], "PARTIAL_REFUND", amount=200)
    ).status_code == 200
    r = await client.get(f"{A}/bookings/{booking.id}", headers=_session(admins.admin))
    assert r.json()["refunds"]["dispute_refunds"] == 200.0
    assert r.json()["refunds"]["refundable"] == 300.0

    row = await _dispute(db_session, first["id"])
    await PaymentService(db_session).handle_webhook(
        "refund.failed", _webhook("refund.failed", row.razorpay_refund_id, row.id)
    )
    r = await client.get(f"{A}/bookings/{booking.id}", headers=_session(admins.admin))
    assert r.json()["refunds"]["refundable"] == 500.0


# -- one open dispute; which bookings ---------------------------------------------


async def test_one_open_dispute_per_booking(client, db_session, admins):
    booking, _ = await _online(db_session)
    first = await _raise(client, admins.admin, booking)
    r = await client.post(
        A,
        json={"booking_id": str(booking.id), "reason_code": "OTHER", "description": "Again"},
        headers=_session(admins.support),
    )
    _assert_error(r, 409, "DISPUTE_ALREADY_OPEN")
    assert r.json()["details"]["dispute_id"] == first["id"]

    # Closed, a new one may be logged.
    assert (await _resolve(client, admins.admin, first["id"], "NO_REFUND")).status_code == 200
    await _raise(client, admins.admin, booking, reason="OTHER")


async def test_the_database_allows_one_open_dispute(db_session):
    booking, _ = await _online(db_session)

    def row(status):
        return BookingDispute(
            booking_id=booking.id,
            raised_by_user_id=booking.consumer_id,
            reason_code="OTHER",
            description="x",
            status=status,
        )

    db_session.add_all([row("CLOSED"), row("CLOSED"), row("RAISED")])
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(row("IN_REVIEW"))
            await db_session.flush()


@pytest.mark.parametrize(
    "status",
    [BookingStatus.REQUESTED, BookingStatus.ACCEPTED, BookingStatus.IN_PROGRESS],
)
async def test_only_finished_bookings_can_be_disputed(client, db_session, admins, status):
    booking, _ = await _online(db_session, status=status)
    r = await client.post(
        A,
        json={"booking_id": str(booking.id), "reason_code": "OTHER", "description": "x"},
        headers=_session(admins.admin),
    )
    _assert_error(r, 409, "DISPUTE_BOOKING_NOT_FINISHED")


async def test_create_on_a_missing_booking_is_404(client, db_session, admins):
    r = await client.post(
        A,
        json={"booking_id": str(uuid.uuid4()), "reason_code": "OTHER", "description": "x"},
        headers=_session(admins.admin),
    )
    _assert_error(r, 404, "BOOKING_NOT_FOUND")


async def test_create_refuses_unknown_fields_and_reasons(client, db_session, admins):
    booking, _ = await _online(db_session)
    for body in (
        {"booking_id": str(booking.id), "reason_code": "LATE", "description": "x"},
        {"booking_id": str(booking.id), "reason_code": "OTHER", "description": " "},
        {"booking_id": str(booking.id), "reason_code": "OTHER", "description": "x", "status": "CLOSED"},
    ):  # fmt: skip
        r = await client.post(A, json=body, headers=_session(admins.admin))
        assert r.status_code == 422, r.text


# -- review ---------------------------------------------------------------------


async def test_pick_up_moves_raised_to_in_review_and_audits(client, db_session, admins, rzp):
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.support, booking)
    r = await client.post(f"{A}/{dispute['id']}/review", headers=_session(admins.support))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "IN_REVIEW"

    r = await client.post(f"{A}/{dispute['id']}/review", headers=_session(admins.support))
    _assert_error(r, 409, "DISPUTE_INVALID_STATE")

    [create, review] = await _audit(db_session, dispute["id"])
    assert (create.action, review.action) == ("dispute.create", "dispute.review")
    assert (review.before_json["status"], review.after_json["status"]) == ("RAISED", "IN_REVIEW")
    assert "description" not in create.after_json

    # Resolve works from IN_REVIEW too.
    r = await _resolve(client, admins.support, dispute["id"], "PARTIAL_REFUND", amount=10)
    assert r.status_code == 200, r.text


async def test_unknown_dispute_is_404(client, db_session, admins):
    r = await client.post(f"{A}/{uuid.uuid4()}/review", headers=_session(admins.admin))
    _assert_error(r, 404, "DISPUTE_NOT_FOUND")
    r = await _resolve(client, admins.admin, uuid.uuid4(), "NO_REFUND")
    _assert_error(r, 404, "DISPUTE_NOT_FOUND")


# -- provider invariant -------------------------------------------------------------


async def _owed(db, booking) -> tuple[bool, Decimal]:
    await db.flush()
    await db.refresh(booking)
    in_filter = await db.scalar(
        select(Booking.id).where(Booking.id == booking.id, *PayoutService._owed_filter())
    )
    return in_filter is not None, PayoutService._owed_amount(booking)


async def test_a_dispute_refund_never_changes_the_providers_payout(client, db_session, admins, rzp):
    """The platform absorbs dispute refunds. A COMPLETED online booking waiting
    for its payout stays owed, for the same amount, after the refund AND after
    the refund.processed webhook; no ledger rows; payments untouched."""
    booking, payment = await _online(db_session, service_amount=500.0)
    before = await _owed(db_session, booking)
    assert before == (True, booking.provider_payout + booking.tip_amount)
    ledger_before = await db_session.scalar(
        select(ProviderLedgerEntry.id).where(ProviderLedgerEntry.reference_id == booking.id)
    )

    dispute = await _raise(client, admins.admin, booking)
    r = await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")
    assert r.status_code == 200, r.text
    assert await _owed(db_session, booking) == before

    await PaymentService(db_session).handle_webhook(
        "refund.processed",
        _webhook("refund.processed", r.json()["razorpay_refund_id"], dispute["id"]),
    )
    assert await _owed(db_session, booking) == before
    assert booking.payment_status is PaymentStatus.PAID

    await db_session.refresh(payment)
    assert payment.status is PaymentRecordStatus.CAPTURED
    assert (payment.refund_id, payment.refund_amount_paise, payment.refunded_at) == (
        None,
        None,
        None,
    )
    assert ledger_before is None
    assert (
        await db_session.scalar(
            select(ProviderLedgerEntry.id).where(ProviderLedgerEntry.reference_id == booking.id)
        )
        is None
    )


# -- webhooks -------------------------------------------------------------------------


async def test_refund_processed_closes_the_dispute_found_by_its_notes(
    client, db_session, admins, rzp
):
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    refund_id = (await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")).json()[
        "razorpay_refund_id"
    ]

    payload = _webhook("refund.processed", refund_id, dispute["id"])
    await PaymentService(db_session).handle_webhook("refund.processed", payload)
    row = await _dispute(db_session, dispute["id"])
    assert (row.status, row.refund_status) == ("CLOSED", "PROCESSED")
    closed_at = row.closed_at
    assert closed_at is not None

    # A redelivery changes nothing.
    await PaymentService(db_session).handle_webhook("refund.processed", payload)
    row = await _dispute(db_session, dispute["id"])
    assert (row.status, row.closed_at) == ("CLOSED", closed_at)


async def test_the_webhook_locks_the_dispute_it_names(client, db_session, admins, rzp):
    """Razorpay can deliver before the resolve commits; the lock on the dispute
    named in the notes waits for that commit."""
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    refund_id = (await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")).json()[
        "razorpay_refund_id"
    ]
    statements: list[str] = []

    from sqlalchemy import event

    def capture(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await PaymentService(db_session).handle_webhook(
            "refund.processed", _webhook("refund.processed", refund_id, dispute["id"])
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    first = next(s for s in statements if "booking_disputes" in s)
    assert "FOR UPDATE" in first and "booking_disputes.id = " in first
    assert not any("payments.refund_id" in s for s in statements)


async def test_refund_processed_without_notes_falls_back_to_the_refund_id(
    client, db_session, admins, rzp
):
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    refund_id = (await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")).json()[
        "razorpay_refund_id"
    ]
    await PaymentService(db_session).handle_webhook(
        "refund.processed", _webhook("refund.processed", refund_id)
    )
    assert (await _dispute(db_session, dispute["id"])).status == "CLOSED"


async def test_refund_failed_stays_resolved_and_shows_the_failure(client, db_session, admins, rzp):
    booking, payment = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    refund_id = (await _resolve(client, admins.admin, dispute["id"], "FULL_REFUND")).json()[
        "razorpay_refund_id"
    ]
    await PaymentService(db_session).handle_webhook(
        "refund.failed",
        _webhook("refund.failed", refund_id, dispute["id"], error_description="Account closed"),
    )
    await db_session.flush()
    await db_session.refresh(booking)
    assert booking.payment_status is PaymentStatus.PAID  # not REFUND_FAILED

    r = await client.get(f"{A}?status=RESOLVED", headers=_session(admins.admin))
    [row] = [d for d in r.json()["items"] if d["id"] == dispute["id"]]
    assert (row["status"], row["refund_status"], row["refund_failed_reason"]) == (
        "RESOLVED",
        "FAILED",
        "Account closed",
    )
    detail = (await client.get(f"{A}/bookings/{booking.id}", headers=_session(admins.admin))).json()
    assert detail["disputes"][0]["refund_status"] == "FAILED"


async def test_a_refund_naming_an_unknown_dispute_is_acked_not_treated_as_a_cancel(
    db_session,
):
    booking, payment = await _online(db_session, status=BookingStatus.CANCELLED)
    payment.refund_id = "rfnd_shared_id"
    payment.refund_amount_paise = 1000
    booking.payment_status = PaymentStatus.REFUND_PROCESSING
    await db_session.flush()
    await PaymentService(db_session).handle_webhook(
        "refund.processed", _webhook("refund.processed", "rfnd_shared_id", uuid.uuid4())
    )
    await db_session.flush()
    await db_session.refresh(booking)
    assert booking.payment_status is PaymentStatus.REFUND_PROCESSING


async def test_the_cancel_refund_webhook_still_settles_the_payment(db_session):
    """No notes, no dispute row: the cancel path runs as before."""
    booking, payment = await _online(
        db_session, status=BookingStatus.CANCELLED, payment_status=PaymentStatus.REFUND_PROCESSING
    )
    payment.refund_id = "rfnd_cancel_" + uuid.uuid4().hex[:8]
    payment.refund_amount_paise = 50000
    await db_session.flush()
    await PaymentService(db_session).handle_webhook(
        "refund.processed",
        {"refund": {"entity": {"id": payment.refund_id, "notes": {"reason": "cancellation"}}}},
    )
    await db_session.flush()
    await db_session.refresh(booking)
    await db_session.refresh(payment)
    assert booking.payment_status is PaymentStatus.REFUNDED
    assert payment.status is PaymentRecordStatus.REFUNDED


# -- reads ----------------------------------------------------------------------------


async def test_detail_shows_the_booking_refunds_and_every_dispute(client, db_session, admins, rzp):
    booking, payment = await _cancelled_after_accept(db_session, service_amount=500.0, fee=49.0)
    first = await _raise(client, admins.admin, booking, reason="BILLING")
    await _resolve(client, admins.admin, first["id"], "NO_REFUND", notes="Fee was right")
    second = await _raise(client, admins.admin, booking, reason="NO_SHOW")
    # One test transaction gives both rows the same now(); in production each
    # create is its own transaction.
    row = await db_session.get(BookingDispute, uuid.UUID(second["id"]))
    row.created_at = datetime.now(UTC) + timedelta(seconds=1)
    await db_session.flush()

    r = await client.get(f"{A}/bookings/{booking.id}", headers=_session(admins.support))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["booking"]["id"] == str(booking.id)
    assert body["booking"]["status"] == "cancelled"
    assert body["booking"]["payment_method"] == "online"
    assert body["booking"]["payment_status"] == "refunded"
    assert body["booking"]["customer"]["id"] == str(booking.consumer_id)
    assert body["booking"]["provider"]["id"] == str(booking.provider_id)
    assert body["refunds"] == {
        "channel": "RAZORPAY",
        "captured": 500.0,
        "cancel_refund": 451.0,
        "cancel_refund_id": payment.refund_id,
        "cancel_refunded_at": body["refunds"]["cancel_refunded_at"],
        "dispute_refunds": 0.0,
        "refundable": 49.0,
        "support_refund_limit": 500.0,
    }
    assert body["open_dispute"]["id"] == second["id"]
    assert [d["id"] for d in body["disputes"]] == [second["id"], first["id"]]
    assert body["disputes"][1]["resolution_notes"] == "Fee was right"
    assert body["disputes"][0]["created_by_name"] == admins.admin.name


async def test_detail_without_disputes_and_for_a_missing_booking(client, db_session, admins):
    booking, _ = await _online(db_session)
    r = await client.get(f"{A}/bookings/{booking.id}", headers=_session(admins.support))
    assert r.status_code == 200, r.text
    assert (r.json()["open_dispute"], r.json()["disputes"]) == (None, [])
    assert r.json()["refunds"]["refundable"] == 500.0

    r = await client.get(f"{A}/bookings/{uuid.uuid4()}", headers=_session(admins.support))
    _assert_error(r, 404, "BOOKING_NOT_FOUND")


async def test_list_filters_and_pages_newest_first(client, db_session, admins, rzp):
    since = datetime.now(UTC) - timedelta(seconds=1)
    made = []
    for reason in ("NO_SHOW", "QUALITY", "NO_SHOW"):
        booking, _ = await _online(db_session)
        made.append(await _raise(client, admins.admin, booking, reason=reason))
    # One test transaction gives every row the same now(); space them out.
    for i, d in enumerate(made):
        row = await db_session.get(BookingDispute, uuid.UUID(d["id"]))
        row.created_at = datetime.now(UTC) + timedelta(seconds=i)
    await db_session.flush()
    await client.post(f"{A}/{made[0]['id']}/review", headers=_session(admins.admin))
    ours = {d["id"] for d in made}

    async def ids(query):
        r = await client.get(f"{A}?{query}", headers=_session(admins.support))
        assert r.status_code == 200, r.text
        return [d["id"] for d in r.json()["items"] if d["id"] in ours]

    window = f"date_from={since.isoformat().replace('+', '%2B')}&per_page=200"
    assert await ids(window) == [made[2]["id"], made[1]["id"], made[0]["id"]]
    assert await ids(f"{window}&reason_code=NO_SHOW") == [made[2]["id"], made[0]["id"]]
    assert await ids(f"{window}&status=IN_REVIEW") == [made[0]["id"]]
    assert await ids(f"{window}&status=RAISED&reason_code=QUALITY") == [made[1]["id"]]
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+", "%2B")
    assert await ids(f"date_from={future}") == []

    r = await client.get(f"{A}?{window}&per_page=1&page=1", headers=_session(admins.admin))
    assert (r.json()["page"], r.json()["per_page"], len(r.json()["items"])) == (1, 1, 1)
    assert r.json()["total"] >= 3

    for bad in ("status=OPEN", "reason_code=LATE", "per_page=201", "page=0"):
        r = await client.get(f"{A}?{bad}", headers=_session(admins.admin))
        assert r.status_code == 422, bad


# -- roles ----------------------------------------------------------------------------


async def test_every_route_needs_an_admin_console_session(client, db_session, auth_headers):
    consumer = await UserFactory.create(db_session, role="consumer")
    some = uuid.uuid4()
    routes = [
        ("get", A, None),
        ("get", f"{A}/bookings/{some}", None),
        ("post", A, {"booking_id": str(some), "reason_code": "OTHER", "description": "x"}),
        ("post", f"{A}/{some}/review", None),
        ("post", f"{A}/{some}/resolve", {"resolution_type": "NO_REFUND", "notes": "x"}),
    ]
    for method, url, body in routes:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(client, method)(url, **kwargs)
        assert r.status_code == 401, (url, r.text)
        r = await getattr(client, method)(url, headers=auth_headers(str(consumer.id)), **kwargs)
        assert r.status_code in (401, 403), (url, r.text)


@pytest.mark.parametrize("role", ["admin", "ops", "support"])
async def test_all_three_admin_roles_can_read_and_write(client, db_session, admins, rzp, role):
    who = getattr(admins, role)
    booking, _ = await _online(db_session)
    dispute = await _raise(client, who, booking)
    assert (await client.get(A, headers=_session(who))).status_code == 200
    assert (
        await client.get(f"{A}/bookings/{booking.id}", headers=_session(who))
    ).status_code == 200
    assert (
        await client.post(f"{A}/{dispute['id']}/review", headers=_session(who))
    ).status_code == 200
    assert (await _resolve(client, who, dispute["id"], "NO_REFUND")).status_code == 200


# -- audit vocabulary ------------------------------------------------------------------


def test_dispute_actions_are_registered():
    assert {"dispute.create", "dispute.review", "dispute.resolve"} <= set(ADMIN_ACTIONS)
    assert "dispute" in ADMIN_ACTION_TARGET_TYPES


# -- the customer push -------------------------------------------------------------------


async def test_resolve_enqueues_the_push_only_after_commit(client, db_session, admins, rzp):
    sent: list[dict] = []
    booking, _ = await _online(db_session)
    dispute = await _raise(client, admins.admin, booking)
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append(kw),
    ):
        r = await _resolve(client, admins.admin, dispute["id"], "PARTIAL_REFUND", amount=50)
        assert r.status_code == 200, r.text
        assert sent == []  # not committed yet
        _run_after_commit(db_session.sync_session)
    assert sent == [
        {"booking_id": str(booking.id), "event": "dispute_refunded", "dispute_id": dispute["id"]}
    ]


@pytest.mark.parametrize(
    ("channel", "amount", "event"),
    [("RAZORPAY", Decimal("50.00"), "dispute_refunded"),
     ("MANUAL", Decimal("1250.00"), "dispute_refunded_manual"),
     (None, None, "dispute_no_refund")],
)  # fmt: skip
def test_the_dispute_push_copy(channel, amount, event):
    """Run the real task body against stand-in rows: consumer only, the amount
    in the copy, within the push length cap."""
    booking_id = uuid.uuid4()
    consumer = SimpleNamespace(id=uuid.uuid4(), fcm_token="tok-consumer")
    provider = SimpleNamespace(id=uuid.uuid4(), fcm_token="tok-provider")
    booking = SimpleNamespace(
        id=booking_id, consumer=consumer, provider=provider, provider_id=provider.id
    )
    dispute = SimpleNamespace(booking_id=booking_id, refund_amount=amount, refund_channel=channel)

    db = MagicMock()
    db.get.side_effect = lambda model, key, **_: (
        booking if model.__name__ == "Booking" else dispute
    )
    session = MagicMock()
    session.__enter__.return_value = db
    sent = []
    with (
        patch("app.database.SyncSessionLocal", return_value=session),
        patch.object(notification_tasks, "_send_booking_push") as push,
    ):
        push.side_effect = lambda token, bid, ev, title, body: sent.append((token, ev, body))
        notification_tasks.send_booking_notification(
            booking_id=str(booking_id), event=event, dispute_id=str(uuid.uuid4())
        )
    [(token, ev, body)] = sent
    assert (token, ev) == ("tok-consumer", event)
    assert len(body) <= notification_tasks.PUSH_BODY_MAX_CHARS
    if amount is not None:
        assert notification_tasks._format_rupees(int(amount * 100)) in body
