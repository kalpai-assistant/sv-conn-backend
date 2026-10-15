"""
SVC-364: the SP owner moving a booking that has no Pro (a team Pro declined and
the owner went without re-picking) is recorded as doing it themselves. Their
Pro-for-self is attached, with one ``claimed`` audit row, in the transition's
own transaction.

Before, the job finished with ``professional_id`` NULL: earnings in "Not
assigned", an audit trail that stopped at the decline, and a consumer still
told we were finding someone.
"""

import pytest
from sqlalchemy import select

from app.models.booking import BookingStatus
from app.models.booking_assignment_event import BookingAssignmentEvent
from app.models.user import User
from tests.test_accept_contract import _booking, _service
from tests.test_hide_declined_pro import _declined_by_member
from tests.test_self_pro_toggle import _switch
from tests.test_two_level_dispatch import (
    _as,
    _assigned_to,
    _team_sp,
    pushes,  # noqa: F401  (fixture)
)

BOOKING = "/api/v1/bookings/{}"
JOBS = "/api/v1/pros/me/jobs/{}"

# Every test stubs the push task; none of them asserts on pushes.
pytestmark = pytest.mark.usefixtures("pushes")


async def _events(db, booking_id) -> list[BookingAssignmentEvent]:
    return (
        await db.scalars(
            select(BookingAssignmentEvent)
            .where(BookingAssignmentEvent.booking_id == booking_id)
            .order_by(BookingAssignmentEvent.created_at, BookingAssignmentEvent.id)
        )
    ).all()


async def _claimed(db, booking_id, self_pro) -> list[BookingAssignmentEvent]:
    """The ``claimed`` rows naming the owner's Pro-for-self. Not every
    ``claimed`` row: ``/assign``'s first claim of the team Pro is one too."""
    return [
        e
        for e in await _events(db, booking_id)
        if e.kind == "claimed" and e.to_professional_id == self_pro.id
    ]


async def _en_route_with_no_pro(db, booking):
    """Already on the way with nobody attached: what a pre-SVC-364 owner
    en-route left behind, and the only way start/complete meet a null Pro."""
    booking.status = BookingStatus.EN_ROUTE
    await db.flush()
    assert booking.professional_id is None


def _assert_claimed_by_owner(row, sp, self_pro):
    assert (row.kind, row.from_professional_id, row.to_professional_id, row.actor_user_id) == (
        "claimed",
        None,
        self_pro.id,
        sp.id,
    )


# --- The owner self-services -------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_en_route_after_a_decline_attaches_their_pro_for_self(
    client, db_session, auth_headers
):
    _, sp, self_pro, *_, booking = await _declined_by_member(client, auth_headers, db_session)

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/en-route", headers=_as(auth_headers, sp)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["professional_id"] == str(self_pro.id)
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.EN_ROUTE
    assert booking.professional_id == self_pro.id
    [row] = await _claimed(db_session, booking.id, self_pro)
    _assert_claimed_by_owner(row, sp, self_pro)


@pytest.mark.asyncio
async def test_owner_start_on_a_null_pro_booking_attaches(client, db_session, auth_headers):
    _, sp, self_pro, *_, booking = await _declined_by_member(client, auth_headers, db_session)
    await _en_route_with_no_pro(db_session, booking)

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/start",
        json={"otp": booking.doorstep_otp},
        headers=_as(auth_headers, sp),
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.IN_PROGRESS
    assert booking.professional_id == self_pro.id
    [row] = await _claimed(db_session, booking.id, self_pro)
    _assert_claimed_by_owner(row, sp, self_pro)


@pytest.mark.asyncio
async def test_owner_complete_on_a_null_pro_booking_attaches(client, db_session, auth_headers):
    _, sp, self_pro, *_, booking = await _declined_by_member(client, auth_headers, db_session)
    booking.status = BookingStatus.IN_PROGRESS
    await db_session.flush()

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/complete", headers=_as(auth_headers, sp)
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.COMPLETED
    assert booking.professional_id == self_pro.id
    [row] = await _claimed(db_session, booking.id, self_pro)
    _assert_claimed_by_owner(row, sp, self_pro)


