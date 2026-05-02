"""
Cleanup Celery tasks — periodic jobs run by Celery Beat.
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy import update

from app.tasks.celery_app import celery_app
from app.models.booking import Booking, BookingStatus
from app.config import get_settings

settings = get_settings()


@celery_app.task(name="app.tasks.cleanup_tasks.expire_stale_bookings")
def expire_stale_bookings():
    """
    Celery Beat task: runs every 30 seconds.
    Bulk-expires REQUESTED bookings older than BOOKING_REQUEST_TIMEOUT_SECONDS.
    """
    from app.database import SyncSessionLocal

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.BOOKING_REQUEST_TIMEOUT_SECONDS
    )

    with SyncSessionLocal() as db:
        result = db.execute(
            update(Booking)
            .where(Booking.status == BookingStatus.REQUESTED)
            .where(Booking.created_at < cutoff)
            .values(status=BookingStatus.EXPIRED)
        )
        db.commit()
        return f"Expired {result.rowcount} stale bookings"
