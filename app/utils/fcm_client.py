"""
Firebase Cloud Messaging push notification client.
firebase-admin is initialised lazily on first use.
"""

import firebase_admin
from firebase_admin import credentials, messaging

from app.config import get_settings

settings = get_settings()

_app: firebase_admin.App | None = None


def _get_app() -> firebase_admin.App:
    global _app
    if _app is None:
        if not settings.FCM_CREDENTIALS_PATH:
            raise RuntimeError(
                "FCM_CREDENTIALS_PATH is not configured. "
                "Provide the path to your Firebase service account JSON."
            )
        cred = credentials.Certificate(settings.FCM_CREDENTIALS_PATH)
        _app = firebase_admin.initialize_app(cred)
    return _app


def send_push_notification(
    token: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> str:
    """
    Send a single push notification to a device token.
    Returns the FCM message ID on success.
    Raises firebase_admin.exceptions.FirebaseError on failure.
    """
    _get_app()
    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data=data or {},
        token=token,
    )
    return messaging.send(message)