@pytest.mark.asyncio
async def test_attached_once_then_the_rest_of_the_job_leaves_it_alone(
    client, db_session, auth_headers
):
    _, sp, self_pro, *_, booking = await _declined_by_member(client, auth_headers, db_session)
    headers = _as(auth_headers, sp)
    base = BOOKING.format(booking.id)

    assert (await client.patch(f"{base}/en-route", headers=headers)).status_code == 200
    await db_session.refresh(booking)
    resp = await client.patch(f"{base}/start", json={"otp": booking.doorstep_otp}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert (await client.patch(f"{base}/complete", headers=headers)).status_code == 200

    await db_session.refresh(booking)
    assert booking.professional_id == self_pro.id
    assert len(await _claimed(db_session, booking.id, self_pro)) == 1


@pytest.mark.asyncio
async def test_attached_even_when_the_owner_switched_self_off(client, db_session, auth_headers):
    """Option A: ``accepts_direct_jobs`` governs who can be assigned, not who
    did the work. No SELF_NOT_TAKING_JOBS here, and not left NULL."""
    _, sp, self_pro, *_, booking = await _declined_by_member(client, auth_headers, db_session)
    assert (await _switch(client, auth_headers, sp, False)).status_code == 200
    await db_session.refresh(self_pro)
    assert self_pro.accepts_direct_jobs is False

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/en-route", headers=_as(auth_headers, sp)
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.professional_id == self_pro.id
    [row] = await _claimed(db_session, booking.id, self_pro)
    _assert_claimed_by_owner(row, sp, self_pro)


@pytest.mark.asyncio
async def test_the_consumer_then_sees_the_owner_as_their_professional(
    client, db_session, auth_headers
):
    _, sp, self_pro, *_, booking = await _declined_by_member(client, auth_headers, db_session)
    consumer = await db_session.get(User, booking.consumer_id)
    headers = _as(auth_headers, consumer, role="consumer")

    before = (await client.get(BOOKING.format(booking.id), headers=headers)).json()
    assert before["professional"] is None

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/en-route", headers=_as(auth_headers, sp)
    )
    assert resp.status_code == 200, resp.text

    after = (await client.get(BOOKING.format(booking.id), headers=headers)).json()
    assert after["professional_id"] == str(self_pro.id)
    assert after["professional"]["id"] == str(self_pro.id)
    assert after["professional"]["is_self"] is True


# --- Where it must not attach ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wrong_otp_on_start_attaches_nothing(client, db_session, auth_headers):
    _, sp, _, *_, booking = await _declined_by_member(client, auth_headers, db_session)
    await _en_route_with_no_pro(db_session, booking)
    events_before = len(await _events(db_session, booking.id))
    not_it = "0000" if booking.doorstep_otp != "0000" else "1111"

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/start", json={"otp": not_it}, headers=_as(auth_headers, sp)
    )

    assert resp.status_code != 200
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.EN_ROUTE
    assert booking.professional_id is None
    assert len(await _events(db_session, booking.id)) == events_before


@pytest.mark.asyncio
async def test_owner_reassigning_after_a_decline_keeps_their_pick(client, db_session, auth_headers):
    _, sp, self_pro, _, other, booking = await _declined_by_member(client, auth_headers, db_session)
    await _assigned_to(client, auth_headers, db_session, booking, sp, other)

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/en-route", headers=_as(auth_headers, sp)
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.professional_id == other.id
    assert await _claimed(db_session, booking.id, self_pro) == []


@pytest.mark.asyncio
async def test_a_booking_that_already_has_a_pro_is_untouched(client, db_session, auth_headers):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)
    events_before = len(await _events(db_session, booking.id))

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/en-route", headers=_as(auth_headers, sp)
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.professional_id == member.id
    assert len(await _events(db_session, booking.id)) == events_before


@pytest.mark.asyncio
async def test_a_consumer_cancel_on_a_null_pro_booking_attaches_nothing(
    client, db_session, auth_headers
):
    _, _, _, *_, booking = await _declined_by_member(client, auth_headers, db_session)
    consumer = await db_session.get(User, booking.consumer_id)
    events_before = len(await _events(db_session, booking.id))

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/cancel",
        json={"reason": "changed my mind"},
        headers=_as(auth_headers, consumer, role="consumer"),
    )

    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.CANCELLED
    assert booking.professional_id is None
    assert len(await _events(db_session, booking.id)) == events_before


@pytest.mark.asyncio
async def test_a_team_pro_on_its_own_route_gets_a_404(client, db_session, auth_headers):
    _, _, _, member, _, booking = await _declined_by_member(client, auth_headers, db_session)
    await _en_route_with_no_pro(db_session, booking)
    member_user = await db_session.get(User, member.user_id)
    events_before = len(await _events(db_session, booking.id))

    resp = await client.post(
        f"{JOBS.format(booking.id)}/start",
        json={"otp": booking.doorstep_otp},
        headers=_as(auth_headers, member_user, role="consumer"),
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "JOB_NOT_FOUND"
    await db_session.refresh(booking)
    assert booking.professional_id is None
    assert len(await _events(db_session, booking.id)) == events_before


@pytest.mark.asyncio
async def test_a_team_pro_on_the_sp_route_is_refused(client, db_session, auth_headers):
    """The SP routes have no role gate, so this path is real: the Pro reaches
    ``transition_status`` and is refused there, before any attach."""
    _, _, _, member, _, booking = await _declined_by_member(client, auth_headers, db_session)
    await _en_route_with_no_pro(db_session, booking)
    member_user = await db_session.get(User, member.user_id)
    events_before = len(await _events(db_session, booking.id))

    resp = await client.patch(
        f"{BOOKING.format(booking.id)}/start",
        json={"otp": booking.doorstep_otp},
        headers=_as(auth_headers, member_user, role="consumer"),
    )

    assert resp.status_code == 403, resp.text
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.EN_ROUTE
    assert booking.professional_id is None
    assert len(await _events(db_session, booking.id)) == events_before
