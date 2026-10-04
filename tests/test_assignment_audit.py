"""
SVC-333: the assignment audit trail and the admin booking timeline.

Every change of a booking's Pro writes exactly one ``booking_assignment_events``
row: claimed (accept / assign), repicked, declined, released, provider_cancelled.
Nothing else does: a same-Pro re-assign, a failed assign, the SVC-344 no-taker
cancel, an admin force-cancel, a consumer cancel. The consumer's
``status_history`` never sees any of it. ``GET /admin/bookings/{id}/timeline``
merges both tables, oldest first.

The harness never commits (one transaction per test), so every ``now()`` in a
test is the same instant, across requests too. The ordering tests push earlier
requests' rows into the past (``_earlier``), leaving only the tie a real
request has: its own status row and assignment row, stamped alike.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.booking import BookingStatus
from app.models.booking_assignment_event import BookingAssignmentEvent
from app.models.booking_status_log import BookingStatusLog
from app.models.user import User
from app.services.booking_service import BookingService
from app.tasks import notification_tasks
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_paid_no_taker import RZP, _ago, _paid_open_booking
from tests.test_two_level_dispatch import (
    ASSIGN,
    DECLINE,
    RELEASE,
    _accepted_by,
    _as,
    _assigned_to,
    _self_pro,
    _team_pro,
    _team_sp,
)

TIMELINE = "/api/v1/admin/bookings/{}/timeline"


@pytest.fixture
def pushes():
    """Swallow the pushes the routes enqueue; this file doesn't assert on them."""
    with patch.object(notification_tasks.send_booking_notification, "delay"):
        yield


async def _events(db, booking) -> list[tuple]:
    rows = (
        await db.scalars(
            select(BookingAssignmentEvent).where(BookingAssignmentEvent.booking_id == booking.id)
        )
    ).all()
    return [(e.kind, e.from_professional_id, e.to_professional_id, e.actor_user_id) for e in rows]


async def _earlier(db, booking, minutes=1):
    """Move every row so far ``minutes`` into the past: what a later commit
    would look like. Leaves only the tie *within* the next request."""
    for model in (BookingStatusLog, BookingAssignmentEvent):
        await db.execute(
            update(model)
            .where(model.booking_id == booking.id)
            .values(created_at=model.created_at - timedelta(minutes=minutes))
        )


async def _assign(client, auth_headers, booking, sp, pro):
    return await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(pro.id)},
        headers=_as(auth_headers, sp),
    )


