"""
Cleanup Celery tasks — periodic jobs run by Celery Beat.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.config import get_settings
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.tasks.celery_app import celery_app

settings = get_settings()


@celery_app.task(name="app.tasks.cleanup_tasks.expire_stale_bookings")
def expire_stale_bookings():
    """
    Celery Beat task: runs every 30 seconds.
    Bulk-expires REQUESTED bookings older than BOOKING_REQUEST_TIMEOUT_SECONDS.
    """
    from app.database import SyncSessionLocal

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.BOOKING_REQUEST_TIMEOUT_SECONDS)

    with SyncSessionLocal() as db:
        result = db.execute(
            update(Booking)
            .where(Booking.status == BookingStatus.REQUESTED)
            .where(Booking.created_at < cutoff)
            .values(status=BookingStatus.EXPIRED)
        )
        db.commit()
        return f"Expired {result.rowcount} stale bookings"


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
