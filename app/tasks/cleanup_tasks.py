"""
Cleanup Celery tasks — periodic jobs run by Celery Beat.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update

from app.config import get_settings
from app.models.booking import Booking, BookingStatus, PaymentMethod, PaymentStatus
from app.tasks.celery_app import celery_app

settings = get_settings()
logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.cleanup_tasks.expire_stale_bookings")
def expire_stale_bookings():
    """
    Celery Beat task: runs every 30 seconds. Closes out REQUESTED bookings whose
    offer has expired (``Booking.offer_expires_at``).

    * **Unpaid** (``payment_status == PENDING``, cash included): bulk-expired,
      as always. Nothing to refund.
    * **PAID** (SVC-344; skipped entirely before, SVC-251): never expired,
      since expiry doesn't refund. Instead each one moves one step, in its own
      transaction: first a second offer (one more broadcast, 90 s more), then,
      if still untaken, a system cancel with a full refund and a consumer push.
      See ``BookingService.advance_paid_unclaimed``.

    ``scheduled_at IS NULL OR scheduled_at <= now`` (SVC-243) guards both: a
    booking scheduled two hours out legitimately sits REQUESTED until its slot.
    """
    from app.database import SyncSessionLocal

    now = datetime.now(UTC)
    due = [
        Booking.status == BookingStatus.REQUESTED,
        Booking.offer_expires_at < now,
        or_(Booking.scheduled_at.is_(None), Booking.scheduled_at <= now),
    ]

    with SyncSessionLocal() as db:
        expired_ids = (
            db.execute(
                update(Booking)
                .where(*due, Booking.payment_status == PaymentStatus.PENDING)
                .values(status=BookingStatus.EXPIRED)
                .returning(Booking.id)
            )
            .scalars()
            .all()
        )
        # The system's own transition (changed_by NULL), one row per booking,
        # so the admin timeline shows the expiry instead of stopping at
        # "requested". Same commit as the UPDATE.
        if expired_ids:
            from app.models.booking_status_log import BookingStatusLog

            db.add_all(
                BookingStatusLog(
                    booking_id=booking_id,
                    from_status=BookingStatus.REQUESTED.value,
                    to_status=BookingStatus.EXPIRED.value,
                    changed_by=None,
                    metadata_={"reason": "offer_window_elapsed"},
                    note="offer window elapsed",
                )
                for booking_id in expired_ids
            )
        db.commit()
        expired = len(expired_ids)
        for booking_id, counts in _offer_counts(db, expired_ids).items():
            _log_no_acceptance(booking_id, *counts, outcome="expired")

        paid_ids = (
            db.execute(
                select(Booking.id).where(
                    *due,
                    Booking.payment_status == PaymentStatus.PAID,
                    Booking.payment_method == PaymentMethod.ONLINE,
                    Booking.provider_id.is_(None),
                )
            )
            .scalars()
            .all()
        )
        # Counted before advancing: a second offer broadcasts again and adds
        # offer rows of its own.
        paid_counts = _offer_counts(db, paid_ids)

    outcomes = []
    for booking_id in paid_ids:
        outcome = _advance_paid(booking_id)
        outcomes.append(outcome)
        # "skipped": it no longer qualified (taken meanwhile), so not a no-taker.
        if outcome != "skipped" and booking_id in paid_counts:
            _log_no_acceptance(booking_id, *paid_counts[booking_id], outcome=outcome)
    return (
        f"Expired {expired} stale bookings; paid: "
        f"{outcomes.count('second_offer')} second offer, "
        f"{outcomes.count('refunded')} refunded, "
        f"{outcomes.count('failed')} failed"
    )


def _offer_counts(db, booking_ids) -> dict:
    """``{booking_id: (offers_sent, declined, reopened)}`` for the
    ``dispatch.no_acceptance`` lines (SVC-349). One query for the lot.

    Never in the way of the sweep: it runs outside the expiry's transaction,
    and a failure rolls back only its own read (so the session stays usable)
    and comes back empty, so those bookings just go unlogged.
    """
    if not booking_ids:
        return {}
    from app.models.booking_offer import BookingOffer, OfferResponse

    try:
        rows = db.execute(
            select(
                Booking.id,
                func.count(BookingOffer.id),
                func.count(BookingOffer.id).filter(BookingOffer.response == OfferResponse.DECLINED),
                Booking.reopened_at.is_not(None),
            )
            .outerjoin(BookingOffer, BookingOffer.booking_id == Booking.id)
            .where(Booking.id.in_(booking_ids))
            .group_by(Booking.id)
        ).all()
    except Exception:
        db.rollback()
        logger.exception("dispatch.no_acceptance_count_failed booking_ids=%s", list(booking_ids))
        return {}
    return {booking_id: tuple(counts) for booking_id, *counts in rows}


def _log_no_acceptance(booking_id, offers_sent, declined, reopened, *, outcome: str) -> None:
    """A booking's offer window ran out with nobody taking it (SVC-349): the
    other way a consumer ends up at "No partners found nearby", beside
    ``dispatch.no_providers``. ``outcome`` is ``expired`` for an unpaid booking,
    else what ``_advance_paid`` did with it."""
    logger.warning(
        "dispatch.no_acceptance booking_id=%s offers_sent=%d declined=%d reopened=%s outcome=%s",
        booking_id,
        offers_sent,
        declined,
        reopened,
        outcome,
    )


def _advance_paid(booking_id) -> str:
    """One PAID, untaken booking one step along (SVC-344), in its own session and
    transaction, so one booking's refund failure can't touch another's.

    Pushes are enqueued only after the commit: a push for a step that then
    rolled back would tell the consumer about a refund that never happened.
    """
    import asyncio

    from app.database import AsyncSessionLocal
    from app.services.booking_service import BookingService
    from app.tasks.notification_tasks import send_booking_notification

    async def _run() -> str | None:
        async with AsyncSessionLocal() as session:
            try:
                outcome = await BookingService(session).advance_paid_unclaimed(booking_id)
                await session.commit()
                return outcome
            except Exception:
                await session.rollback()
                raise

    try:
        outcome = asyncio.run(_run())
    except Exception:
        # Most likely the Razorpay refund call. Nothing was saved, so the
        # booking is still REQUESTED + PAID and the next sweep tries again.
        logger.exception("booking.no_taker_refund_failed booking_id=%s", booking_id)
        return "failed"

    if outcome == "second_offer":
        send_booking_notification.delay(booking_id=str(booking_id), event="new_request")
    elif outcome == "refunded":
        send_booking_notification.delay(booking_id=str(booking_id), event="no_taker_refunded")
    return outcome or "skipped"


@celery_app.task(name="app.tasks.cleanup_tasks.expire_abandoned_payments")
def expire_abandoned_payments():
    """
    SVC-190 (G14): Celery Beat task — flip PENDING → EXPIRED on bookings whose
    Razorpay order is past its `expire_by` and was never captured.

    Wire-side, Razorpay already refuses payment after `expire_by`
    (`PAYMENT_ORDER_EXPIRY_SECONDS` at order creation); this task keeps the
    *local* view in agreement so reconciliation (§8) and the admin surfaces
    can distinguish "waiting on the customer" from "the customer never
    paid".

    Bounds:
    - Only rows with `razorpay_order_id IS NOT NULL` — cash bookings have
      no order and are irrelevant here.
    - Only `payment_status == PENDING` — a PAID booking stays PAID (the
      order was captured before it expired); a REFUNDED / already-EXPIRED
      row is a no-op.
    - Waits `PAYMENT_ORDER_EXPIRY_SECONDS + PAYMENT_ORDER_EXPIRY_GRACE_SECONDS`
      past the booking's `created_at` before flipping. The grace absorbs
      webhook redelivery jitter so we never mark EXPIRED locally while a
      late `payment.captured` for the same order is still landing.

    Deliberately does NOT touch `Booking.status` — that is owned by
    fulfilment (`expire_stale_bookings`) or an explicit cancel path.
    Idempotent by construction (only PENDING flips).
    """
    from app.database import SyncSessionLocal

    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.PAYMENT_ORDER_EXPIRY_SECONDS + settings.PAYMENT_ORDER_EXPIRY_GRACE_SECONDS
    )

    with SyncSessionLocal() as db:
        result = db.execute(
            update(Booking)
            .where(Booking.razorpay_order_id.is_not(None))
            .where(Booking.payment_status == PaymentStatus.PENDING)
            .where(Booking.created_at < cutoff)
            .values(payment_status=PaymentStatus.EXPIRED)
        )
        db.commit()
        return f"Expired {result.rowcount} abandoned payments"
