"""
SVC-348: the assigned team Pro is told when a job is taken off them.

Before, a cancel told only "whoever didn't cancel" (the consumer or the SP), and
a give-back or re-pick told nobody but the consumer or the new Pro. The Pro on
the job, who may already be on their way, heard nothing.

Two halves, pinned separately:

- **The task** (real ``send_booking_notification`` on committed rows, FCM
  mocked): who gets ``job_cancelled`` / ``unassigned``, in which words, and that
  the Pro-for-self never gets a second push on top of the SP's.
- **The enqueue** (real routers, ``.delay`` patched): ``record_assignment``
  queues ``unassigned`` for the Pro a re-pick, release or give-back took the job
  off, and only once the transaction commits.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import _run_after_commit, call_after_commit
from app.models.booking import BookingStatus
from app.models.user import User
from app.tasks import notification_tasks
from app.tasks.notification_tasks import NOTIFICATION_TEMPLATES, send_booking_notification
from tests.conftest import TEST_DATABASE_URL
from tests.test_accept_contract import _booking, _service
from tests.test_push_copy_pro_brand import _job
from tests.test_two_level_dispatch import (
    ASSIGN,
    DECLINE,
    RELEASE,
    _accepted_by,
    _as,
    _assigned_to,
    _team_pro,
    _team_sp,
)

CANCEL = "/api/v1/bookings/{}/cancel"
CANCELLED = NOTIFICATION_TEMPLATES["cancelled"]
JOB_CANCELLED = NOTIFICATION_TEMPLATES["job_cancelled"]
UNASSIGNED = NOTIFICATION_TEMPLATES["unassigned"]


@pytest.fixture
async def committed(db_session):
    """Commit test rows (the task has its own connection), then remove them."""
    created: list = []
    yield created
    for obj in reversed(created):
        await db_session.delete(obj)
    await db_session.commit()


@pytest.fixture
def pushes():
    """Every ``send_booking_notification.delay`` call, as ``(event, kwargs)``."""
    sent: list[tuple[str, dict]] = []
    with patch.object(
        notification_tasks.send_booking_notification,
        "delay",
        side_effect=lambda **kw: sent.append((kw["event"], kw)),
    ):
        yield sent


def _sent(booking_id, event, actor_id=None, professional_id=None):
    """Run the real task; capture ``(token, title, data event)`` for every push."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_url = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url)
    sent = []
    with (
        patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)),
        patch("app.tasks.notification_tasks.send_push_notification") as mock_push,
    ):
        mock_push.side_effect = lambda token, title, data, **_: sent.append(
            (token, title, data["event"])
        )
        send_booking_notification(
            booking_id=str(booking_id),
            event=event,
            actor_id=actor_id,
            professional_id=professional_id,
        )
    engine.dispose()
    return sent


# --- The task: cancels ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_cancel_tells_the_sp_and_the_team_pro_once_each(db_session, committed):
    """The ticket: the SP gets its push as before, and the Pro gets one too."""
    booking, _, _ = await _job(db_session, committed, brand="Sparkle", team=True)

    assert _sent(booking.id, "cancelled", actor_id=str(booking.consumer_id)) == [
        ("sp-token", CANCELLED["title"], "cancelled"),
        ("pro-token", JOB_CANCELLED["title"], "job_cancelled"),
    ]


@pytest.mark.asyncio
async def test_customer_cancel_on_a_solo_job_is_one_push(db_session, committed):
    """The Pro-for-self is the SP's own user: no second push on top of the SP's."""
    booking, _, _ = await _job(db_session, committed, brand="Sparkle", team=False)

    assert _sent(booking.id, "cancelled", actor_id=str(booking.consumer_id)) == [
        ("sp-token", CANCELLED["title"], "cancelled"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS])
async def test_customer_cancel_later_still_reaches_the_pro(db_session, committed, status):
    """The case that matters most: the Pro may already be on the way."""
    booking, _, _ = await _job(db_session, committed, brand="Sparkle", team=True, status=status)

    assert ("pro-token", JOB_CANCELLED["title"], "job_cancelled") in _sent(
        booking.id, "cancelled", actor_id=str(booking.consumer_id)
    )


@pytest.mark.asyncio
async def test_admin_cancel_tells_the_consumer_the_sp_and_the_team_pro(db_session, committed):
    """The admin is neither party, so both parties hear it (the second bug)."""
    booking, _, _ = await _job(db_session, committed, brand="Sparkle", team=True)

    assert _sent(booking.id, "cancelled", actor_id=str(uuid.uuid4())) == [
        ("consumer-token", CANCELLED["title"], "cancelled"),
        ("sp-token", CANCELLED["title"], "cancelled"),
        ("pro-token", JOB_CANCELLED["title"], "job_cancelled"),
    ]


@pytest.mark.asyncio
async def test_admin_cancel_on_a_solo_job_does_not_double_send(db_session, committed):
    booking, _, _ = await _job(db_session, committed, brand="Sparkle", team=False)

    assert _sent(booking.id, "cancelled", actor_id=str(uuid.uuid4())) == [
        ("consumer-token", CANCELLED["title"], "cancelled"),
        ("sp-token", CANCELLED["title"], "cancelled"),
    ]


@pytest.mark.asyncio
async def test_cancel_with_no_pro_sends_no_pro_push(db_session, committed):
    booking, _, _ = await _job(db_session, committed, brand="Sparkle", team=True)
    booking.professional_id = None
    await db_session.commit()

    assert _sent(booking.id, "cancelled", actor_id=str(booking.consumer_id)) == [
        ("sp-token", CANCELLED["title"], "cancelled"),
    ]


