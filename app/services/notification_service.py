"""
Notification service: thin orchestration layer that delegates to FCM and SMS clients.
Kept separate so the notification channel strategy can evolve without touching
business services (Open/Closed principle).
"""
from app.utils.fcm_client import send_push_notification
from app.utils.sms_client import get_sms_client


class NotificationService:
    """Sends push and SMS notifications. Callers never import FCM/SMS directly."""

    async def send_push(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None:
        """Fire-and-forget push notification."""
        try:
            send_push_notification(token=token, title=title, body=body, data=data or {})
        except Exception:
            # Push failures are non-fatal; they are retried by Celery tasks
            pass

    async def send_sms(self, phone: str, otp: str) -> None:
        """Send an OTP via the configured SMS provider."""
        client = get_sms_client()
        await client.send_otp(phone, otp)
