"""
Firebase Cloud Messaging push notification client.
firebase-admin is initialised lazily on first use.

When ``FCM_CREDENTIALS_PATH`` is unset the module logs each push instead of
sending it, so the app runs without Firebase credentials outside production —
see ``send_push_notification``.
"""
import firebase_admin
import structlog
from firebase_admin import credentials, messaging

from app.config import get_settings

settings = get_settings()
logger = structlog.get_logger(__name__)

_app: firebase_admin.App | None = None

# Returned in place of an FCM message ID when the push was logged, not sent.
# Callers only pass the return value through, so a sentinel is enough to make
# "this never left the building" visible in a trace.
NOT_SENT = "fcm-not-configured"


def is_configured() -> bool:
    """Whether real credentials are available to send with."""
    return bool(settings.FCM_CREDENTIALS_PATH)


def _get_app() -> firebase_admin.App:
    global _app
    if _app is None:
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

    **Without credentials, outside production, the push is logged and not sent**
    and ``NOT_SENT`` is returned. Firebase credentials are per-project and are
    not something a dev machine, CI runner or the test suite should hold — and
    none of them should be firing real notifications at real devices anyway.
    Logging the full payload also makes the ``data`` dict visible while the
    client apps are being built against it, which is the part most likely to be
    wrong (SVC-156).

    In **production** a missing credential still raises. Silently dropping every
    notification there would be far worse than failing loudly: the symptom is
    invisible — providers simply never hear about bookings — and there is no
    error anywhere to trace it back from. Mirrors the E2E OTP bypass, which is
    likewise a non-production affordance that hard-refuses in production.

    The device token is deliberately not logged: it is redacted by the logging
    pipeline anyway (``token`` is a sensitive-key substring) and it is not the
    part anyone needs while wiring up a client.
    """
    if not is_configured():
        if settings.ENVIRONMENT == "production":
            raise RuntimeError(
                "FCM_CREDENTIALS_PATH is not configured. "
                "Provide the path to your Firebase service account JSON."
            )
        logger.warning(
            "fcm_not_configured_push_logged_not_sent",
            title=title,
            body=body,
            data=data or {},
            environment=settings.ENVIRONMENT,
        )
        return NOT_SENT

    _get_app()
    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data=data or {},
        token=token,
    )
    return messaging.send(message)
