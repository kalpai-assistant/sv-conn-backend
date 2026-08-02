"""
Notification Celery tasks — handles async push + SMS dispatch for booking events.
Uses a synchronous DB session (SyncSessionLocal) because Celery workers are not async.
"""

from app.tasks.celery_app import celery_app
from app.utils.fcm_client import send_push_notification

NOTIFICATION_TEMPLATES: dict[str, dict[str, str]] = {
    "new_request": {
        "title": "New Booking Request!",
        # {service} is filled per booking — see build_new_request_body.
        "body": "{service} — accept within 2 minutes.",
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


def build_new_request_body(service_name: str | None) -> str:
    """Body for the broadcast push: what the job is, and nothing more (SVC-153).

    Naming the service lets a provider decide whether the notification is worth
    opening. The **amount and address deliberately stay out**:

    * the address would be delivered to every nearby provider, including all the
      ones who lose the race, and would persist in OS notification logs — the
      offer endpoint hands it only to eligible providers and stops once the job
      is taken;
    * the amount would have to be rendered as a ₹ string here, and this branch has
      no money formatter. Indian digit grouping (₹10,00,000, not ₹1,000,000) is
      only visibly different above ₹1,00,000, so a naive f-string would look
      correct in review and in every realistic test, then misread for a provider
      one day. ``GET /bookings/{id}/offer`` returns the number and the app formats
      it. See the money note on SVC-153.

    Falls back to generic copy when the service is missing — an FK makes that
    unreachable today, but a push that renders "{service}" literally would be
    worse than a vague one.
    """
    return NOTIFICATION_TEMPLATES["new_request"]["body"].format(
        service=service_name or "A new service request",
    )


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

            body = template["body"]

            if event == "new_request":
                from app.models.address import Address
                from app.models.service import Service
                from app.services.provider_matching import eligible_providers_stmt

                # Get the booking address location directly
                address = db.get(Address, booking.consumer_address_id)
                if not address or not address.location:
                    return

                service = db.get(Service, booking.service_id)
                body = build_new_request_body(service.name if service else None)

                # PostGIS broadcast: verified, online, subscribed providers whose own
                # service radius covers the address. The predicate lives in
                # provider_matching so the endpoint serving the offer behind this push
                # applies the identical rule — see that module's docstring (SVC-153).
                stmt = eligible_providers_stmt(booking.service_id, address.location)
                recipients = db.execute(stmt).scalars().all()
            else:
                recipients = [booking.provider if event == "accepted" else booking.consumer]

            for recipient in recipients:
                if recipient and getattr(recipient, "fcm_token", None):
                    send_push_notification(
                        token=recipient.fcm_token,
                        title=template["title"],
                        body=body,
                        # Payload stays a bare pointer — the card fetches its data
                        # from the offer endpoint, which can revoke access once the
                        # booking is claimed. A push cannot be taken back.
                        data={"booking_id": booking_id, "event": event},
                    )

    except Exception as exc:
        self.retry(exc=exc)
