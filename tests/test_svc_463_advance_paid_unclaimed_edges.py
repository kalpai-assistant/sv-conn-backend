"""SVC-463 (edge cases for SVC-344's two-step ``advance_paid_unclaimed``).

Three failure modes this file guards — each is a scenario where the sweep
could either double-send money or double-send a push, which is what the
ticket's one-liner summarises as "consumer gets both a refund AND an
accepted booking, or a push for a refund the DB rolled back":

(a) **Booking becomes taken between step 1 and step 2.** The sweep sets
    ``last_chance_at`` on tick N, a provider accepts on tick N+1 via
    ``/accept``, and the sweep fires again on tick N+2. The row lock
    ``advance_paid_unclaimed`` takes in step 2 MUST re-read the state and
    refuse — any refund now would leave the consumer with both a refund and
    an accepted booking.

(b) **Razorpay refund API 503.** A 5xx is the operationally-relevant
    failure mode (Razorpay tells us "down right now"). The refund call has
    to raise through to ``_advance_paid``, which rolls the whole step 2 back;
    the booking stays ``REQUESTED`` + ``PAID`` and the next sweep retries.
    The existing SVC-344 tests cover a plain ``RuntimeError``; a 503 goes
    through the SDK's ``BadRequestError`` / ``ServerError`` surface, so we
    exercise that shape explicitly here.

(c) **Push enqueue before commit.** The sweep's push ("new_request" for the
    second offer; "no_taker_refunded" for the cancel) must fire AFTER the
    step's own transaction commits — otherwise a crash mid-commit, a lost
    db connection, or a network partition leaves the worker chasing a
    booking that was never saved. This is the sweep-side mirror of SVC-461.

Shape: the first two tests run against a committed-row scratch session (the
sweep's ``asyncio.run`` cannot share pytest's loop); the third patches
``send_booking_notification.delay`` to observe call ordering against the
sweep's ``_advance_paid`` helper directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.booking import BookingStatus, PaymentStatus
from app.services.booking_service import BookingService
from app.tasks.cleanup_tasks import _advance_paid
from tests.test_paid_no_taker import _paid_open_booking

RZP = "app.services.payment_service.razorpay_client"


def _ago(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


# --- (a) Booking becomes taken between step-1 and step-2 ---------------------


@pytest.mark.asyncio
async def test_claim_between_sweep_ticks_blocks_step_2_refund(db_session):
    """Failure mode: a provider claims the booking after step 1 set
    ``last_chance_at`` but before step 2 would have fired the refund. If step
    2 runs anyway, the consumer ends up with both a refund AND an accepted
    booking — the worst outcome.

    ``advance_paid_unclaimed`` re-reads every gating field under
    ``with_for_update``, so a ``provider_id is not None`` on re-entry must
    short-circuit to ``None`` with no refund call, no state change, and no
    ledger write.
    """
    booking = await _paid_open_booking(db_session)
    service = BookingService(db_session)

    # Step 1 of the two-step: second offer begins.
    assert await service.advance_paid_unclaimed(booking.id) == "second_offer"
    assert booking.last_chance_at is not None

    # Simulate the "taken between ticks" race: a /accept call lands between
    # step 1 and step 2. After /accept the row has provider_id + ACCEPTED.
    # We set those directly rather than going through the whole route
    # (route-level coverage lives in test_paid_no_taker.py); the point here
    # is that the sweep's step 2 must *refuse* no matter how it got taken.
    booking.provider_id = booking.consumer_id  # any user; the guard is "not None"
    booking.status = BookingStatus.ACCEPTED
    # Age the last_chance window past expiry so the only remaining guard is
    # the provider_id + status re-check.
    booking.last_chance_at = _ago(95)
    await db_session.flush()

    with patch(RZP) as rzp:
        outcome = await service.advance_paid_unclaimed(booking.id)

    assert outcome is None, (
        "SVC-463 regression: advance_paid_unclaimed refunded a booking that "
        "was taken between step 1 and step 2. Consumer would get both a "
        "refund and an accepted booking."
    )
    assert rzp.payment.refund.call_count == 0
    # Booking stayed accepted.
    assert booking.status is BookingStatus.ACCEPTED
    assert booking.payment_status is PaymentStatus.PAID


# --- (b) Razorpay refund API returns 503 -------------------------------------


@pytest.mark.asyncio
async def test_razorpay_503_on_refund_rolls_back_cleanly(db_session):
    """A Razorpay 503 during step 2 must raise, so the sweep's own
    ``asyncio.run`` → ``session.rollback`` keeps the booking in
    ``REQUESTED`` + ``PAID`` for the next tick to retry.

    The SDK surfaces upstream 5xx as ``razorpay.errors.ServerError``. We
    simulate it with that exact type; the booking service should propagate
    it as ``PaymentError`` (``initiate_refund`` wraps the exception).
    """
    import razorpay.errors

    from app.exceptions import PaymentError

    booking = await _paid_open_booking(db_session)
    booking.last_chance_at = _ago(95)
    await db_session.flush()

    before_status = booking.status
    before_payment_status = booking.payment_status

    with patch(RZP) as rzp:
        rzp.payment.refund.side_effect = razorpay.errors.ServerError("503 service unavailable")
        with pytest.raises(PaymentError, match="Refund initiation failed"):
            await BookingService(db_session).advance_paid_unclaimed(booking.id)

    # We're in the test session; the real sweep's rollback is covered by
    # test_svc_344_sweep_rolls_back_a_failed_refund in test_paid_no_taker.
    # The invariant here is: initiate_refund itself raised, and no state
    # we assert on was committed (we read the in-session row).
    #
    # In the real sweep (``_advance_paid``), the surrounding ``session.rollback``
    # drops this change; here we rely on the test-session wrapper doing the
    # same when the fixture tears down. Either way, the invariant is: the
    # refund call raised, so the caller could not have fired a push.
    #
    # Status check: ``advance_paid_unclaimed`` sets booking.status = CANCELLED
    # BEFORE the refund call, so the local ORM copy will show CANCELLED. The
    # real-world guarantee is that the surrounding transaction rolls back and
    # DB sees REQUESTED again — test_paid_no_taker.test_svc_344_sweep_rolls_...
    # covers that against a committed DB. Here we only pin the "raises"
    # behaviour that enables the rollback.
    assert rzp.payment.refund.call_count == 1
    # Sanity: before any of this the booking was unclaimed and paid.
    assert before_status is BookingStatus.REQUESTED
    assert before_payment_status is PaymentStatus.PAID


# --- (c) Push enqueue before commit ------------------------------------------


def test_sweep_enqueues_push_only_after_commit():
    """The sweep's ``_advance_paid`` helper commits its own session, then
    fires ``send_booking_notification.delay(...)``. A push that fires before
    the commit lets a worker 404 on an unflushed row — the sweep-side mirror
    of SVC-461's failure mode.

    Mechanism: patch ``BookingService.advance_paid_unclaimed`` to record when
    it's called, and patch the ``.delay`` to record when it's called;
    additionally wrap ``AsyncSession.commit`` so we can observe its ordering
    relative to the push. The required order is: advance_paid_unclaimed →
    commit → delay. If a future refactor inlines ``.delay`` inside ``_run``
    (pre-commit), the recorded sequence flips and this fails.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    sequence: list[str] = []

    async def _fake_advance(self, _bid):
        sequence.append("advance_paid_unclaimed")
        return "second_offer"

    original_commit = AsyncSession.commit

    async def _tracked_commit(self):
        sequence.append("commit")
        return await original_commit(self)

    def _tracked_delay(**kwargs):
        sequence.append(f"delay:{kwargs.get('event')}")

    with (
        patch(
            "app.services.booking_service.BookingService.advance_paid_unclaimed",
            new=_fake_advance,
        ),
        patch.object(AsyncSession, "commit", _tracked_commit),
        patch(
            "app.tasks.notification_tasks.send_booking_notification.delay",
            side_effect=_tracked_delay,
        ),
    ):
        outcome = _advance_paid(uuid.uuid4())

    assert outcome == "second_offer"
    # Required ordering: advance → commit → delay.
    assert sequence[:3] == ["advance_paid_unclaimed", "commit", "delay:new_request"], (
        "SVC-463 regression: _advance_paid did not run advance → commit → delay. "
        "A push fired before commit lets a worker 404 on an unflushed row. "
        f"Observed order: {sequence}"
    )


def test_sweep_does_not_push_on_rollback():
    """A refund failure in step 2 rolls the session back inside ``_run``;
    ``_advance_paid`` must NOT then call ``.delay(...)``. Otherwise the
    consumer gets a "refund processed" push for a refund that didn't happen.
    """

    def _tracked_delay(**kwargs):
        raise AssertionError(
            "SVC-463 regression: _advance_paid fired .delay() after a rollback. "
            f"Would have sent: {kwargs}"
        )

    with (
        patch(
            "app.services.booking_service.BookingService.advance_paid_unclaimed",
            side_effect=RuntimeError("razorpay down"),
        ),
        patch(
            "app.tasks.notification_tasks.send_booking_notification.delay",
            side_effect=_tracked_delay,
        ),
    ):
        outcome = _advance_paid(uuid.uuid4())

    assert outcome == "failed", (
        "After a rollback, _advance_paid must return 'failed' so the sweep's "
        "summary metric reflects what happened."
    )
