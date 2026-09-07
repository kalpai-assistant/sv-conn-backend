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


def _android_config(channel_id: str | None):
    """Route an Android push to a specific notification channel (SVC-278).

    Android takes the sound, the vibration and whether the notification
    interrupts from the *channel*, not from the message — so a job request can
    only sound different from an app-update nudge by naming a channel the app
    created. ``priority="high"`` alongside it governs delivery (it wakes a dozing
    device); the two are unrelated and both are needed for a time-critical push.

    ``None`` means "leave Android to its defaults", which is what every event
    other than a new job request wants: only one channel should stand out, or
    none of them does.

    The id must match ``JOB_REQUEST_CHANNEL_ID`` in the app's
    ``notification-channels.ts``. A mismatch is silent — Android falls back to
    the default channel and the push simply sounds ordinary.
    """
    if channel_id is None:
        return None
    return messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(channel_id=channel_id),
    )


def send_push_notification(
    token: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
    android_channel_id: str | None = None,
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
        android=_android_config(android_channel_id),
    )
    return messaging.send(message)


# FCM's multicast API caps at 500 tokens per call. Anything beyond that
# is one HTTP request per chunk. The number is a hard FCM limit — do not
# raise it thinking it'll go faster.
_MULTICAST_CHUNK_SIZE = 500


def send_multicast(
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> dict[str, int]:
    """
    Fan out one push to many device tokens (SVC-227). Used by
    ``broadcast_update_available`` and any future admin broadcast.

    Chunked at 500 (FCM's per-call cap). Returns a summary
    ``{"success_count": N, "failure_count": N}`` so the caller can log
    without walking every response.

    Non-production without credentials mirrors ``send_push_notification``:
    log-and-count-as-success so tests and dev can drive the flow without
    real Firebase creds. Production still hard-refuses on missing creds
    so silent no-delivery cannot happen there.

    Failed tokens are logged individually so an operator can spot a
    pattern (e.g. every push failing = credential rotation needed) but
    the return is aggregate to keep the caller's log line short.
    """
    if not tokens:
        return {"success_count": 0, "failure_count": 0}

    if not is_configured():
        if settings.ENVIRONMENT == "production":
            raise RuntimeError(
                "FCM_CREDENTIALS_PATH is not configured. "
                "Provide the path to your Firebase service account JSON."
            )
        logger.warning(
            "fcm_not_configured_multicast_logged_not_sent",
            recipient_count=len(tokens),
            title=title,
            body=body,
            data=data or {},
            environment=settings.ENVIRONMENT,
        )
        return {"success_count": len(tokens), "failure_count": 0}

    _get_app()
    success = 0
    failure = 0
    for start in range(0, len(tokens), _MULTICAST_CHUNK_SIZE):
        chunk = tokens[start : start + _MULTICAST_CHUNK_SIZE]
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data=data or {},
            tokens=chunk,
        )
        # send_each_for_multicast returns per-token results — needed so a
        # bad token in the batch doesn't fail-fast the whole chunk. The
        # legacy send_multicast() is deprecated in firebase-admin >= 6.
        batch = messaging.send_each_for_multicast(message)
        success += batch.success_count
        failure += batch.failure_count
        for i, resp in enumerate(batch.responses):
            if not resp.success:
                # Token itself is not logged — the logging pipeline
                # redacts anything with "token" in the key anyway (SVC-46).
                logger.warning(
                    "fcm_multicast_send_failed",
                    chunk_index=start + i,
                    error=str(resp.exception),
                )
    return {"success_count": success, "failure_count": failure}
