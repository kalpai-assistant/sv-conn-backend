from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from app.config import get_settings
from app.logging_config import configure_logging

settings = get_settings()

celery_app = Celery(
    "eazyserv",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
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
    beat_schedule={
        "expire-stale-bookings": {
            "task": "app.tasks.cleanup_tasks.expire_stale_bookings",
            "schedule": 30.0,  # Every 30 seconds
        },
        "reconcile-payouts": {
            "task": "app.tasks.payment_tasks.reconcile_payouts",
            "schedule": crontab(hour=2, minute=0),  # Nightly at 02:00 (hits RazorpayX API)
        },
    },
)
