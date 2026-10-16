"""
A failed push is retried on its own, not by re-running the broadcast; and the
2-minute sweep writes a status row for every booking it expires.

Before: any push error that wasn't a dead token escaped the send loop, and
Celery retried the whole ``send_booking_notification``. Everyone already
pushed got the job again, and (until SVC-373) a retry landing after an accept
expired the accepted booking. Now the loop hands that one recipient to
``resend_booking_push``, which re-checks the job is still open before pushing.
"""

import logging
from unittest.mock import patch

import pytest
from firebase_admin import messaging as fcm_messaging
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

from app.models.booking import Booking, BookingStatus
from app.models.booking_offer import BookingOffer, OfferResponse
from app.models.booking_status_log import BookingStatusLog
from app.tasks.notification_tasks import resend_booking_push, send_booking_notification
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_fcm_dead_token_clearing import (
    _address,
    _current_token,
    _eligible_provider,
    _run_task,
    _service,
)
from tests.test_paid_no_taker import _seed, _sweep, committed_sweep_world  # noqa: F401
from tests.test_unbroadcastable_booking import committed  # noqa: F401 (fixture)

RESEND = "app.tasks.notification_tasks.resend_booking_push.apply_async"


async def _open_job(db, committed, tokens):  # noqa: F811
    service = await _service(db)
    providers = [await _eligible_provider(db, service, token=t) for t in tokens]
    consumer = await UserFactory.create(db, role="consumer", fcm_token=None)
    addr = await _address(db, consumer.id)
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=None,
        service_id=service.id,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )
    committed.extend([booking, addr, consumer, *providers])
    await db.commit()
    return booking, providers


def _resend(booking, user, fcm_side_effect=None, **task):
    kwargs = dict(
        booking_id=str(booking.id),
        user_id=str(user.id),
        event="new_request",
        title="New job",
        body="AC installation — accept within 2 minutes.",
    )
    kwargs.update(task)
    if fcm_side_effect is not None:
        kwargs["fcm_side_effect"] = fcm_side_effect
    return _run_task(resend_booking_push, **kwargs)


