"""SVC-461: Celery enqueues must fire inside SQLAlchemy's ``after_commit``.

Failure mode this guards: a FastAPI route calls ``BookingService`` → the
service flushes (but does not commit) → the route then calls
``send_booking_notification.delay(booking_id=...)``. Celery enqueues to the
broker immediately. A worker on another process can pick up the task and SELECT
the booking before FastAPI's ``get_db`` dependency commits its transaction, so
the worker 404s on a row that doesn't exist yet and the push is dropped.

The fix (same ticket) wraps every ``delay(...)`` after a ``BookingService``
call in ``call_after_commit`` so the enqueue only happens from inside the
SQLAlchemy ``after_commit`` event. This test pins that invariant by:

1. Patching ``send_booking_notification`` / ``process_cash_payment`` so each
   ``.delay(...)`` records the ``in_transaction()`` state of the test's
   session at the moment of the call.
2. POSTing / PATCHing against the live app with those patches in place.
3. Asserting the recorded state is ``in_transaction() is False`` — i.e. the
   enqueue happened AFTER the session committed, not before.

If a future refactor drops the ``call_after_commit`` wrapper, the recorded
state is ``True`` (we're still mid-transaction) and the test red-flags the
regression.

The ticket's "fakeredis + async session that commits mid-call" shape is what
the fixture graph adds up to — the ``client`` fixture uses the real test
Postgres, Redis is fine as-is, and the ``.delay`` patch is the "bytes never
leave this process" seam. The assertion is the same: the enqueue is observed
from inside after_commit, not before it.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


def _record_delay_state(db_session: AsyncSession):
    """Build a ``.delay(...)`` recorder that notes whether ``db_session`` is
    still in an open transaction at the moment of the call.

    Returns ``(recorder_fn, recorded_list)``. Each entry is
    ``{"event": str | None, "in_transaction": bool, "kwargs": dict}``.
    """
    recorded: list[dict] = []
    sync_session: Session = db_session.sync_session

    def _delay(**kwargs):
        recorded.append(
            {
                "event": kwargs.get("event"),
                "in_transaction": sync_session.in_transaction(),
                "kwargs": kwargs,
            }
        )

    return _delay, recorded


async def _seed_bookable(db_session: AsyncSession):
    """Minimal graph for creating a booking: consumer + provider + a service
    they offer + an address."""
    consumer = await UserFactory.create(db_session, role="consumer")
    await UserFactory.create(db_session, role="provider")

    category = Category(name="Plumbing", slug="plumbing", is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()

    service = Service(
        category_id=category.id,
        name="Basic Pipe Fix",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)

    address = Address(user_id=consumer.id, label="home", full_address="123 Main St")
    db_session.add(address)
    await db_session.commit()
    return consumer, service, address


async def test_create_booking_enqueues_push_after_commit_not_before(
    client, auth_headers, db_session
):
    """POST /bookings/ must not ``.delay(...)`` until the booking's transaction
    commits. A worker on another process reads by ``booking_id``; enqueuing
    pre-commit races the worker to a 404 and the push is lost.
    """
    consumer, service, address = await _seed_bookable(db_session)

    delay_fn, recorded = _record_delay_state(db_session)

    # The Celery task is imported at module scope by the router, so patch the
    # NAME it uses, not the task object.
    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = delay_fn
        resp = await client.post(
            "/api/v1/bookings/",
            headers=auth_headers(str(consumer.id), "consumer"),
            json={
                "service_id": str(service.id),
                "address_id": str(address.id),
                "payment_method": "online",
            },
        )
    assert resp.status_code == 201, resp.text
    assert len(recorded) == 1, f"Expected exactly one push enqueue, got {recorded}"
    entry = recorded[0]
    assert entry["event"] == "new_request"
    assert entry["in_transaction"] is False, (
        "SVC-461 regression: send_booking_notification.delay() fired while the "
        "request's AsyncSession was still in an open transaction. A Celery "
        "worker can pick the task up before FastAPI's get_db commits — the "
        "worker's SELECT then returns no row and the push is dropped. "
        "Wrap the enqueue in app.database.call_after_commit."
    )


async def test_cancel_booking_enqueues_push_after_commit_not_before(
    client, auth_headers, db_session
):
    """PATCH /bookings/{id}/cancel — the ``cancelled`` push must also fire
    after commit, not before. Same race, same failure mode.
    """
    consumer, service, address = await _seed_bookable(db_session)
    provider = await UserFactory.create(db_session, role="provider")
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.REQUESTED,
    )
    await db_session.commit()

    delay_fn, recorded = _record_delay_state(db_session)

    with patch("app.routers.bookings.send_booking_notification") as mock_task:
        mock_task.delay = delay_fn
        resp = await client.patch(
            f"/api/v1/bookings/{booking.id}/cancel",
            headers=auth_headers(str(consumer.id), "consumer"),
            json={"reason": "changed my mind"},
        )
    assert resp.status_code == 200, resp.text
    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["event"] == "cancelled"
    assert entry["in_transaction"] is False, (
        "SVC-461 regression: /cancel's send_booking_notification.delay() fired "
        "mid-transaction. Wrap it in call_after_commit."
    )


# ---------------------------------------------------------------------------
# call_after_commit mechanism tests — the invariant everything else relies on
# ---------------------------------------------------------------------------


async def test_call_after_commit_runs_callback_exactly_once_post_commit(db_session):
    """``call_after_commit`` must only run its callback from inside
    SQLAlchemy's ``after_commit`` event: never synchronously, and the
    callback must observe the session already out of its transaction.
    """
    from app.database import call_after_commit

    fired: list[bool] = []
    sync_session: Session = db_session.sync_session

    def _cb() -> None:
        # True = session was no longer in a transaction when the callback ran.
        fired.append(not sync_session.in_transaction())

    call_after_commit(db_session, _cb)
    assert fired == [], "pre-commit: callback must not have fired yet"

    await db_session.commit()
    assert fired == [True], (
        "post-commit: callback must have fired exactly once and seen the "
        f"session already out of its transaction. Got: {fired}"
    )


async def test_rollback_drops_the_after_commit_callback(db_session):
    """If the transaction rolls back, the enqueue must NOT fire. Otherwise a
    request that failed after queuing a push would still send one.
    """
    from app.database import call_after_commit

    fired: list[bool] = []

    call_after_commit(db_session, lambda: fired.append(True))
    await db_session.rollback()
    assert fired == [], (
        "SVC-461: a rolled-back transaction must drop its after_commit callbacks. "
        "Otherwise a request that failed mid-flight could still enqueue pushes "
        "for changes that never happened."
    )
