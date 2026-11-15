"""
SVC-479: a marketplace booking goes to the SP the customer chose, and only them.

- ``POST /bookings`` with ``sp_id`` stores it as ``chosen_sp_id``; an SP who
  can't take the cart right now is a 409 ``SP_UNAVAILABLE`` and no booking.
- The one rule in ``provider_matching`` (``_chosen_sp_only``) limits who is
  pushed, who can see or poll the offer, who can accept, the paid second offer
  and support's retry to that SP. Every other SP gets the existing 403.
- The chosen SP declining, or the job coming back through any of the three
  reopen routes, ends the booking at once (cash → EXPIRED, paid → full refund)
  with one consumer push, ``chosen_sp_unavailable``.
- A timeout: cash expires as before, plus that push; paid gets its second offer
  (to the chosen SP only) and then the usual refund.

Bookings without ``sp_id`` are pinned by every other test in the suite, which
this story leaves unchanged.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import _run_after_commit
from app.models.address import Address
from app.models.booking import Booking, BookingStatus, PaymentMethod, PaymentStatus
from app.models.booking_offer import BookingOffer, OfferResponse
from app.models.booking_status_log import BookingStatusLog
from app.models.payment import Payment, PaymentRecordMethod, PaymentRecordStatus
from app.models.provider_ledger_entry import ProviderLedgerEntry
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.user import AdminRole, User, UserRole
from app.services.booking_service import BookingService
from app.services.provider_matching import broadcast_funnel, broadcast_recipients_stmt
from app.tasks import notification_tasks
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import ADDRESS_POINT, _booking, _eligible_provider, _service
from tests.test_admin_console_auth import _admin, _session
from tests.test_paid_no_taker import (  # noqa: F401  (fixture)
    RZP,
    _get,
    _seed,
    _sweep,
    committed_sweep_world,
)
from tests.test_two_level_dispatch import _team_pro

CREATE = "/api/v1/bookings/"
OFFER = "/api/v1/bookings/{}/offer"
ACCEPT = "/api/v1/bookings/{}/accept"
DECLINE_OFFER = "/api/v1/bookings/{}/decline"
RELEASE = "/api/v1/bookings/{}/release"
CANCEL = "/api/v1/bookings/{}/cancel"
DECLINE_JOB = "/api/v1/pros/me/jobs/{}/decline"
RETRY = "/api/v1/admin/bookings/{}/retry-dispatch"


@pytest.fixture
def pushes():
    """Every ``send_booking_notification.delay``, as ``(event, booking_id)``."""
    sent: list[tuple[str, str]] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append((kw["event"], kw["booking_id"])),
    ):
        yield sent


def _events(pushes, booking) -> list[str]:
    return [event for event, booking_id in pushes if booking_id == str(booking.id)]


def _as(auth_headers, user, role="provider"):
    return auth_headers(str(user.id), role)


async def _chosen_booking(db, service, sp, *, paid=False):
    """A REQUESTED booking whose customer picked ``sp``. Paid = online, captured."""
    consumer, booking = await _booking(db, service=service)
    booking.chosen_sp_id = sp.id
    if paid:
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
    return consumer, booking


async def _logs(db, booking) -> list[tuple[str, str, dict]]:
    rows = (
        await db.scalars(
            select(BookingStatusLog)
            .where(BookingStatusLog.booking_id == booking.id)
            .order_by(BookingStatusLog.created_at, BookingStatusLog.to_status.desc())
        )
    ).all()
    return [(r.from_status, r.to_status, r.metadata_) for r in rows]


async def _recipients(db, service, booking) -> set:
    address = await db.get(Address, booking.consumer_address_id)
    stmt = broadcast_recipients_stmt([service.id], address.location, booking.id)
    return {u.id for u in (await db.scalars(stmt)).all()}


# --- Create -------------------------------------------------------------------------


async def _consumer_with_address(db):
    consumer = await UserFactory.create(db, role="consumer")
    address = Address(
        user_id=consumer.id, label="home", full_address="12 MG Road", location=ADDRESS_POINT
    )
    db.add(address)
    await db.flush()
    return consumer, address


def _create_body(service, address, sp_id):
    return {
        "service_id": str(service.id),
        "address_id": str(address.id),
        "payment_method": "cash",
        "sp_id": str(sp_id),
    }


@pytest.mark.asyncio
async def test_create_with_an_eligible_sp_stores_it(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    consumer, address = await _consumer_with_address(db_session)

    resp = await client.post(
        CREATE,
        json=_create_body(service, address, sp.id),
        headers=_as(auth_headers, consumer, "consumer"),
    )

    assert resp.status_code == 201, resp.text
    booking = await db_session.get(Booking, uuid.UUID(resp.json()["id"]))
    assert booking.chosen_sp_id == sp.id
    assert booking.status is BookingStatus.REQUESTED and booking.provider_id is None
    assert _events(pushes, booking) == ["new_request"]


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["offline", "kyc", "not_subscribed", "out_of_area", "not_an_sp"])
async def test_create_with_an_ineligible_sp_is_409_and_writes_nothing(
    client, db_session, auth_headers, pushes, why
):
    service = await _service(db_session)
    if why == "not_an_sp":
        sp_id = (await UserFactory.create(db_session, role="consumer")).id
    else:
        knobs = {
            "offline": {"online": False},
            "kyc": {"kyc": KYCStatus.PENDING},
            "not_subscribed": {"subscribed": False},
            "out_of_area": {"point": "SRID=4326;POINT(72.8777 19.0760)"},  # Mumbai
        }[why]
        sp_id = (await _eligible_provider(db_session, service, **knobs)).id
    # Someone else could take it: the chosen SP is still the only one checked.
    await _eligible_provider(db_session, service)
    consumer, address = await _consumer_with_address(db_session)

    resp = await client.post(
        CREATE,
        json=_create_body(service, address, sp_id),
        headers=_as(auth_headers, consumer, "consumer"),
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "SP_UNAVAILABLE"
    await db_session.flush()
    count = await db_session.scalar(
        select(func.count()).select_from(Booking).where(Booking.consumer_id == consumer.id)
    )
    assert count == 0
    assert pushes == []


@pytest.mark.asyncio
async def test_a_multi_service_cart_needs_the_sp_to_cover_every_item(
    client, db_session, auth_headers, pushes
):
    service_a = await _service(db_session)
    service_b = await _service(db_session)
    sp = await _eligible_provider(db_session, service_a)  # covers A only
    consumer, address = await _consumer_with_address(db_session)

    resp = await client.post(
        CREATE,
        json={
            "items": [{"service_id": str(service_a.id)}, {"service_id": str(service_b.id)}],
            "address_id": str(address.id),
            "payment_method": "cash",
            "sp_id": str(sp.id),
        },
        headers=_as(auth_headers, consumer, "consumer"),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "SP_UNAVAILABLE"


# --- Who is offered it --------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_chosen_sp_is_pushed_sees_the_offer_and_can_accept(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    chosen = await _eligible_provider(db_session, service)
    other = await _eligible_provider(db_session, service)
    _, booking = await _chosen_booking(db_session, service, chosen)
    # Control: the same two SPs both get an open booking.
    _, open_booking = await _booking(db_session, service=service)

    assert await _recipients(db_session, service, booking) == {chosen.id}
    assert {chosen.id, other.id} <= await _recipients(db_session, service, open_booking)

    resp = await client.get(OFFER.format(booking.id), headers=_as(auth_headers, other))
    assert resp.status_code == 403
    assert resp.json()["code"] == "NOT_OFFERED"
    resp = await client.get(OFFER.format(booking.id), headers=_as(auth_headers, chosen))
    assert resp.status_code == 200, resp.text

    service_ = BookingService(db_session)
    assert booking.id not in [o.booking_id for o in await service_.list_open_offers(other.id)]
    assert booking.id in [o.booking_id for o in await service_.list_open_offers(chosen.id)]

    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, other))
    assert resp.status_code == 403
    assert resp.json()["code"] == "NOT_OFFERED"
    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, chosen))
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider_id"] == str(chosen.id)


# --- The chosen SP declines the offer -----------------------------------------------


async def _offered(db, booking, sp):
    db.add(BookingOffer(booking_id=booking.id, provider_id=sp.id))
    await db.flush()


@pytest.mark.asyncio
async def test_chosen_sp_decline_expires_a_cash_booking(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _chosen_booking(db_session, service, sp)
    await _offered(db_session, booking, sp)

    resp = await client.patch(DECLINE_OFFER.format(booking.id), headers=_as(auth_headers, sp))

    assert resp.status_code == 204, resp.text
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.EXPIRED
    assert booking.doorstep_otp is None
    assert await _logs(db_session, booking) == [
        ("requested", "expired", {"actor": "system", "reason": "chosen_sp_declined"})
    ]
    offer = await db_session.scalar(
        select(BookingOffer).where(BookingOffer.booking_id == booking.id)
    )
    assert offer.response is OfferResponse.DECLINED
    # decline_offer commits itself, so the after-commit push has fired.
    assert _events(pushes, booking) == ["chosen_sp_unavailable"]

    # A double tap: nothing more.
    resp = await client.patch(DECLINE_OFFER.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 204
    assert _events(pushes, booking) == ["chosen_sp_unavailable"]


@pytest.mark.asyncio
async def test_chosen_sp_decline_refunds_a_paid_booking_with_one_push(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _chosen_booking(db_session, service, sp, paid=True)
    await _offered(db_session, booking, sp)
    captured = int(booking.service_amount * 100)

    with patch(RZP) as rzp:
        rzp.payment.refund.return_value = {"id": "rfnd_chosen"}
        resp = await client.patch(DECLINE_OFFER.format(booking.id), headers=_as(auth_headers, sp))

    assert resp.status_code == 204, resp.text
    assert rzp.payment.refund.call_count == 1
    assert rzp.payment.refund.call_args.args[1]["amount"] == captured
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.CANCELLED
    assert booking.payment_status is PaymentStatus.REFUND_PROCESSING
    [log] = await _logs(db_session, booking)
    assert log == ("requested", "cancelled", {"actor": "system", "reason": "chosen_sp_declined"})
    # One push, not no_taker_refunded as well.
    assert _events(pushes, booking) == ["chosen_sp_unavailable"]


@pytest.mark.asyncio
async def test_another_sp_declining_a_chosen_booking_is_refused(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    chosen = await _eligible_provider(db_session, service)
    other = await _eligible_provider(db_session, service)
    _, booking = await _chosen_booking(db_session, service, chosen)

    resp = await client.patch(DECLINE_OFFER.format(booking.id), headers=_as(auth_headers, other))
    assert resp.status_code == 403
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED
    assert pushes == []


# --- The job comes back: the three reopen routes ------------------------------------


async def _do_reopen(client, auth_headers, booking, sp, route):
    if route == "released":
        return await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, sp))
    if route == "provider_cancelled":
        return await client.patch(
            CANCEL.format(booking.id), json={"reason": "can't"}, headers=_as(auth_headers, sp)
        )
    return await client.post(DECLINE_JOB.format(booking.id), headers=_as(auth_headers, sp))


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", [False, True], ids=["cash", "paid"])
@pytest.mark.parametrize("route", ["released", "provider_cancelled", "pro_declined"])
async def test_a_reopen_ends_a_chosen_sp_booking(
    client, db_session, auth_headers, pushes, route, paid
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    other = await _eligible_provider(db_session, service)
    _, booking = await _chosen_booking(db_session, service, sp, paid=paid)
    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    captured = int(booking.service_amount * 100)
    pushes.clear()

    with patch(RZP) as rzp:
        rzp.payment.refund.return_value = {"id": "rfnd_reopen"}
        resp = await _do_reopen(client, auth_headers, booking, sp, route)
    assert resp.status_code == 200, resp.text
    await db_session.flush()

    await db_session.refresh(booking)
    ended = BookingStatus.CANCELLED if paid else BookingStatus.EXPIRED
    assert booking.status is ended
    assert (booking.provider_id, booking.professional_id) == (None, None)
    # One transaction, one now(): key the rows by transition, not by order.
    logs = {(f, t): meta for f, t, meta in await _logs(db_session, booking)}
    assert set(logs) == {
        ("requested", "accepted"),
        ("accepted", "requested"),
        ("requested", ended.value),
    }
    assert logs[("accepted", "requested")]["reason"] == route
    assert logs[("requested", ended.value)] == {
        "actor": "system",
        "reason": "chosen_sp_released",
        "reopen_reason": route,
    }
    if paid:
        # Ended from REQUESTED: the full amount back, and no fee to anyone.
        assert rzp.payment.refund.call_args.args[1]["amount"] == captured
        fees = (
            await db_session.scalars(
                select(ProviderLedgerEntry).where(ProviderLedgerEntry.reference_id == booking.id)
            )
        ).all()
        assert fees == []
    else:
        rzp.payment.refund.assert_not_called()

    # Not re-broadcast, not "reopened": one push, once the request commits.
    assert pushes == []
    _run_after_commit(db_session.sync_session)
    assert _events(pushes, booking) == ["chosen_sp_unavailable"]
    assert await BookingService(db_session).list_open_offers(other.id) == []


@pytest.mark.asyncio
async def test_a_release_keeps_the_team_pros_unassigned_push(
    client, db_session, auth_headers, pushes
):
    """Only the consumer "reopened" push and the re-broadcast are dropped: the
    team Pro taken off the job is still told not to travel (SVC-348)."""
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    member = await _team_pro(db_session, sp.id)
    _, booking = await _chosen_booking(db_session, service, sp)
    resp = await client.post(
        f"/api/v1/bookings/{booking.id}/assign",
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 200, resp.text
    pushes.clear()

    resp = await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    _run_after_commit(db_session.sync_session)

    assert sorted(_events(pushes, booking)) == ["chosen_sp_unavailable", "unassigned"]


@pytest.mark.asyncio
async def test_a_reopen_without_a_chosen_sp_is_unchanged(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp))
    pushes.clear()

    resp = await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200
    assert resp.json()["status"] == "requested"
    _run_after_commit(db_session.sync_session)
    assert sorted(_events(pushes, booking)) == ["new_request", "reopened"]


# --- Paid timeout: the second offer is for the chosen SP only -----------------------


@pytest.mark.asyncio
async def test_the_paid_second_offer_reaches_only_the_chosen_sp(db_session):
    service = await _service(db_session)
    chosen = await _eligible_provider(db_session, service)
    await _eligible_provider(db_session, service)
    _, booking = await _chosen_booking(db_session, service, chosen, paid=True)
    booking.created_at = datetime.now(UTC) - timedelta(seconds=130)
    await db_session.flush()
    service_ = BookingService(db_session)

    assert await service_.advance_paid_unclaimed(booking.id) == "second_offer"
    assert await _recipients(db_session, service, booking) == {chosen.id}

    booking.last_chance_at = datetime.now(UTC) - timedelta(seconds=95)
    await db_session.flush()
    with patch(RZP) as rzp:
        rzp.payment.refund.return_value = {"id": "rfnd_timeout"}
        assert await service_.advance_paid_unclaimed(booking.id) == "refunded"
    assert booking.status is BookingStatus.CANCELLED


# --- Support's retry ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_retry_offers_only_the_chosen_sp(client, db_session, auth_headers, pushes):
    admin = await _admin(db_session, AdminRole.SUPPORT)
    service = await _service(db_session)
    chosen = await _eligible_provider(db_session, service)
    await _eligible_provider(db_session, service)
    _, booking = await _chosen_booking(db_session, service, chosen)

    resp = await client.post(RETRY.format(booking.id), headers=_session(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["recipients"] == 1


@pytest.mark.asyncio
async def test_admin_retry_names_the_chosen_sp_when_they_are_not_eligible(
    client, db_session, auth_headers, pushes
):
    admin = await _admin(db_session, AdminRole.OPS)
    service = await _service(db_session)
    chosen = await _eligible_provider(db_session, service)
    await _eligible_provider(db_session, service)
    _, booking = await _chosen_booking(db_session, service, chosen)
    profile = await db_session.scalar(
        select(ProviderProfile).where(ProviderProfile.user_id == chosen.id)
    )
    profile.is_online = False
    await db_session.flush()

    resp = await client.post(RETRY.format(booking.id), headers=_session(admin))
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "NO_ELIGIBLE_PROVIDERS"
    assert resp.json()["details"]["reason"] == "chosen_sp_not_eligible"

    address = await db_session.get(Address, booking.consumer_address_id)
    counts = await db_session.run_sync(
        lambda s: broadcast_funnel(s, [service.id], address.location, booking.id)
    )
    # The other SP passes every stage of their own; the chosen one doesn't.
    assert counts["has_someone_to_do_it"] >= 1
    assert counts["chosen_sp"] == 0
    assert list(counts)[-2:] == ["chosen_sp", "not_already_answered"]


@pytest.mark.asyncio
async def test_the_funnel_has_no_chosen_sp_stage_for_an_open_booking(db_session):
    service = await _service(db_session)
    await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    address = await db_session.get(Address, booking.consumer_address_id)
    counts = await db_session.run_sync(
        lambda s: broadcast_funnel(s, [service.id], address.location, booking.id)
    )
    assert "chosen_sp" not in counts


# --- On committed rows: the sweep, the broadcast, the push, the race ----------------


@pytest.fixture
def chosen_world(committed_sweep_world):  # noqa: F811
    """``committed_sweep_world`` plus a committed SP to choose (business name
    "CoolFix"). Its bookings are deleted here first, since the FK refuses to
    let the SP go while a booking names it."""
    sync_factory, made = committed_sweep_world
    with sync_factory() as db:
        sp = User(
            phone=f"+9198{uuid.uuid4().int % 10**8:08d}",
            role=UserRole.PROVIDER,
            name="Rakesh",
            fcm_token=f"fcm-{uuid.uuid4().hex[:8]}",
        )
        db.add(sp)
        db.flush()
        db.add(
            ProviderProfile(
                user_id=sp.id,
                business_name="CoolFix",
                kyc_status=KYCStatus.VERIFIED,
                is_online=True,
            )
        )
        db.commit()
        sp_id = sp.id
    yield sync_factory, made, sp_id

    with sync_factory() as db:
        ids = [str(b) for b in made["bookings"]]
        if ids:
            for table in ("booking_status_logs", "payments", "booking_offers", "booking_items"):
                db.execute(
                    text(f"DELETE FROM {table} WHERE booking_id::text = ANY(:ids)"), {"ids": ids}
                )
            db.execute(text("DELETE FROM bookings WHERE id::text = ANY(:ids)"), {"ids": ids})
        db.execute(text("DELETE FROM provider_profiles WHERE user_id = :id"), {"id": sp_id})
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": sp_id})
        db.commit()


def _pushed(sent, booking_id) -> list[str]:
    return [event for b, event in sent if b == str(booking_id)]


def test_cash_timeout_expires_and_pushes_only_chosen_sp_bookings(chosen_world):
    sync_factory, made, sp_id = chosen_world
    chosen = _seed(sync_factory, made, paid=False, method=PaymentMethod.CASH, chosen_sp_id=sp_id)
    unchosen = _seed(sync_factory, made, paid=False, method=PaymentMethod.CASH)

    _, sent, _ = _sweep()

    assert _get(sync_factory, chosen).status is BookingStatus.EXPIRED
    assert _get(sync_factory, unchosen).status is BookingStatus.EXPIRED
    assert _pushed(sent, chosen) == ["chosen_sp_unavailable"]
    assert _pushed(sent, unchosen) == []


def test_paid_timeout_is_a_second_offer_then_the_usual_refund(chosen_world):
    sync_factory, made, sp_id = chosen_world
    paid = _seed(sync_factory, made, chosen_sp_id=sp_id)

    _, sent, _ = _sweep()
    assert _pushed(sent, paid) == ["new_request"]
    assert _get(sync_factory, paid).last_chance_at is not None

    with sync_factory() as db:
        db.get(Booking, paid).last_chance_at = datetime.now(UTC) - timedelta(seconds=95)
        db.commit()
    _, sent, _ = _sweep()
    assert _get(sync_factory, paid).status is BookingStatus.CANCELLED
    assert _pushed(sent, paid) == ["no_taker_refunded"]


def test_an_unbroadcastable_chosen_sp_booking_tells_the_customer(chosen_world):
    """The broadcast finds nobody (here: no address location) and expires it."""
    sync_factory, made, sp_id = chosen_world
    chosen = _seed(sync_factory, made, paid=False, age=0, chosen_sp_id=sp_id)
    unchosen = _seed(sync_factory, made, paid=False, age=0)

    sent: list[tuple[str, str]] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append((kw["booking_id"], kw["event"])),
    ):
        for booking_id in (chosen, unchosen):
            notification_tasks.send_booking_notification(
                booking_id=str(booking_id), event="new_request"
            )

    assert _get(sync_factory, chosen).status is BookingStatus.EXPIRED
    assert _get(sync_factory, unchosen).status is BookingStatus.EXPIRED
    assert _pushed(sent, chosen) == ["chosen_sp_unavailable"]
    assert _pushed(sent, unchosen) == []


@pytest.mark.parametrize("refunded", [False, True], ids=["expired", "refunded"])
def test_the_chosen_sp_unavailable_wording(chosen_world, refunded):
    sync_factory, made, sp_id = chosen_world
    booking_id = _seed(sync_factory, made, paid=refunded, age=0, chosen_sp_id=sp_id)
    with sync_factory() as db:
        row = db.get(Booking, booking_id)
        if refunded:
            row.status = BookingStatus.CANCELLED
            row.payment_status = PaymentStatus.REFUND_PROCESSING
        else:
            row.status = BookingStatus.EXPIRED
        db.commit()

    with patch.object(notification_tasks, "send_push_notification") as push:
        notification_tasks.send_booking_notification(
            booking_id=str(booking_id), event="chosen_sp_unavailable"
        )

    [call] = push.call_args_list
    assert call.kwargs["title"] == "Pick another pro"
    expected = "CoolFix couldn't take your booking — pick another pro."
    if refunded:
        expected += " Your full refund of ₹799 has been started."
    assert call.kwargs["body"] == expected
    with sync_factory() as db:
        consumer_token = db.get(Booking, booking_id).consumer.fcm_token
    assert call.kwargs["token"] == consumer_token, "consumer only"


def _offer_for(sync_factory, booking_id, sp_id):
    with sync_factory() as db:
        db.add(BookingOffer(booking_id=booking_id, provider_id=sp_id))
        db.commit()


def _expiries(sync_factory, booking_id) -> list[dict]:
    with sync_factory() as db:
        return [
            log.metadata_
            for log in db.scalars(
                select(BookingStatusLog).where(BookingStatusLog.booking_id == booking_id)
            )
        ]


def test_a_decline_holding_the_lock_beats_the_sweep_once(chosen_world):
    """The decline holds the row lock while the sweep's UPDATE waits on it; the
    UPDATE then re-checks REQUESTED and skips. One end, one push."""
    sync_factory, made, sp_id = chosen_world
    booking_id = _seed(
        sync_factory, made, paid=False, method=PaymentMethod.CASH, chosen_sp_id=sp_id
    )
    _offer_for(sync_factory, booking_id, sp_id)
    sent: list[tuple[str, str]] = []

    async def race():
        from app.tasks.cleanup_tasks import expire_stale_bookings

        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                await s.execute(select(Booking).where(Booking.id == booking_id).with_for_update())
                sweep = asyncio.create_task(asyncio.to_thread(expire_stale_bookings))
                await asyncio.sleep(1.0)  # the sweep is now blocked on our lock
                assert not sweep.done()
                await BookingService(s).decline_offer(booking_id, sp_id)
                await asyncio.wait_for(sweep, timeout=30)
        finally:
            await engine.dispose()

    with (
        patch(RZP) as rzp,
        patch.object(
            notification_tasks.send_booking_notification,
            "delay",
            side_effect=lambda **kw: sent.append((kw["booking_id"], kw["event"])),
        ),
    ):
        # Any other test's leftover paid booking the sweep finds refunds into a stub.
        rzp.payment.refund.return_value = {"id": "rfnd_race"}
        asyncio.run(race())

    assert _get(sync_factory, booking_id).status is BookingStatus.EXPIRED
    assert _expiries(sync_factory, booking_id) == [
        {"actor": "system", "reason": "chosen_sp_declined"}
    ]
    assert _pushed(sent, booking_id) == ["chosen_sp_unavailable"]


def test_a_decline_after_the_sweep_is_only_a_decline(chosen_world):
    sync_factory, made, sp_id = chosen_world
    booking_id = _seed(
        sync_factory, made, paid=False, method=PaymentMethod.CASH, chosen_sp_id=sp_id
    )
    _offer_for(sync_factory, booking_id, sp_id)

    _, swept, _ = _sweep()

    sent: list[tuple[str, str]] = []

    async def decline():
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                await BookingService(s).decline_offer(booking_id, sp_id)
        finally:
            await engine.dispose()

    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append((kw["booking_id"], kw["event"])),
    ):
        asyncio.run(decline())

    assert _get(sync_factory, booking_id).status is BookingStatus.EXPIRED
    assert _expiries(sync_factory, booking_id) == [{"reason": "offer_window_elapsed"}]
    assert _pushed(swept, booking_id) + _pushed(sent, booking_id) == ["chosen_sp_unavailable"]
