from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from app.config import get_settings
from app.logging_config import configure_logging

settings = get_settings()


def _redis_url_with_ssl_reqs(url: str) -> str:
    """Append ``ssl_cert_reqs=CERT_REQUIRED`` when Redis is over TLS.

    Celery's Redis result-backend initialiser hard-refuses a ``rediss://``
    URL that doesn't declare an explicit ``ssl_cert_reqs`` param
    (celery/backends/redis.py raises
    ``E_REDIS_SSL_CERT_REQS_MISSING_INVALID`` at Backend construction time,
    which is the first ``.delay()`` from any process). Beta's Upstash URL
    is ``rediss://…`` — every route path that fires a Celery task
    (bookings, payouts, the SVC-227 update-broadcast) trips this.

    Discovered when SVC-227.2's release endpoint was the first
    ``notify=true`` call from the API after the SSM param existed.
    ``CERT_REQUIRED`` (not ``CERT_NONE``) because Upstash presents a
    proper CA-signed cert; we want the verify.

    No-op on non-TLS ``redis://`` (local dev) and on URLs that already
    declare the param.
    """
    if url.startswith("rediss://") and "ssl_cert_reqs" not in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}ssl_cert_reqs=CERT_REQUIRED"
    return url


celery_app = Celery(
    "eazyserv",
    broker=_redis_url_with_ssl_reqs(settings.REDIS_URL),
    backend=_redis_url_with_ssl_reqs(settings.REDIS_URL),
)


# SVC-46: workers don't run through app/main.py, so structlog needs to be
# configured explicitly per worker process. worker_process_init fires once
# per prefork child, before any task runs.
@worker_process_init.connect
def _configure_worker_logging(**_):
    configure_logging(level=settings.LOG_LEVEL, json_logs=settings.LOG_JSON)


# Import for the side effect of registering the log-correlation signal
# handlers (before_task_publish / task_prerun / task_postrun). Kept as an
# import-for-side-effects rather than a call because signals self-register
# via decorators.
from app.tasks import log_correlation  # noqa: E402,F401

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_default_queue="default",
    task_routes={
        "app.tasks.notification_tasks.*": {"queue": "notifications"},
        "app.tasks.payment_tasks.*": {"queue": "payments"},
    },
    # --- Upstash command-budget cuts ---
    #
    # Kombu's Redis transport defaults to a 1-second BRPOP polling interval;
    # with our 2-worker × 3-queue setup that's ~6 idle BRPOPs/sec, or
    # ~250k commands/day at zero traffic. Beta hit the 500k/2-day Upstash
    # free-tier ceiling on this alone.
    #
    # `polling_interval=15.0` cuts idle BRPOPs by 15×. Trade-off: worst-case
    # task pickup latency goes 1s → 15s. For our tasks (FCM push after a
    # booking transition, cleanup sweeps) that's imperceptible — a consumer
    # doesn't distinguish a push at 1s from a push at 15s.
    #
    # `visibility_timeout` stated explicitly at 1h (the default) because
    # kombu forgets to persist it across worker restarts if you don't.
    broker_transport_options={
        "polling_interval": 15.0,
        "visibility_timeout": 3600,
    },
    # Nobody calls `.get()` on task results anywhere in the codebase — every
    # dispatch is fire-and-forget via `.delay()`. The default result-backend
    # writes are pure waste (SET per task completion + eventual EXPIRE).
    # Turning off cuts the result-backend slice entirely.
    task_ignore_result=True,
    beat_schedule={
        # Ticket timeout is 120s — a 60s sweep still catches expiry within
        # one grace window and halves the dispatch overhead vs. the old 30s
        # cadence.
        "expire-stale-bookings": {
            "task": "app.tasks.cleanup_tasks.expire_stale_bookings",
            "schedule": 60.0,
        },
        # SVC-190 (G14): the local mirror of Razorpay's order-expiry. The
        # window is 15 min + grace, so a 10-min sweep is still comfortably
        # within the grace and halves the tick count vs. the old 300s.
        "expire-abandoned-payments": {
            "task": "app.tasks.cleanup_tasks.expire_abandoned_payments",
            "schedule": 600.0,
        },
        "reconcile-payouts": {
            "task": "app.tasks.payment_tasks.reconcile_payouts",
            "schedule": crontab(hour=2, minute=0),  # Nightly at 02:00 (hits RazorpayX API)
        },
    },
)
