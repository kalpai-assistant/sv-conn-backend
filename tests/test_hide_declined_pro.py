"""
SVC-361: a Pro who declined a booking is left out of the SP's re-pick list and
refused at ``/assign``, so the owner can't hand the job straight back to them.

Read from the SVC-333 audit trail (``booking_assignment_events``, kind
``declined``); the decline itself and its row are unchanged.
"""

import pytest
from sqlalchemy import select

from app.models.booking_assignment_event import BookingAssignmentEvent
from app.models.booking_offer import BookingOffer
from app.models.user import User
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_two_level_dispatch import (
    ASSIGN,
    DECLINE,
    _accepted_by,
    _as,
    _assigned_to,
    _team_pro,
    _team_sp,
    pushes,  # noqa: F401  (fixture)
)

TEAM = "/api/v1/providers/me/team"

# Every test stubs the push task; none of them asserts on pushes.
pytestmark = pytest.mark.usefixtures("pushes")


async def _declined_by_member(client, auth_headers, db):
    """An SP with its Pro-for-self and two team Pros; ``member`` was assigned
    the booking and declined it, so it waits at ACCEPTED for a re-pick."""
    service = await _service(db)
    sp, self_pro, member = await _team_sp(db, service)
    other = await _team_pro(db, sp.id, name="Other")
    _, booking = await _booking(db, service=service)
    await _assigned_to(client, auth_headers, db, booking, sp, member)
    member_user = await db.get(User, member.user_id)
    resp = await client.post(
        DECLINE.format(booking.id), headers=_as(auth_headers, member_user, role="consumer")
    )
    assert resp.json()["outcome"] == "awaiting_repick", resp.text
    return service, sp, self_pro, member, other, booking


async def _team_ids(client, auth_headers, sp, **params) -> list[str]:
    resp = await client.get(TEAM, params=params, headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    return [p["id"] for p in resp.json()]


async def _decline_rows(db, booking_id) -> list[BookingAssignmentEvent]:
    return (
        await db.scalars(
            select(BookingAssignmentEvent).where(
                BookingAssignmentEvent.booking_id == booking_id,
                BookingAssignmentEvent.kind == "declined",
            )
        )
    ).all()


# --- The re-pick list ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_team_list_for_a_booking_leaves_out_the_decliner(
    client, db_session, auth_headers
):
    _, sp, self_pro, member, other, booking = await _declined_by_member(
        client, auth_headers, db_session
    )

    ids = await _team_ids(client, auth_headers, sp, booking_id=str(booking.id))

    # Pro-for-self still first, the rest in join order.
    assert ids == [str(self_pro.id), str(other.id)]


@pytest.mark.asyncio
async def test_the_team_list_without_a_booking_is_unchanged(client, db_session, auth_headers):
    _, sp, self_pro, member, other, _ = await _declined_by_member(client, auth_headers, db_session)

    ids = await _team_ids(client, auth_headers, sp)

    # Same-transaction Pros tie on created_at, so only the head is ordered.
    assert ids[0] == str(self_pro.id)
    assert set(ids[1:]) == {str(member.id), str(other.id)}


@pytest.mark.asyncio
async def test_a_booking_that_isnt_yours_hides_nobody(client, db_session, auth_headers):
    """Ignored, not a 404: the lookup only sees the caller's own Pros, so
    another SP's declines never show, and a foreign id answers like an
    unknown one."""
    service, sp, *_, booking = await _declined_by_member(client, auth_headers, db_session)
    stranger, stranger_self, stranger_member = await _team_sp(db_session, service)
    full = [str(stranger_self.id), str(stranger_member.id)]

    assert await _team_ids(client, auth_headers, stranger, booking_id=str(booking.id)) == full
    unknown = "00000000-0000-0000-0000-000000000000"
    assert await _team_ids(client, auth_headers, stranger, booking_id=unknown) == full


# --- /assign -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_refuses_the_decliner_and_takes_anyone_else(client, db_session, auth_headers):
    _, sp, _, member, other, booking = await _declined_by_member(client, auth_headers, db_session)

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "PRO_DECLINED_THIS_BOOKING"
    assert resp.json()["message"] == "This Pro already declined this booking."
    await db_session.refresh(booking)
    assert booking.professional_id is None

    # The audit trail is untouched: the decline row is still there.
    [row] = await _decline_rows(db_session, booking.id)
    assert row.from_professional_id == member.id

    await _assigned_to(client, auth_headers, db_session, booking, sp, other)
    assert booking.professional_id == other.id


@pytest.mark.asyncio
async def test_an_owner_who_declined_cant_reassign_themselves(client, db_session, auth_headers):
    """The Pro-for-self is no exception. The SP has another Pro, so its own
    decline leaves the booking waiting for a re-pick rather than reopening."""
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    assert booking.professional_id == self_pro.id

    resp = await client.post(DECLINE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.json()["outcome"] == "awaiting_repick", resp.text

    assert await _team_ids(client, auth_headers, sp, booking_id=str(booking.id)) == [str(member.id)]
    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(self_pro.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "PRO_DECLINED_THIS_BOOKING"
    [row] = await _decline_rows(db_session, booking.id)
    assert row.from_professional_id == self_pro.id

    await _assigned_to(client, auth_headers, db_session, booking, sp, member)


@pytest.mark.asyncio
async def test_a_solo_owner_who_declined_cant_claim_it_back_by_assign(
    client, db_session, auth_headers
):
    """A solo SP's decline reopens the booking. The re-broadcast leaves the SP
    out, but the claim preconditions don't refuse it, so ``/assign`` in claim
    mode reaches the SVC-361 check and that is what refuses it. (``/accept``
    has no such check and still lets the SP claim it back: out of scope here.)"""
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    db_session.add(BookingOffer(booking_id=booking.id, provider_id=sp.id))
    await db_session.flush()
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    self_pro_id = booking.professional_id

    resp = await client.post(DECLINE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.json()["outcome"] == "reopened", resp.text

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(self_pro_id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "PRO_DECLINED_THIS_BOOKING"
    await db_session.refresh(booking)
    assert (booking.status.value, booking.provider_id) == ("requested", None)
