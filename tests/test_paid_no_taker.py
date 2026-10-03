"""
SVC-344: a PAID booking nobody takes gets a second offer, then a system cancel
with a full refund and a consumer push. And a provider backing out of an
accepted job reopens it instead of cancelling it on the customer.

Two layers:

* ``BookingService`` directly, on the usual rolled-back session: the second
  offer, the refund, the re-checks that make it idempotent, a reopen restarting
  the cycle, and the provider-cancel rules through the real route.
* The real sweep (``expire_stale_bookings``), which runs each paid booking in
  its own ``asyncio.run`` + session. That can't share pytest's event loop or its
  uncommitted transaction, so those tests are plain sync tests on committed
  rows, cleaned up afterwards.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.exceptions import PaymentError
from app.models.booking import Booking, BookingStatus, PaymentMethod, PaymentStatus
from app.models.booking_status_log import BookingStatusLog
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.provider_ledger_entry import ProviderLedgerEntry
from app.services.booking_service import BookingService
from app.tasks import notification_tasks
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_refunds import _seed_booking_with_payment

RZP = "app.services.payment_service.razorpay_client"


def _ago(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


async def _paid_open_booking(db, *, discount=0.0, age=130):
    """A PAID online booking at REQUESTED, nobody on it, created ``age`` s ago."""
    consumer = await UserFactory.create(db, role="consumer")
    provider = await UserFactory.create(db, role="provider")
    booking, payment = await _seed_booking_with_payment(
        db,
        consumer=consumer,
        provider=provider,
        status=BookingStatus.REQUESTED,
        service_amount=500.0,
        discount_amount=discount,
        razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
    )
    booking.provider_id = None
    booking.created_at = _ago(age)
    await db.flush()
    return booking


# --- The cycle, at the service ------------------------------------------------------


@pytest.mark.asyncio
async def test_window_over_starts_a_second_offer_once(db_session):
    booking = await _paid_open_booking(db_session)
    service = BookingService(db_session)

    assert await service.advance_paid_unclaimed(booking.id) == "second_offer"
    assert booking.status is BookingStatus.REQUESTED
    assert booking.last_chance_at is not None
    assert booking.offer_expires_at == booking.last_chance_at + timedelta(seconds=90)

    # Within the second offer: nothing to do, however often the sweep runs.
    assert await service.advance_paid_unclaimed(booking.id) is None
    assert await service.advance_paid_unclaimed(booking.id) is None


@pytest.mark.asyncio
async def test_not_due_yet_is_left_alone(db_session):
    booking = await _paid_open_booking(db_session, age=30)
    assert await BookingService(db_session).advance_paid_unclaimed(booking.id) is None
    assert booking.last_chance_at is None


@pytest.mark.asyncio
async def test_second_offer_over_cancels_and_refunds_in_full(db_session):
    """Refund = exactly what was captured: ₹500 − ₹100 discount = ₹400."""
    booking = await _paid_open_booking(db_session, discount=100.0)
    booking.last_chance_at = _ago(95)
    await db_session.flush()

    with patch(RZP) as rzp:
        rzp.payment.refund.return_value = {"id": "rfnd_no_taker"}
        outcome = await BookingService(db_session).advance_paid_unclaimed(booking.id)
        # A second sweep (or a Celery retry) changes nothing and refunds nothing.
        again = await BookingService(db_session).advance_paid_unclaimed(booking.id)

    assert (outcome, again) == ("refunded", None)
    assert rzp.payment.refund.call_count == 1
    assert rzp.payment.refund.call_args.args[1]["amount"] == 40000
    assert booking.status is BookingStatus.CANCELLED
    assert booking.payment_status is PaymentStatus.REFUND_PROCESSING
    assert booking.doorstep_otp is None
    [log] = (
        await db_session.scalars(
            select(BookingStatusLog).where(BookingStatusLog.booking_id == booking.id)
        )
    ).all()
    assert (log.from_status, log.to_status) == ("requested", "cancelled")
    assert log.changed_by is None, "the system, not a person"
    assert log.note == "system: no professional available"
    assert log.metadata_ == {"actor": "system", "reason": "no_taker"}
    # No fee anywhere: nobody had the job.
    fees = (
        await db_session.scalars(
            select(ProviderLedgerEntry).where(ProviderLedgerEntry.reference_id == booking.id)
        )
    ).all()
    assert fees == []


@pytest.mark.asyncio
async def test_a_refund_failure_raises_so_nothing_is_saved(db_session):
    """The sweep runs this in its own transaction and rolls it back on error, so
    the booking stays REQUESTED + PAID for the next sweep to retry."""
    booking = await _paid_open_booking(db_session)
    booking.last_chance_at = _ago(95)
    await db_session.flush()

    with patch(RZP) as rzp:
        rzp.payment.refund.side_effect = RuntimeError("razorpay down")
        with pytest.raises(PaymentError):
            await BookingService(db_session).advance_paid_unclaimed(booking.id)


@pytest.mark.asyncio
async def test_unpaid_cash_scheduled_and_claimed_bookings_are_not_its_business(db_session):
    service = BookingService(db_session)

    unpaid = await _paid_open_booking(db_session)
    unpaid.payment_status = PaymentStatus.PENDING
    cash = await _paid_open_booking(db_session)
    cash.payment_method = PaymentMethod.CASH
    scheduled = await _paid_open_booking(db_session)
    scheduled.scheduled_at = datetime.now(UTC) + timedelta(hours=2)
    claimed = await _paid_open_booking(db_session)
    claimed.status = BookingStatus.ACCEPTED
    claimed.provider_id = claimed.consumer_id
    await db_session.flush()

    for booking in (unpaid, cash, scheduled, claimed):
        assert await service.advance_paid_unclaimed(booking.id) is None
        assert booking.last_chance_at is None


@pytest.mark.asyncio
async def test_a_taker_during_the_second_offer_wins_normally(client, db_session, auth_headers):
    service_row = await _service(db_session)
    provider = await _eligible_provider(db_session, service_row)
    _, booking = await _booking(db_session, service=service_row)
    booking.payment_method = PaymentMethod.ONLINE
    booking.payment_status = PaymentStatus.PAID
    booking.created_at = _ago(130)
    await db_session.flush()
    assert await BookingService(db_session).advance_paid_unclaimed(booking.id) == "second_offer"

    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/accept",
        headers=auth_headers(str(provider.id), "provider"),
    )
    assert resp.status_code == 200, resp.text

    # Even once the second offer would have run out, a claimed booking is left be.
    booking.last_chance_at = _ago(95)
    await db_session.flush()
    assert await BookingService(db_session).advance_paid_unclaimed(booking.id) is None
    assert booking.status is BookingStatus.ACCEPTED


@pytest.mark.asyncio
async def test_a_reopen_restarts_the_cycle_with_a_new_otp(db_session):
    booking = await _paid_open_booking(db_session)
    booking.last_chance_at = _ago(30)
    booking.status = BookingStatus.ACCEPTED
    booking.provider_id = booking.consumer_id
    await db_session.flush()

    with patch.object(BookingService, "_generate_doorstep_otp", return_value="4321"):
        await BookingService(db_session).reopen(
            booking, changed_by=booking.consumer_id, reason="released"
        )

    assert booking.last_chance_at is None
    assert booking.doorstep_otp == "4321", "a fresh OTP: the old one has been seen"
    # A fresh full window: not due for 120 s.
    assert await BookingService(db_session).advance_paid_unclaimed(booking.id) is None
    booking.reopened_at = _ago(130)
    await db_session.flush()
    assert await BookingService(db_session).advance_paid_unclaimed(booking.id) == "second_offer"


# --- Offer visibility follows the second offer --------------------------------------


@pytest.mark.asyncio
async def test_the_card_and_poll_follow_the_second_offer(db_session):
    service_row = await _service(db_session)
    provider = await _eligible_provider(db_session, service_row)
    _, booking = await _booking(db_session, service=service_row)
    booking.payment_method = PaymentMethod.ONLINE
    booking.payment_status = PaymentStatus.PAID
    booking.created_at = _ago(130)
    await db_session.flush()
    service = BookingService(db_session)

    assert await service.list_open_offers(provider.id) == [], "window over"
    await service.advance_paid_unclaimed(booking.id)
    [offer] = await service.list_open_offers(provider.id)
    assert offer.booking_id == booking.id
    assert 80 <= offer.seconds_remaining <= 90


# --- Provider backing out (PATCH /cancel) -------------------------------------------


async def _accepted_paid(db, client, auth_headers):
    service_row = await _service(db)
    provider = await _eligible_provider(db, service_row)
    consumer, booking = await _booking(db, service=service_row)
    booking.payment_method = PaymentMethod.ONLINE
    booking.payment_status = PaymentStatus.PAID
    db.add(
        Payment(
            booking_id=booking.id,
            razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
            razorpay_order_id=f"order_{uuid.uuid4().hex[:8]}",
            amount=Decimal(str(booking.service_amount)),
            platform_fee=Decimal("0"),
            provider_settlement=Decimal("0"),
            status=PaymentRecordStatus.CAPTURED,
            method=PaymentRecordMethod.UPI,
        )
    )
    await db.flush()
    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/accept",
        headers=auth_headers(str(provider.id), "provider"),
    )
    assert resp.status_code == 200, resp.text
    await db.refresh(booking)
    return consumer, provider, booking


@pytest.fixture
def pushes():
    sent: list[str] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append(kw["event"]),
    ):
        yield sent


@pytest.mark.asyncio
async def test_provider_cancel_while_accepted_reopens_with_no_fee(
    client, db_session, auth_headers, pushes
):
    consumer, provider, booking = await _accepted_paid(db_session, client, auth_headers)
    pushes.clear()

    with (
        patch(RZP) as rzp,
        patch.object(BookingService, "_generate_doorstep_otp", return_value="4321"),
    ):
        resp = await client.patch(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "can't make it"},
            headers=auth_headers(str(provider.id), "provider"),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # What the ex-provider gets back: the reopened booking, and nothing private.
    assert body["status"] == "requested"
    assert body["provider_id"] is None
    assert body["doorstep_otp"] is None
    assert body["address"] is None
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED
    assert (booking.provider_id, booking.professional_id) == (None, None)
    assert booking.payment_status is PaymentStatus.PAID, "no refund: still being served"
    assert booking.doorstep_otp == "4321"
    rzp.payment.refund.assert_not_called()
    fees = (
        await db_session.scalars(
            select(ProviderLedgerEntry).where(ProviderLedgerEntry.reference_id == booking.id)
        )
    ).all()
    assert fees == [], "nobody is charged when the provider backs out"
    assert sorted(pushes) == ["new_request", "reopened"]


@pytest.mark.asyncio
async def test_provider_cancel_while_en_route_is_409(client, db_session, auth_headers, pushes):
    _, provider, booking = await _accepted_paid(db_session, client, auth_headers)
    booking.status = BookingStatus.EN_ROUTE
    await db_session.flush()

    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/cancel",
        json={"reason": "stuck in traffic"},
        headers=auth_headers(str(provider.id), "provider"),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "PROVIDER_EN_ROUTE"
    assert resp.json()["message"] == "You're on the way — contact support to cancel this job."
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.EN_ROUTE


@pytest.mark.asyncio
async def test_consumer_cancel_after_accept_still_charges_the_fee(
    client, db_session, auth_headers, pushes
):
    consumer, provider, booking = await _accepted_paid(db_session, client, auth_headers)
    captured = int(booking.service_amount * 100)
    pushes.clear()

    with patch(RZP) as rzp:
        rzp.payment.refund.return_value = {"id": "rfnd_consumer"}
        resp = await client.patch(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "changed my mind"},
            headers=auth_headers(str(consumer.id), "consumer"),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    from app.config import get_settings

    fee = get_settings().CANCELLATION_FEE_PAISE
    assert rzp.payment.refund.call_args.args[1]["amount"] == captured - fee
    [credit] = (
        await db_session.scalars(
            select(ProviderLedgerEntry).where(ProviderLedgerEntry.reference_id == booking.id)
        )
    ).all()
    assert credit.provider_id == provider.id
    assert pushes == ["cancelled"]


# --- The real sweep, on committed rows ----------------------------------------------


def _sync_url() -> str:
    return TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture
def committed_sweep_world():
    """Point both session factories the sweep uses at the test DB, and clean up
    every row a test registers."""
    engine = create_engine(_sync_url())
    sync_factory = sessionmaker(bind=engine, expire_on_commit=False)
    made: dict[str, list] = {"users": [], "bookings": []}

    def async_factory():
        # A fresh engine per asyncio.run: an engine's pool is bound to the loop
        # that created it.
        return async_sessionmaker(create_async_engine(TEST_DATABASE_URL), expire_on_commit=False)()

    with (
        patch("app.database.SyncSessionLocal", sync_factory),
        patch("app.database.AsyncSessionLocal", async_factory),
    ):
        yield sync_factory, made

    with sync_factory() as db:
        ids = [str(b) for b in made["bookings"]]
        if ids:
            for table in ("booking_status_logs", "payments", "booking_offers", "booking_items"):
                db.execute(
                    text(f"DELETE FROM {table} WHERE booking_id::text = ANY(:ids)"), {"ids": ids}
                )
            db.execute(text("DELETE FROM bookings WHERE id::text = ANY(:ids)"), {"ids": ids})
        users = [str(u) for u in made["users"]]
        if users:
            db.execute(
                text("DELETE FROM addresses WHERE user_id::text = ANY(:ids)"), {"ids": users}
            )
            db.execute(text("DELETE FROM users WHERE id::text = ANY(:ids)"), {"ids": users})
        db.commit()
    engine.dispose()


def _seed(sync_factory, made, *, paid=True, method=PaymentMethod.ONLINE, age=130, **extra):
    """A committed REQUESTED booking with its consumer, address and (if paid) a
    captured payment. Returns its id."""
    from app.models.address import Address
    from app.models.user import User, UserRole

    with sync_factory() as db:
        consumer = User(
            phone=f"+9199{uuid.uuid4().int % 10**8:08d}",
            role=UserRole.CONSUMER,
            name="Asha",
            fcm_token=f"fcm-{uuid.uuid4().hex[:8]}",
        )
        db.add(consumer)
        db.flush()
        address = Address(user_id=consumer.id, label="home", full_address="1 Test Road")
        db.add(address)
        db.flush()
        booking = Booking(
            consumer_id=consumer.id,
            consumer_address_id=address.id,
            status=BookingStatus.REQUESTED,
            service_amount=Decimal("799"),
            discount_amount=Decimal("0"),
            platform_commission=Decimal("160"),
            provider_payout=Decimal("639"),
            tip_amount=Decimal("0"),
            payment_method=method,
            payment_status=PaymentStatus.PAID if paid else PaymentStatus.PENDING,
            doorstep_otp="1234",
            created_at=_ago(age),
            **extra,
        )
        db.add(booking)
        db.flush()
        if paid and method is PaymentMethod.ONLINE:
            db.add(
                Payment(
                    booking_id=booking.id,
                    razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
                    razorpay_order_id=f"order_{uuid.uuid4().hex[:8]}",
                    amount=Decimal("799"),
                    platform_fee=Decimal("160"),
                    provider_settlement=Decimal("639"),
                    status=PaymentRecordStatus.CAPTURED,
                    method=PaymentRecordMethod.UPI,
                )
            )
        db.commit()
        made["users"].append(consumer.id)
        made["bookings"].append(booking.id)
        return booking.id


def _get(sync_factory, booking_id) -> Booking:
    with sync_factory() as db:
        return db.get(Booking, booking_id)


def _sweep(rzp_refund=None):
    """Run the real sweep with Razorpay and the push queue stubbed."""
    from app.tasks.cleanup_tasks import expire_stale_bookings

    sent: list[tuple[str, str]] = []
    with (
        patch(RZP) as rzp,
        patch.object(
            notification_tasks.send_booking_notification,
            "delay",
            side_effect=lambda **kw: sent.append((kw["booking_id"], kw["event"])),
        ),
    ):
        if isinstance(rzp_refund, Exception):
            rzp.payment.refund.side_effect = rzp_refund
        else:
            rzp.payment.refund.return_value = {"id": f"rfnd_{uuid.uuid4().hex[:6]}"}
        summary = expire_stale_bookings()
    return summary, sent, rzp


def test_sweep_paid_cycle_end_to_end_and_unpaid_unchanged(committed_sweep_world):
    sync_factory, made = committed_sweep_world
    paid = _seed(sync_factory, made)
    unpaid = _seed(sync_factory, made, paid=False)
    cash = _seed(sync_factory, made, paid=False, method=PaymentMethod.CASH)
    scheduled = _seed(sync_factory, made, scheduled_at=datetime.now(UTC) + timedelta(hours=2))

    # 1st sweep at T+130 s: unpaid + cash expire as always; paid gets its second
    # offer and a re-broadcast; the scheduled one waits for its slot.
    _, sent, _ = _sweep()
    assert _get(sync_factory, unpaid).status is BookingStatus.EXPIRED
    assert _get(sync_factory, cash).status is BookingStatus.EXPIRED
    after_first = _get(sync_factory, paid)
    assert after_first.status is BookingStatus.REQUESTED
    assert after_first.last_chance_at is not None
    assert (str(paid), "new_request") in sent
    assert _get(sync_factory, scheduled).last_chance_at is None
    assert _get(sync_factory, scheduled).status is BookingStatus.REQUESTED

    # Inside the second offer: nothing.
    _, sent, rzp = _sweep()
    assert [e for b, e in sent if b == str(paid)] == []
    rzp.payment.refund.assert_not_called()

    # Second offer over: two sweeps back to back, one refund, one push.
    with sync_factory() as db:
        db.get(Booking, paid).last_chance_at = _ago(95)
        db.commit()
    _, sent_a, rzp_a = _sweep()
    _, sent_b, rzp_b = _sweep()
    done = _get(sync_factory, paid)
    assert done.status is BookingStatus.CANCELLED
    assert done.payment_status is PaymentStatus.REFUND_PROCESSING
    assert rzp_a.payment.refund.call_args.args[1]["amount"] == 79900
    rzp_b.payment.refund.assert_not_called()
    assert [e for b, e in sent_a + sent_b if b == str(paid)] == ["no_taker_refunded"]


def test_sweep_refund_failure_leaves_the_booking_for_the_next_sweep(committed_sweep_world, caplog):
    import logging

    sync_factory, made = committed_sweep_world
    paid = _seed(sync_factory, made)
    with sync_factory() as db:
        db.get(Booking, paid).last_chance_at = _ago(95)
        db.commit()

    with caplog.at_level(logging.ERROR, logger="app.tasks.cleanup_tasks"):
        summary, sent, _ = _sweep(rzp_refund=RuntimeError("razorpay down"))

    still = _get(sync_factory, paid)
    assert still.status is BookingStatus.REQUESTED
    assert still.payment_status is PaymentStatus.PAID
    assert [e for b, e in sent if b == str(paid)] == []
    assert "1 failed" in summary
    assert any("booking.no_taker_refund_failed" in r.getMessage() for r in caplog.records)

    # Razorpay is back: the next sweep finishes the job.
    _sweep()
    assert _get(sync_factory, paid).status is BookingStatus.CANCELLED


@pytest.mark.parametrize(
    ("reopened", "title", "opening"),
    [
        (False, "No professional available", "We couldn't find a professional"),
        (True, "Sorry, your professional couldn't make it", "Your professional had to cancel"),
    ],
)
def test_the_refund_push_wording(committed_sweep_world, reopened, title, opening):
    sync_factory, made = committed_sweep_world
    booking_id = _seed(sync_factory, made, reopened_at=_ago(300) if reopened else None)
    with patch.object(notification_tasks, "send_push_notification") as push:
        notification_tasks.send_booking_notification(
            booking_id=str(booking_id), event="no_taker_refunded"
        )
    [call] = push.call_args_list
    assert call.kwargs["title"] == title
    body = call.kwargs["body"]
    assert body.startswith(opening)
    assert body.endswith("Your full refund of ₹799 has been started.")
    with sync_factory() as db:
        consumer_token = db.get(Booking, booking_id).consumer.fcm_token
    assert call.kwargs["token"] == consumer_token, "consumer only"
