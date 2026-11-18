"""SVC-461: Celery enqueue must fire inside SQLAlchemy's ``after_commit``.

Failure mode this guards: a FastAPI route calls ``BookingService`` → the
service flushes (but does not commit) → the route then calls
``send_booking_notification.delay(booking_id=...)``. Celery enqueues to
the Redis broker immediately. A worker on another process can pick up the
task and SELECT the booking BEFORE FastAPI's ``get_db`` dependency commits
its transaction — the worker then 404s on a row that doesn't exist yet and
the push is dropped.

Status (2026-11-14): the regression is **live**. Every ``.delay(...)``
inside ``app/routers/bookings.py`` and ``app/routers/pros.py`` runs after
the service's ``flush`` but before FastAPI's ``get_db`` has had a chance
to commit. The parent-level recommendation is to move these enqueues into
``call_after_commit`` (which already exists in ``app/database.py``) — the
test agent's exploratory fix wrapped ~8 call sites and found that doing so
in-wave broke ~17 existing tests whose patches rely on the pre-commit
timing and whose ``client`` fixture (``tests/conftest.py``) overrides
``get_db`` to a no-commit lambda, so the ``after_commit`` event never
fires in tests.

Resolving that cascade is a bigger refactor than a stability-wave
regression lock should carry (see parent-level recommendation in the
BACKEND_REPORT of STABILITY_AUDIT.md). This file therefore ships the
route-level assertion as ``xfail(strict=False)``: it documents the live
bug, lights up red in reports, and flips to ``XPASS`` the moment someone
wires the fix in.

The rock-solid non-xfail tests here pin the ``call_after_commit``
mechanism itself — if that helper ever regressed, every production use
(admin_support_service, admin_dispute_service, booking_service._apply_
cancellation_policy, payout_service) would silently drop its side effect.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.address import Address
from app.models.category import Category
from app.models.service import Service
from tests.factories.user_factory import UserFactory


def _record_delay_state(db_session: AsyncSession):
    """Record each ``.delay(...)`` call with a snapshot of whether the
    session had already fired its ``after_commit`` event by the moment
    the enqueue ran.

    Returns ``(recorder_fn, recorded_list)``. Each entry is
    ``{"event": str, "committed_before_delay": bool, "kwargs": dict}``.
    """
    recorded: list[dict] = []
    sync_session: Session = db_session.sync_session

    committed_at_least_once = [False]

    @event.listens_for(sync_session, "after_commit")
    def _marker(_session):
        committed_at_least_once[0] = True

    def _delay(**kwargs):
        recorded.append(
            {
                "event": kwargs.get("event"),
                "committed_before_delay": committed_at_least_once[0],
                "kwargs": kwargs,
            }
        )

    return _delay, recorded


@pytest.mark.xfail(
    strict=False,
    reason=(
        "SVC-461 live regression: send_booking_notification.delay() fires "
        "before the session commits. Parent-level fix recommended: wrap each "
        "enqueue in call_after_commit across app/routers/bookings.py and "
        "app/routers/pros.py. In-wave fix breaks ~17 pre-existing tests "
        "whose pushes fixture asserts synchronous delay — needs a separate "
        "test-infra slice before the production fix can land cleanly."
    ),
)
async def test_create_booking_enqueues_push_after_commit_not_before(
    client, auth_headers, db_session
):
    """POST /bookings/ must not ``.delay(...)`` until the booking's
    transaction commits. A worker on another process reads by
    ``booking_id``; enqueuing pre-commit races the worker to a 404 and the
    push is lost.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    await UserFactory.create(db_session, role="provider")

    category = Category(
        name="SVC-461-plumbing",
        slug="svc-461-plumbing",
        is_active=True,
        sort_order=1,
    )
    db_session.add(category)
    await db_session.flush()
    service = Service(
        category_id=category.id,
        name="SVC-461 Basic",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    address = Address(user_id=consumer.id, label="home", full_address="SVC-461 St")
    db_session.add_all([service, address])
    await db_session.flush()

    delay_fn, recorded = _record_delay_state(db_session)

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
    # Manually commit so the after_commit marker fires. In production,
    # FastAPI's get_db dependency does this on request teardown; the
    # test's get_db override is a no-commit passthrough.
    await db_session.commit()

    assert resp.status_code == 201, resp.text
    assert len(recorded) == 1, f"Expected exactly one push enqueue, got {recorded}"
    entry = recorded[0]
    assert entry["event"] == "new_request"
    assert entry["committed_before_delay"] is True, (
        "SVC-461: send_booking_notification.delay() fired BEFORE the "
        "session's after_commit event. A Celery worker picking it up races "
        "to a 404 on an unflushed booking and the push is dropped. Wrap "
        "the enqueue in call_after_commit."
    )


# ---------------------------------------------------------------------------
# call_after_commit mechanism — pins the helper itself. If this regresses,
# every production site that already uses it drops its side effect silently.
# ---------------------------------------------------------------------------


async def test_call_after_commit_fires_callback_on_commit(db_session):
    """``call_after_commit`` must enqueue ``fn`` to run during the session's
    ``after_commit`` event: never synchronously, exactly once per commit.
    """
    from app.database import call_after_commit

    fired: list[int] = []

    call_after_commit(db_session, lambda: fired.append(1))
    assert fired == [], "pre-commit: must not have fired"

    await db_session.commit()
    assert fired == [1], "after commit: must have fired exactly once"


async def test_rollback_drops_the_after_commit_callback(db_session):
    """A rolled-back transaction must drop queued callbacks. Otherwise a
    request that failed mid-flight could still enqueue a push for a change
    the DB never saw.
    """
    from app.database import call_after_commit

    fired: list[int] = []

    call_after_commit(db_session, lambda: fired.append(1))
    await db_session.rollback()
    assert fired == [], (
        "SVC-461: a rolled-back transaction must drop its after_commit "
        "callbacks. Otherwise a request that failed mid-flight could still "
        "enqueue pushes for changes that never happened."
    )
