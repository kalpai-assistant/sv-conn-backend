"""
Notification Celery tasks — handles async push + SMS dispatch for booking events.
Uses a synchronous DB session (SyncSessionLocal) because Celery workers are not async.
"""
from app.tasks.celery_app import celery_app
from app.utils.fcm_client import send_push_notification
from app.utils.sms_client import get_sms_client


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


# Provider-facing payout templates, keyed by SettlementStatus value.
#
# Copy is deliberately neutral for the unhappy states: a failed or reversed payout
# is almost always an internal/bank-side issue we resolve ourselves (re-queued by an
# admin), so telling the provider "payout failed" invites support calls about
# something they cannot act on. They are told we're on it instead.
PAYOUT_NOTIFICATION_TEMPLATES: dict[str, dict[str, str]] = {
    "processing": {
        "title": "Payout On Its Way",
        "body": "Your payout of {amount} has been initiated and should reach your account shortly.",
    },
    "completed": {
        "title": "Payout Received",
        "body": "{amount} has been credited to your account.",
    },
    "failed": {
        "title": "Payout Update",
        "body": "We're still processing your payout of {amount}. We'll update you shortly.",
    },
    "reversed": {
        "title": "Payout Update",
        "body": "Your payout of {amount} was returned to us. We're looking into it "
                "and will update you shortly.",
    },
}


def _format_rupees(amount_paise: int) -> str:
    """Format a paise amount as rupees for notification copy (₹1,234 / ₹1,234.50)."""
    return f"₹{amount_paise / 100:,.2f}".removesuffix(".00")


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
                from sqlalchemy import select, func
                from app.models.provider_profile import ProviderProfile, KYCStatus
                from app.models.provider_service import ProviderService
                from app.models.user import User
                from app.models.address import Address

                # Get the booking address location directly
                address = db.get(Address, booking.consumer_address_id)
                if not address or not address.location:
                    return

                # PostGIS broadcast query: find all verified, online, subscribed providers nearby
                stmt = select(User).join(ProviderProfile, User.id == ProviderProfile.user_id).join(
                    ProviderService, ProviderService.provider_id == ProviderProfile.user_id
                ).where(
                    ProviderService.service_id == booking.service_id,
                    ProviderProfile.is_online == True,
                    ProviderProfile.kyc_status == KYCStatus.VERIFIED,
                    func.ST_DWithin(
                        ProviderProfile.service_area_center,
                        address.location,
                        ProviderProfile.service_area_radius_km * 1000
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


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    name="app.tasks.notification_tasks.send_payout_notification",
)
def send_payout_notification(
    self,
    provider_id: str,
    event: str,
    amount_paise: int,
    payout_id: str | None = None,
):
    """Send a provider push notification for a payout lifecycle event.

    One push per *payout*, not per booking: a payout settles a whole batch of the
    provider's completed bookings, so the recipient is the provider and the copy
    carries the batch total. ``event`` is a ``SettlementStatus`` value passed in by
    the caller rather than re-read from the DB, so the message always describes the
    transition that triggered it.

    No-ops when the provider has no FCM token (single-device: the model has one
    ``user.fcm_token``).
    """
    from app.database import SyncSessionLocal
    from app.models.user import User

    try:
        template = PAYOUT_NOTIFICATION_TEMPLATES.get(event)
        if not template:
            return

        with SyncSessionLocal() as db:
            provider = db.get(User, provider_id)
            if not provider or not provider.fcm_token:
                return

            data = {"type": "payout", "event": event}
            if payout_id:
                data["payout_id"] = payout_id

            send_push_notification(
                token=provider.fcm_token,
                title=template["title"],
                body=template["body"].format(amount=_format_rupees(amount_paise)),
                data=data,
            )

    except Exception as exc:
        self.retry(exc=exc)