@pytest.mark.asyncio
async def test_sp_cancel_push_to_the_consumer_is_unchanged(db_session, committed):
    """Consumer/provider routing is as before; only the Pro push is added."""
    booking, sp, _ = await _job(db_session, committed, brand="Sparkle", team=False)

    assert _sent(booking.id, "cancelled", actor_id=str(sp.id)) == [
        ("consumer-token", CANCELLED["title"], "cancelled"),
    ]


# --- The task: unassigned ------------------------------------------------------------


@pytest.mark.asyncio
async def test_unassigned_goes_to_the_pro_it_names_not_the_current_one(db_session, committed):
    """By the time the task runs the booking has moved on: it names the old Pro."""
    booking, _, member = await _job(db_session, committed, brand="Sparkle", team=True)
    old_pro_id = booking.professional_id
    booking.professional_id = None
    await db_session.commit()

    assert _sent(booking.id, "unassigned", professional_id=str(old_pro_id)) == [
        ("pro-token", UNASSIGNED["title"], "unassigned"),
    ]


@pytest.mark.asyncio
async def test_unassigned_without_a_named_pro_sends_nothing(db_session, committed, caplog):
    """Never falls back to the booking's Pro: after a re-pick that's the new one."""
    booking, _, _ = await _job(db_session, committed, brand="Sparkle", team=True)

    assert _sent(booking.id, "unassigned") == []
    assert "push.unassigned_without_pro" in caplog.text


@pytest.mark.asyncio
async def test_unassigned_never_goes_to_the_pro_for_self(db_session, committed):
    booking, _, _ = await _job(db_session, committed, brand="Sparkle", team=False)

    assert _sent(booking.id, "unassigned", professional_id=str(booking.professional_id)) == []


def test_the_pro_copy_says_do_not_travel_and_fits():
    for template in (JOB_CANCELLED, UNASSIGNED):
        assert "don't travel" in template["body"]
        assert len(template["body"]) <= notification_tasks.PUSH_BODY_MAX_CHARS


# --- The enqueue: record_assignment, after commit ------------------------------------


def _commit_hooks(db: AsyncSession) -> None:
    """Run what would run on commit. ``get_db``'s commit never runs under test
    (the override is a plain session), so this stands in for it."""
    _run_after_commit(db.sync_session)


def _unassigned(pushes) -> list[str]:
    return [kw["professional_id"] for event, kw in pushes if event == "unassigned"]


@pytest.mark.asyncio
async def test_repick_tells_the_old_team_pro(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, first = await _team_sp(db_session, service)
    second = await _team_pro(db_session, sp.id, name="Amit")
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, first)
    _commit_hooks(db_session)
    assert _unassigned(pushes) == []  # a claim takes the job off nobody

    await _assigned_to(client, auth_headers, db_session, booking, sp, second)
    _commit_hooks(db_session)

    assert _unassigned(pushes) == [str(first.id)]
    assert [kw["booking_id"] for e, kw in pushes if e == "unassigned"] == [str(booking.id)]


@pytest.mark.asyncio
async def test_repick_off_the_pro_for_self_tells_nobody(client, db_session, auth_headers, pushes):
    """The SP moved its own job to a team member: it knows."""
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)

    await _assigned_to(client, auth_headers, db_session, booking, sp, member)
    _commit_hooks(db_session)

    assert _unassigned(pushes) == []


@pytest.mark.asyncio
async def test_release_tells_the_team_pro(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    resp = await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    _commit_hooks(db_session)

    assert _unassigned(pushes) == [str(member.id)]


@pytest.mark.asyncio
async def test_release_of_a_solo_job_tells_no_pro(client, db_session, auth_headers, pushes):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _accepted_by(client, auth_headers, db_session, booking, sp)

    resp = await client.post(RELEASE.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    _commit_hooks(db_session)

    assert _unassigned(pushes) == []


@pytest.mark.asyncio
async def test_provider_give_back_tells_the_team_pro(client, db_session, auth_headers, pushes):
    """PATCH /cancel by the SP while ACCEPTED reopens; the Pro loses the job."""
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    resp = await client.patch(
        CANCEL.format(booking.id), json={"reason": "can't make it"}, headers=_as(auth_headers, sp)
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED
    _commit_hooks(db_session)

    assert _unassigned(pushes) == [str(member.id)]
    assert "cancelled" not in [event for event, _ in pushes]


@pytest.mark.asyncio
async def test_a_pro_who_declines_is_not_told_they_declined(
    client, db_session, auth_headers, pushes
):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    await _assigned_to(client, auth_headers, db_session, booking, sp, member)

    member_user = await db_session.get(User, member.user_id)
    resp = await client.post(
        DECLINE.format(booking.id), headers=_as(auth_headers, member_user, role="consumer")
    )
    assert resp.status_code == 200, resp.text
    _commit_hooks(db_session)

    assert _unassigned(pushes) == []


@pytest.mark.asyncio
async def test_a_rolled_back_change_sends_nothing():
    """The hook itself, on a real session: commit runs it once, rollback drops it."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(TEST_DATABASE_URL)
    calls: list[str] = []
    try:
        async with async_sessionmaker(engine)() as db:
            await db.execute(text("SELECT 1"))
            call_after_commit(db, lambda: calls.append("rolled back"))
            await db.rollback()
            await db.execute(text("SELECT 1"))
            await db.commit()  # a later commit must not run the dropped one

            await db.execute(text("SELECT 1"))
            call_after_commit(db, lambda: calls.append("committed"))
            await db.commit()
            await db.execute(text("SELECT 1"))
            await db.commit()  # and runs it only once

            await db.execute(text("SELECT 1"))
            call_after_commit(db, lambda: calls.append("closed"))
        # Closed without a commit: dropped.
    finally:
        await engine.dispose()

    assert calls == ["committed"]