def _sql(statement, **params):
    engine = create_engine(TEST_DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    try:
        with engine.begin() as conn:
            conn.execute(statement, params)
    finally:
        engine.dispose()


# --- The broadcast no longer retries itself on a push error ---------------------------


@pytest.mark.asyncio
async def test_one_failed_push_is_retried_alone_and_nobody_is_pushed_twice(
    db_session,
    committed,  # noqa: F811
):
    booking, (a, b, c) = await _open_job(db_session, committed, ["tok-a", "tok-b", "tok-c"])

    def _flaky_b(token, **_):
        if token == "tok-b":
            raise fcm_messaging.QuotaExceededError("slow down")

    with patch(RESEND) as resend:
        calls = _run_task(
            send_booking_notification,
            booking_id=str(booking.id),
            event="new_request",
            fcm_side_effect=_flaky_b,
        )

    # No task retry: the call returned, each token was tried exactly once.
    assert sorted(calls) == ["tok-a", "tok-b", "tok-c"]
    [call] = resend.call_args_list
    assert call.kwargs["kwargs"]["user_id"] == str(b.id)
    assert call.kwargs["kwargs"]["event"] == "new_request"
    assert call.kwargs["countdown"] == 5
    # The token itself is not carried in the message.
    assert "tok-b" not in str(call)


# --- The resend re-checks the job before pushing -------------------------------------


@pytest.mark.asyncio
async def test_resend_pushes_an_open_job_with_the_current_token(db_session, committed):  # noqa: F811
    booking, (sp,) = await _open_job(db_session, committed, ["old-token"])
    _run_task(send_booking_notification, booking_id=str(booking.id), event="new_request")
    # The app re-registered between the failure and the resend.
    _sql(text("UPDATE users SET fcm_token = 'new-token' WHERE id = :id"), id=sp.id)

    assert _resend(booking, sp) == ["new-token"]


@pytest.mark.asyncio
async def test_resend_never_pushes_a_claimed_job(db_session, committed):  # noqa: F811
    booking, (sp,) = await _open_job(db_session, committed, ["tok"])
    _run_task(send_booking_notification, booking_id=str(booking.id), event="new_request")
    _sql(
        text("UPDATE bookings SET status = 'ACCEPTED' WHERE id = :id"),
        id=booking.id,
    )

    assert _resend(booking, sp) == []


@pytest.mark.asyncio
async def test_resend_skips_an_sp_who_already_answered(db_session, committed):  # noqa: F811
    booking, (sp,) = await _open_job(db_session, committed, ["tok"])
    _run_task(send_booking_notification, booking_id=str(booking.id), event="new_request")
    await db_session.execute(
        update(BookingOffer)
        .where(BookingOffer.booking_id == booking.id, BookingOffer.provider_id == sp.id)
        .values(response=OfferResponse.DECLINED)
    )
    await db_session.commit()

    assert _resend(booking, sp) == []


@pytest.mark.asyncio
async def test_resend_releases_the_booking_lock_before_pushing(db_session, committed):  # noqa: F811
    """accept and assign take FOR UPDATE on the booking; they must never wait
    on FCM. Inside the push, another connection must be able to lock it."""
    booking, (sp,) = await _open_job(db_session, committed, ["tok"])
    _run_task(send_booking_notification, booking_id=str(booking.id), event="new_request")
    engine = create_engine(TEST_DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    locked: list[bool] = []

    def _try_lock(token, **_):
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            conn.execute(
                select(Booking.id).where(Booking.id == booking.id).with_for_update(nowait=True)
            )
            locked.append(True)

    try:
        _resend(booking, sp, fcm_side_effect=_try_lock)
    finally:
        engine.dispose()
    assert locked == [True]


@pytest.mark.asyncio
async def test_resend_clears_a_dead_token(db_session, committed):  # noqa: F811
    booking, (sp,) = await _open_job(db_session, committed, ["dead"])
    _run_task(send_booking_notification, booking_id=str(booking.id), event="new_request")

    def _dead(token, **_):
        raise fcm_messaging.UnregisteredError("gone")

    _resend(booking, sp, fcm_side_effect=_dead)
    assert await _current_token(db_session, sp.id) is None


@pytest.mark.asyncio
async def test_resend_retries_a_transient_error_then_gives_up(db_session, committed, caplog):  # noqa: F811
    booking, (sp,) = await _open_job(db_session, committed, ["tok"])
    _run_task(send_booking_notification, booking_id=str(booking.id), event="new_request")

    def _throttle(token, **_):
        raise fcm_messaging.QuotaExceededError("slow down")

    # Called directly, Celery's retry re-raises: this is the "try again" path.
    with pytest.raises(fcm_messaging.QuotaExceededError):
        _resend(booking, sp, fcm_side_effect=_throttle)

    # Out of retries: log and stop, with the event name for CloudWatch.
    engine = create_engine(TEST_DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    try:
        with (
            patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)),
            patch("app.tasks.notification_tasks.send_push_notification", side_effect=_throttle),
            caplog.at_level(logging.WARNING, logger="app.tasks.notification_tasks"),
        ):
            resend_booking_push.apply(
                kwargs=dict(
                    booking_id=str(booking.id),
                    user_id=str(sp.id),
                    event="new_request",
                    title="t",
                    body="b",
                ),
                retries=3,
            )
    finally:
        engine.dispose()
    assert f"push.gave_up booking_id={booking.id}" in caplog.text
    assert "event=new_request" in caplog.text
    assert await _current_token(db_session, sp.id) == "tok"


# --- The 2-minute sweep writes a status row ------------------------------------------


def test_the_sweep_logs_every_booking_it_expires(committed_sweep_world):  # noqa: F811
    sync_factory, made = committed_sweep_world
    stale = [_seed(sync_factory, made, paid=False) for _ in range(3)]
    fresh = _seed(sync_factory, made, paid=False, age=10)

    _sweep()

    with sync_factory() as db:
        rows = (
            db.execute(
                select(BookingStatusLog).where(BookingStatusLog.booking_id.in_([*stale, fresh]))
            )
            .scalars()
            .all()
        )
        statuses = {
            b.id: b.status
            for b in db.execute(select(Booking).where(Booking.id.in_(stale))).scalars()
        }
    assert set(statuses.values()) == {BookingStatus.EXPIRED}
    assert sorted(r.booking_id for r in rows) == sorted(stale)
    for r in rows:
        assert (r.from_status, r.to_status, r.changed_by) == ("requested", "expired", None)
        assert r.metadata_ == {"reason": "offer_window_elapsed"}
