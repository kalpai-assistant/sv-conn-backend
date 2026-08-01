"""
Notification Celery tasks — handles async push + SMS dispatch for booking events.
Uses a synchronous DB session (SyncSessionLocal) because Celery workers are not async.
"""

from app.tasks.celery_app import celery_app
from app.utils.fcm_client import send_push_notification

NOTIFICATION_TEMPLATES: dict[str, dict[str, str]] = {
    "new_request": {
        "title": "New Booking Request!",
        "body": "You have a new service request. Accept within 2 minutes.",
    },
    "accepted": {
        "title": "Booking Confirmed",
        "body": "Your provider has accepted the booking and will arrive soon.",
    },
    "in_progress": {
        "title": "Service Started",
        "body": "Your service is now in progress.",
    },
    "completed": {
        "title": "Service Completed",
        "body": "Your service has been completed. Please proceed to payment.",
    },
}


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    name="app.tasks.notification_tasks.send_booking_notification",
)
def send_booking_notification(self, booking_id: str, event: str):
    """Send push notification for booking lifecycle events."""
    from app.database import SyncSessionLocal
    from app.models.booking import Booking

    try:
        with SyncSessionLocal() as db:
            booking = db.get(Booking, booking_id)
            if not booking:
                return

            template = NOTIFICATION_TEMPLATES.get(event)
            if not template:
                return

            if event == "new_request":
                from sqlalchemy import func, select

                from app.models.address import Address
                from app.models.provider_profile import KYCStatus, ProviderProfile
                from app.models.provider_service import ProviderService
                from app.models.user import User

                # Get the booking address location directly
                address = db.get(Address, booking.consumer_address_id)
                if not address or not address.location:
                    return

                # PostGIS broadcast query: find all verified, online, subscribed providers nearby
                stmt = (
                    select(User)
                    .join(ProviderProfile, User.id == ProviderProfile.user_id)
                    .join(ProviderService, ProviderService.provider_id == ProviderProfile.user_id)
                    .where(
                        ProviderService.service_id == booking.service_id,
                        ProviderProfile.is_online.is_(True),
                        ProviderProfile.kyc_status == KYCStatus.VERIFIED,
                        func.ST_DWithin(
                            ProviderProfile.service_area_center,
                            address.location,
                            ProviderProfile.service_area_radius_km * 1000,
                        ),
                    )
                )
                recipients = db.execute(stmt).scalars().all()
            else:
                recipients = [booking.provider if event == "accepted" else booking.consumer]

            for recipient in recipients:
                if recipient and getattr(recipient, "fcm_token", None):
                    send_push_notification(
                        token=recipient.fcm_token,
                        title=template["title"],
                        body=template["body"],
                        data={"booking_id": booking_id, "event": event},
                    )

    except Exception as exc:
        self.retry(exc=exc)
