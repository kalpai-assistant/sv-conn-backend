from celery import Celery
from celery.schedules import crontab
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "serviceconnect",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

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