async def _admin_timeline(client, auth_headers, db, booking):
    admin = await UserFactory.create(db, role="admin")
    resp = await client.get(
        TIMELINE.format(booking.id), headers=auth_headers(str(admin.id), "admin")
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- One row per kind ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_records_a_claim_for_the_pro_for_self(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    await _accepted_by(client, auth_headers, db_session, booking, sp)

    self_pro = await _self_pro(db_session, sp.id)
    assert await _events(db_session, booking) == [("claimed", None, self_pro.id, sp.id)]

    # An idempotent accept retry is not a second claim.
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    assert len(await _events(db_session, booking)) == 1


@pytest.mark.asyncio
async def test_assign_records_a_claim_then_a_repick(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)

    await _assigned_to(client, auth_headers, db_session, booking, sp, member)
    assert await _events(db_session, booking) == [("claimed", None, member.id, sp.id)]

    await _assigned_to(client, auth_headers, db_session, booking, sp, self_pro)
    assert (await _events(db_session, booking))[1:] == [("repicked", member.id, self_pro.id, sp.id)]

    # The same Pro again: idempotent, no row.
    resp = await _assign(client, auth_headers, booking, sp, self_pro)
    assert resp.status_code == 200
    assert len(await _events(db_session, booking)) == 2


@pytest.mark.asyncio
async def test_decline_records_the_pro_leaving_and_a_repick_after_it_starts_from_nobody(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    member_user = await db_session.get(User, member.user_id)
    resp = await client.post(
        DECLINE.format(booking.id), headers=_as(auth_headers, member_user, role="consumer")
    )
    assert resp.json()["outcome"] == "awaiting_repick"

    await _assigned_to(client, auth_headers, db_session, booking, sp, self_pro)
    assert (await _events(db_session, booking))[1:] == [
        ("declined", member.id, None, member.user_id),
        ("repicked", None, self_pro.id, sp.id),
    ]


@pytest.mark.asyncio
async def test_a_decline_that_reopens_writes_one_event_and_one_status_row(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    self_pro = await _self_pro(db_session, sp.id)

    resp = await client.post(DECLINE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.json()["outcome"] == "reopened"

    assert (await _events(db_session, booking))[1:] == [("declined", self_pro.id, None, sp.id)]
    reopens = (
        await db_session.scalars(
            select(BookingStatusLog).where(
                BookingStatusLog.booking_id == booking.id,
                BookingStatusLog.to_status == "requested",
            )
        )
    ).all()
    assert len(reopens) == 1


@pytest.mark.asyncio
async def test_release_records_the_current_pro_leaving(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    resp = await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text

    assert (await _events(db_session, booking))[1:] == [("released", member.id, None, sp.id)]


@pytest.mark.asyncio
async def test_provider_cancel_while_accepted_records_provider_cancelled(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    self_pro = await _self_pro(db_session, sp.id)

    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/cancel",
        json={"reason": "can't make it"},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "requested"

    assert (await _events(db_session, booking))[1:] == [
        ("provider_cancelled", self_pro.id, None, sp.id)
    ]


# --- No row ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_assigns_record_nothing(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    offline = await _team_pro(db_session, sp.id, name="Asleep", online=False)
    busy = await _team_pro(db_session, sp.id, name="Busy")
    _, elsewhere = await _booking(db_session, service=service, provider_id=sp.id)
    elsewhere.professional_id = busy.id
    elsewhere.status = BookingStatus.IN_PROGRESS
    winner, _, _ = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await db_session.flush()

    for pro, code in ((offline, 409), (busy, 409)):
        resp = await _assign(client, auth_headers, booking, sp, pro)
        assert resp.status_code == code, resp.text
    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(uuid.uuid4())},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 404
    assert await _events(db_session, booking) == []

    # Lost race: the winner's claim is the only row.
    await _accepted_by(client, auth_headers, db_session, booking, winner)
    resp = await _assign(client, auth_headers, booking, sp, member)
    assert resp.status_code == 410
    assert [kind for kind, *_ in await _events(db_session, booking)] == ["claimed"]


@pytest.mark.asyncio
async def test_cancels_that_are_not_a_provider_backing_out_record_nothing(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    admin = await UserFactory.create(db_session, role="admin")

    # Consumer cancel of an accepted booking.
    consumer, by_consumer = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, by_consumer, sp)
    with patch(RZP):
        resp = await client.patch(
            f"/api/v1/bookings/{by_consumer.id}/cancel",
            json={"reason": "changed my mind"},
            headers=auth_headers(str(consumer.id), "consumer"),
        )
    assert resp.status_code == 200, resp.text
    assert [kind for kind, *_ in await _events(db_session, by_consumer)] == ["claimed"]

    # Admin force-cancel of an accepted booking.
    _, by_admin = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, by_admin, sp)
    with patch(RZP):
        resp = await client.post(
            f"/api/v1/admin/bookings/{by_admin.id}/cancel",
            json={"reason": "duplicate booking flagged by ops"},
            headers=auth_headers(str(admin.id), "admin"),
        )
    assert resp.status_code == 200, resp.text
    assert [kind for kind, *_ in await _events(db_session, by_admin)] == ["claimed"]

    # SVC-344 no-taker system cancel.
    no_taker = await _paid_open_booking(db_session)
    no_taker.last_chance_at = _ago(95)
    await db_session.flush()
    with patch(RZP) as rzp:
        rzp.payment.refund.return_value = {"id": "rfnd_no_taker"}
        assert await BookingService(db_session).advance_paid_unclaimed(no_taker.id) == "refunded"
    assert await _events(db_session, no_taker) == []


# --- The consumer never sees it -------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_status_history_is_unchanged_by_repicks(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    consumer, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)
    await _assigned_to(client, auth_headers, db_session, booking, sp, self_pro)
    assert len(await _events(db_session, booking)) == 3

    resp = await client.get(
        f"/api/v1/bookings/{booking.id}", headers=auth_headers(str(consumer.id), "consumer")
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [h["to_status"] for h in body["status_history"]] == ["accepted"]
    assert all(set(h) == {"to_status", "created_at"} for h in body["status_history"])
    assert "repicked" not in resp.text and "claimed" not in resp.text


# --- The admin timeline ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeline_puts_a_claim_after_its_own_accept(client, db_session, auth_headers, pushes):
    """Same transaction, same ``now()``: requested → accepted, then "claimed",
    then the re-pick."""
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    await _earlier(db_session, booking)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    items = await _admin_timeline(client, auth_headers, db_session, booking)

    assert [(i["type"], i.get("to_status") or i.get("kind")) for i in items] == [
        ("status", "accepted"),
        ("assignment", "claimed"),
        ("assignment", "repicked"),
    ]
    claimed, repicked = items[1], items[2]
    assert claimed["from_professional"] is None
    assert claimed["to_professional"] == {
        "id": str(self_pro.id),
        "display_name": self_pro.display_name,
    }
    assert repicked["from_professional"]["id"] == str(self_pro.id)
    assert repicked["to_professional"] == {"id": str(member.id), "display_name": "Rohan"}
    assert repicked["actor_user_id"] == str(sp.id)
    assert items[0]["from_status"] == "requested"
    assert items[0]["actor_user_id"] == str(sp.id)


@pytest.mark.asyncio
async def test_timeline_puts_a_decline_before_the_reopen_it_causes(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)
    await _earlier(db_session, booking)
    resp = await client.post(DECLINE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.json()["outcome"] == "reopened"

    items = await _admin_timeline(client, auth_headers, db_session, booking)

    assert [(i["type"], i.get("to_status") or i.get("kind")) for i in items] == [
        ("status", "accepted"),
        ("assignment", "claimed"),
        ("assignment", "declined"),
        ("status", "requested"),
    ]


@pytest.mark.asyncio
async def test_timeline_merges_both_tables_by_time(client, db_session, auth_headers, pushes):
    """Rows from different moments sort by ``created_at``, whichever table."""
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service, provider_id=sp.id)
    t0 = datetime.now(UTC) - timedelta(hours=1)
    db_session.add_all(
        [
            BookingStatusLog(
                booking_id=booking.id,
                from_status="accepted",
                to_status="en_route",
                changed_by=member.user_id,
                metadata_={"secret": "never shown"},
                created_at=t0 + timedelta(minutes=3),
            ),
            BookingAssignmentEvent(
                booking_id=booking.id,
                kind="repicked",
                from_professional_id=self_pro.id,
                to_professional_id=member.id,
                actor_user_id=sp.id,
                note="customer asked for Rohan",
                created_at=t0 + timedelta(minutes=2),
            ),
            BookingStatusLog(
                booking_id=booking.id,
                from_status="requested",
                to_status="accepted",
                changed_by=sp.id,
                metadata_={},
                created_at=t0,
            ),
            BookingAssignmentEvent(
                booking_id=booking.id,
                kind="claimed",
                to_professional_id=self_pro.id,
                actor_user_id=sp.id,
                created_at=t0 + timedelta(minutes=1),
            ),
        ]
    )
    await db_session.flush()

    items = await _admin_timeline(client, auth_headers, db_session, booking)

    assert [(i["type"], i.get("to_status") or i.get("kind")) for i in items] == [
        ("status", "accepted"),
        ("assignment", "claimed"),
        ("assignment", "repicked"),
        ("status", "en_route"),
    ]
    assert items[2]["note"] == "customer asked for Rohan"
    member_user = await db_session.get(User, member.user_id)
    assert items[3]["actor_name"] == member_user.name
    assert "metadata" not in items[3] and "never shown" not in str(items)


@pytest.mark.asyncio
async def test_timeline_is_admin_only_and_404s_an_unknown_booking(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    consumer, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)

    for user, role in ((consumer, "consumer"), (sp, "provider")):
        resp = await client.get(TIMELINE.format(booking.id), headers=_as(auth_headers, user, role))
        assert resp.status_code == 403, resp.text

    admin = await UserFactory.create(db_session, role="admin")
    resp = await client.get(
        TIMELINE.format(uuid.uuid4()), headers=auth_headers(str(admin.id), "admin")
    )
    assert resp.status_code == 404


# --- The table ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_check_constraint_rejects_an_unknown_kind(db_session):
    service = await _service(db_session)
    _, booking = await _booking(db_session, service=service)
    with pytest.raises(IntegrityError, match="ck_booking_assignment_events_kind"):
        async with db_session.begin_nested():
            db_session.add(BookingAssignmentEvent(booking_id=booking.id, kind="reassigned"))
            await db_session.flush()
