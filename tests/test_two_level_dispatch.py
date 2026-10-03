"""
SVC-320 (Phase B, PR 2): two-level dispatch.

SP accepts → SP picks a Pro → the Pro does the job, and a Pro can give it back.
The accept contract itself (403 / 404 / 410 / idempotent 200 with no second push)
is pinned in ``test_accept_contract.py``, unchanged; these tests pin what PR 2
adds on top:

- accept names the Pro: the claim sets ``professional_id`` to the Pro-for-self;
- the opt-in pick step (``X-Team-Pick: 1``) and ``POST /bookings/{id}/assign``,
  including re-pick, the busy check and the race;
- ``POST /pros/me/jobs/{id}/decline`` and ``POST /bookings/{id}/release``, and
  the reopen they lead to: a fresh offer window, and a re-broadcast that leaves
  out the SP who gave it back.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.address import Address
from app.models.booking import Booking, BookingStatus
from app.models.booking_offer import BookingOffer, OfferResponse
from app.models.booking_status_log import BookingStatusLog
from app.models.professional import Professional
from app.models.user import User
from app.services.booking_service import BookingService
from app.services.provider_matching import broadcast_recipients_stmt
from app.services.provider_service import ProviderService
from app.tasks import notification_tasks
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service

ACCEPT = "/api/v1/bookings/{}/accept"
ASSIGN = "/api/v1/bookings/{}/assign"
RELEASE = "/api/v1/bookings/{}/release"
DECLINE = "/api/v1/pros/me/jobs/{}/decline"
PICK = {"X-Team-Pick": "1"}


@pytest.fixture
def pushes():
    """Every ``send_booking_notification.delay`` call, as ``(event, kwargs)``.

    Patched on the task object, which every caller shares: the routers and
    ``notify_reopened`` alike.
    """
    sent: list[tuple[str, dict]] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append((kw["event"], kw)),
    ):
        yield sent


def _events(pushes) -> list[str]:
    return [event for event, _ in pushes]


async def _self_pro(db, sp_id) -> Professional:
    return await db.scalar(
        select(Professional).where(
            Professional.service_provider_id == sp_id, Professional.user_id == sp_id
        )
    )


async def _team_pro(db, sp_id, name="Rohan", active=True) -> Professional:
    """A team member, signed up the way invitees are: as a consumer."""
    user = await UserFactory.create(db, role="consumer", name=name)
    pro = Professional(
        service_provider_id=sp_id, user_id=user.id, display_name=name, is_active=active
    )
    db.add(pro)
    await db.flush()
    return pro


async def _team_sp(db, service):
    """An eligible SP with its Pro-for-self and one team Pro."""
    sp = await _eligible_provider(db, service)
    self_pro = await ProviderService(db).ensure_self_professional(sp.id)
    member = await _team_pro(db, sp.id)
    return sp, self_pro, member


def _as(auth_headers, user, role="provider", **extra):
    return {**auth_headers(str(user.id), role), **extra}


async def _accepted_by(client, auth_headers, db, booking, sp):
    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    await db.refresh(booking)
    return booking


# --- Accept names the Pro ------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_sets_the_pro_for_self(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    await _accepted_by(client, auth_headers, db_session, booking, sp)

    self_pro = await _self_pro(db_session, sp.id)
    assert booking.status is BookingStatus.ACCEPTED
    assert booking.provider_id == sp.id
    assert booking.professional_id == self_pro.id
    assert _events(pushes) == ["accepted"]


@pytest.mark.asyncio
async def test_accept_repairs_a_missing_pro_for_self(client, db_session, auth_headers, pushes):
    """The fixture provider has no Pro row at all; accept must still work."""
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    assert await _self_pro(db_session, sp.id) is None
    _, booking = await _booking(db_session, service=service)

    await _accepted_by(client, auth_headers, db_session, booking, sp)
    assert booking.professional_id == (await _self_pro(db_session, sp.id)).id


@pytest.mark.asyncio
async def test_a_team_sp_without_the_header_still_claims(client, db_session, auth_headers, pushes):
    """Old clients treat any 2xx as a win, so no header means a real claim."""
    service = await _service(db_session)
    sp, self_pro, _ = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)

    await _accepted_by(client, auth_headers, db_session, booking, sp)
    assert booking.professional_id == self_pro.id


# --- The opt-in pick step ------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_pick_returns_202_with_the_team_and_claims_nothing(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    busy = await _team_pro(db_session, sp.id, name="Busy")
    _, other = await _booking(db_session, service=service, provider_id=sp.id)
    other.professional_id = busy.id
    other.status = BookingStatus.EN_ROUTE
    _, booking = await _booking(db_session, service=service)

    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp, **PICK))

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["booking_id"] == str(booking.id)
    rows = [(p["id"], p["is_self"], p["busy"]) for p in body["pros"]]
    # Pro-for-self first. The rest are by join date, which is a tie here
    # (one test transaction, one now()), so compare them as a set.
    assert rows[0] == (str(self_pro.id), True, False)
    assert set(rows[1:]) == {(str(member.id), False, False), (str(busy.id), False, True)}
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED and booking.provider_id is None
    assert pushes == []


@pytest.mark.asyncio
async def test_the_pick_header_on_a_solo_sp_is_a_plain_accept(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp, **PICK))
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_the_pick_step_keeps_accepts_403_and_410(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    stranger = await _eligible_provider(db_session, await _service(db_session))
    _, booking = await _booking(db_session, service=service)

    resp = await client.patch(
        ACCEPT.format(booking.id), headers=_as(auth_headers, stranger, **PICK)
    )
    assert resp.status_code == 403

    booking.status = BookingStatus.CANCELLED
    await db_session.flush()
    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp, **PICK))
    assert resp.status_code == 410


# --- Assign --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_claims_for_the_picked_pro(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    db_session.add(BookingOffer(booking_id=booking.id, provider_id=sp.id))
    await db_session.flush()

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.ACCEPTED
    assert (booking.provider_id, booking.professional_id) == (sp.id, member.id)
    offer = await db_session.scalar(
        select(BookingOffer).where(BookingOffer.booking_id == booking.id)
    )
    assert offer.response is OfferResponse.ACCEPTED
    log = await db_session.scalar(
        select(BookingStatusLog).where(BookingStatusLog.booking_id == booking.id)
    )
    assert (log.from_status, log.to_status, log.changed_by) == ("requested", "accepted", sp.id)
    assert _events(pushes) == ["accepted", "assigned"]


@pytest.mark.asyncio
async def test_assigning_yourself_sends_no_assigned_push(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, self_pro, _ = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(self_pro.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 200
    assert _events(pushes) == ["accepted"]


@pytest.mark.asyncio
async def test_assign_refuses_a_pro_not_on_your_active_team(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    other_sp, _, other_member = await _team_sp(db_session, service)
    removed = await _team_pro(db_session, sp.id, name="Gone", active=False)
    _, booking = await _booking(db_session, service=service)

    for pro_id in (other_member.id, removed.id, uuid.uuid4()):
        resp = await client.post(
            ASSIGN.format(booking.id),
            json={"professional_id": str(pro_id)},
            headers=_as(auth_headers, sp),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "PRO_NOT_FOUND"
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED
    assert pushes == []


@pytest.mark.asyncio
async def test_assign_keeps_accepts_403_and_410(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    winner, _, _ = await _team_sp(db_session, service)
    ineligible = await _eligible_provider(db_session, service, online=False)
    ineligible_pro = await _team_pro(db_session, ineligible.id)
    _, booking = await _booking(db_session, service=service)
    body = {"professional_id": str(member.id)}

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(ineligible_pro.id)},
        headers=_as(auth_headers, ineligible),
    )
    assert resp.status_code == 403

    await _accepted_by(client, auth_headers, db_session, booking, winner)
    resp = await client.post(ASSIGN.format(booking.id), json=body, headers=_as(auth_headers, sp))
    assert resp.status_code == 410

    _, cancelled = await _booking(db_session, service=service, status=BookingStatus.CANCELLED)
    resp = await client.post(ASSIGN.format(cancelled.id), json=body, headers=_as(auth_headers, sp))
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_assign_is_provider_only(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    _, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, await UserFactory.create(db_session), role="consumer"),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_assign_refuses_a_busy_pro(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, other = await _booking(db_session, service=service, provider_id=sp.id)
    other.professional_id = member.id
    other.status = BookingStatus.IN_PROGRESS
    _, booking = await _booking(db_session, service=service)

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "PRO_BUSY"

    # A finished job doesn't count.
    other.status = BookingStatus.COMPLETED
    await db_session.flush()
    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_repick_replaces_the_pro_without_a_second_confirmation(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    assert booking.professional_id == self_pro.id
    pushes.clear()

    body = {"professional_id": str(member.id)}
    resp = await client.post(ASSIGN.format(booking.id), json=body, headers=_as(auth_headers, sp))
    assert resp.status_code == 200
    await db_session.refresh(booking)
    assert booking.professional_id == member.id
    assert booking.status is BookingStatus.ACCEPTED
    assert _events(pushes) == ["assigned"], "the consumer was already told"

    # The same Pro again: idempotent, silent.
    resp = await client.post(ASSIGN.format(booking.id), json=body, headers=_as(auth_headers, sp))
    assert resp.status_code == 200
    assert _events(pushes) == ["assigned"]


@pytest.mark.asyncio
async def test_no_reassigning_once_en_route(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service, provider_id=sp.id)
    booking.status = BookingStatus.EN_ROUTE
    await db_session.flush()

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_two_sps_assigning_at_once_produce_exactly_one_winner(db_session):
    """Real concurrency on committed data, as in the accept race test: A holds
    the row lock until B has started, so the two genuinely contend."""
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict = {}
    try:
        async with sessions() as setup:
            service = await _service(setup)
            a, _, a_pro = await _team_sp(setup, service)
            b, _, b_pro = await _team_sp(setup, service)
            _, booking = await _booking(setup, service=service)
            await setup.commit()
            ids = {"booking": booking.id, "a": (a.id, a_pro.id), "b": (b.id, b_pro.id)}

        a_holds_lock = asyncio.Event()

        async def run(sp_id, pro_id, *, first):
            if not first:
                await a_holds_lock.wait()
            async with sessions() as s:
                try:
                    _, newly, _ = await BookingService(s).assign(ids["booking"], sp_id, pro_id)
                    if first:
                        a_holds_lock.set()
                        await asyncio.sleep(0.5)
                    await s.commit()
                    return newly
                except Exception as exc:
                    a_holds_lock.set()
                    await s.rollback()
                    return exc

        results = await asyncio.wait_for(
            asyncio.gather(run(*ids["a"], first=True), run(*ids["b"], first=False)), timeout=30
        )
        winners = [r for r in results if r is True]
        losers = [r for r in results if isinstance(r, Exception)]
        assert len(winners) == 1 and len(losers) == 1, results
        assert losers[0].status_code == 410

        async with sessions() as check:
            final = await check.get(Booking, ids["booking"])
            assert final.status is BookingStatus.ACCEPTED
            assert (final.provider_id, final.professional_id) in (ids["a"], ids["b"])
    finally:
        if ids:
            async with sessions() as cleanup:
                from sqlalchemy import delete, text

                await cleanup.execute(
                    text("DELETE FROM booking_status_logs WHERE booking_id = :b"),
                    {"b": str(ids["booking"])},
                )
                await cleanup.execute(delete(Booking).where(Booking.id == ids["booking"]))
                await cleanup.commit()
        await engine.dispose()


# --- Decline -----------------------------------------------------------------------


async def _assigned_to(client, auth_headers, db, booking, sp, pro):
    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(pro.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 200, resp.text
    await db.refresh(booking)


@pytest.mark.asyncio
async def test_a_team_pro_declines_and_the_sp_is_asked_to_repick(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)
    pushes.clear()

    # The Pro signed up as a consumer, and that's the role they call with.
    member_user = await db_session.get(User, member.user_id)
    resp = await client.post(
        DECLINE.format(booking.id), headers=_as(auth_headers, member_user, role="consumer")
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"booking_id": str(booking.id), "outcome": "awaiting_repick"}
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.ACCEPTED
    assert (booking.provider_id, booking.professional_id) == (sp.id, None)
    [(event, kwargs)] = pushes
    assert event == "pro_declined" and kwargs["actor_id"] == str(member.user_id)


@pytest.mark.asyncio
async def test_only_the_assigned_active_pro_can_decline(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    other = await _team_pro(db_session, sp.id, name="Other")
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    for user_id in (other.user_id, booking.consumer_id, uuid.uuid4()):
        user = await db_session.get(User, user_id) or await UserFactory.create(db_session)
        resp = await client.post(
            DECLINE.format(booking.id), headers=_as(auth_headers, user, role="consumer")
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "JOB_NOT_FOUND"

    # Removed from the team after being assigned: no longer theirs to decline.
    member.is_active = False
    await db_session.flush()
    member_user = await db_session.get(User, member.user_id)
    resp = await client.post(
        DECLINE.format(booking.id), headers=_as(auth_headers, member_user, role="consumer")
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_no_declining_once_en_route(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)
    booking.status = BookingStatus.EN_ROUTE
    await db_session.flush()

    member_user = await db_session.get(User, member.user_id)
    resp = await client.post(
        DECLINE.format(booking.id), headers=_as(auth_headers, member_user, role="consumer")
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_the_last_pro_declining_reopens_the_booking(client, db_session, auth_headers, pushes):
    """A solo SP declining their own job: nobody else to give it to."""
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    db_session.add(BookingOffer(booking_id=booking.id, provider_id=sp.id))
    await db_session.flush()
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    pushes.clear()

    resp = await client.post(DECLINE.format(booking.id), headers=_as(auth_headers, sp))

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "reopened"
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED
    assert (booking.provider_id, booking.professional_id) == (None, None)
    assert booking.reopened_at is not None
    assert booking.doorstep_otp is not None, "the consumer's OTP still stands"
    logs = (
        await db_session.scalars(
            select(BookingStatusLog)
            .where(BookingStatusLog.booking_id == booking.id)
            .order_by(BookingStatusLog.created_at, BookingStatusLog.to_status.desc())
        )
    ).all()
    reopen = next(log for log in logs if log.to_status == "requested")
    assert reopen.from_status == "accepted"
    assert reopen.metadata_["reason"] == "pro_declined"
    assert reopen.metadata_["released_by_provider"] == str(sp.id)
    assert sorted(_events(pushes)) == ["new_request", "reopened"]


# --- Release + reopen --------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_reopens_and_the_window_restarts(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    # Created 10 minutes ago: well past the 2-minute window by created_at.
    booking.created_at = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.flush()
    pushes.clear()

    resp = await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "requested"
    assert sorted(_events(pushes)) == ["new_request", "reopened"]

    await db_session.refresh(booking)
    assert booking.offer_window_start == booking.reopened_at
    _, remaining = BookingService._offer_expiry(booking)
    assert remaining > 100, "a reopened booking gets a fresh window, not 0s"
    # The SQL side of the same rule, which the expiry sweep uses.
    cutoff = datetime.now(UTC) - timedelta(minutes=2)
    still_open = await db_session.scalar(
        select(Booking.id).where(Booking.id == booking.id, Booking.offer_window_start > cutoff)
    )
    assert still_open == booking.id


@pytest.mark.asyncio
async def test_release_rules(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    stranger, _, _ = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)

    resp = await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, stranger))
    assert resp.status_code == 403

    booking.status = BookingStatus.EN_ROUTE
    await db_session.flush()
    resp = await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_a_reopened_booking_skips_the_sp_who_gave_it_back(
    client, db_session, auth_headers, pushes
):
    """Both channels: the re-broadcast push and the offer poll. Anyone else
    eligible still gets it, and can claim it."""
    service = await _service(db_session)
    releaser, _, _ = await _team_sp(db_session, service)
    other = await _eligible_provider(db_session, service)
    declined = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    db_session.add(
        BookingOffer(
            booking_id=booking.id,
            provider_id=declined.id,
            response=OfferResponse.DECLINED,
            responded_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    await _accepted_by(client, auth_headers, db_session, booking, releaser)
    await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, releaser))

    address = await db_session.get(Address, booking.consumer_address_id)
    recipients = (
        await db_session.scalars(
            broadcast_recipients_stmt([service.id], address.location, booking.id)
        )
    ).all()
    assert {u.id for u in recipients} == {other.id}

    service_ = BookingService(db_session)
    assert [o.booking_id for o in await service_.list_open_offers(other.id)] == [booking.id]
    assert await service_.list_open_offers(releaser.id) == []

    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, other))
    assert resp.status_code == 200
    await db_session.refresh(booking)
    assert booking.provider_id == other.id
    assert booking.professional_id == (await _self_pro(db_session, other.id)).id
