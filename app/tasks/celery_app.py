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


# Broker + result-backend URLs, sourced from Celery-specific settings and
# falling back to REDIS_URL when unset. Decoupled from the cache Redis so the
# broker can move off Upstash by env-var flip alone (set CELERY_BROKER_URL to
# `sqs://` and kombu picks up the SQS transport — no code change here).
_raw_broker_url = settings.CELERY_BROKER_URL or settings.REDIS_URL
_is_sqs_broker = _raw_broker_url.startswith("sqs")

# The ssl-reqs shim only applies to Redis (`rediss://`); an SQS URL passes
# through unchanged. Result backend is always Redis-shaped — SQS has no
# result-backend concept and `task_ignore_result=True` below means nothing
# writes there anyway, so keeping the URL there is a no-op we don't need to
# refactor.
_broker_url = _raw_broker_url if _is_sqs_broker else _redis_url_with_ssl_reqs(_raw_broker_url)
_backend_url = _redis_url_with_ssl_reqs(settings.CELERY_RESULT_BACKEND_URL or settings.REDIS_URL)

celery_app = Celery(
    "eazyserv",
    broker=_broker_url,
    backend=_backend_url,
)


def _broker_transport_options() -> dict:
    """Broker-specific tuning. SQS and Redis need very different knobs, and
    each transport silently ignores the other's keys — so we branch here to
    keep intent obvious rather than dumping every possible option into one
    dict.

    The Redis path is what PRs #131 and #134 landed:
    - `polling_interval=15.0` bumps kombu's idle BRPOP cadence 1s → 15s
    - `visibility_timeout=3600` stated explicitly so kombu persists it across
      worker restarts (which it forgets otherwise)

    The SQS path relies on native long-polling — the fanout + control-channel
    chatter that dominates our Upstash bill (see INFO STATS diagnosis 2026-08-25)
    doesn't exist on SQS. Even at zero traffic we're bounded by one
    ReceiveMessage per 20s per worker, plus per-task SendMessage/DeleteMessage.
    """
    if _is_sqs_broker:
        opts: dict = {
            # Long polling — one ReceiveMessage call held open for up to 20s
            # (SQS max). Kombu counts these as 1 API call regardless of wait
            # duration, so idle cost is ~3/min per worker vs Redis's constant
            # BRPOP + fanout traffic.
            "wait_time_seconds": 20,
            # Match the Redis path so a task-lease semantics don't shift
            # under callers just by changing broker.
            "visibility_timeout": 3600,
        }
        # `predefined_queues` skips kombu's ListQueues discovery call at
        # startup — matches the tight IAM policy (only per-queue actions
        # granted, no account-wide ListQueues). Only set when the URL is
        # actually configured; otherwise kombu will still attempt discovery
        # and give a clearer error than us guessing a URL wrong.
        if settings.CELERY_SQS_QUEUE_URL:
            opts["predefined_queues"] = {
                # Key must match `task_default_queue` below; kombu uses it
                # to route .delay() calls to the right SQS URL.
                "default": {"url": settings.CELERY_SQS_QUEUE_URL},
            }
        # Region — kombu-SQS defaults to us-east-1 and would silently create
        # queues there without this. Derived from the queue URL when we have
        # it, or from an explicit env var, or the App Runner default of
        # ap-south-1.
        if settings.CELERY_SQS_QUEUE_URL:
            # e.g. https://sqs.ap-south-1.amazonaws.com/12345/foo
            _region = settings.CELERY_SQS_QUEUE_URL.split(".")[1]
        else:
            _region = settings.AWS_REGION or "ap-south-1"
        opts["region"] = _region
        return opts

    return {
        "polling_interval": 15.0,
        "visibility_timeout": 3600,
    }


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
    # Everything on one queue — kombu polls each declared queue with a
    # separate BRPOP, so 3 queues × 2 worker children = 6 idle pollers, and
    # collapsing to 1 queue is another 3× cut on top of PR #131's polling
    # interval bump. The per-queue split (notifications / payments) was
    # ceremony from the LLD that never earned its keep at beta scale — one
    # worker instance drains everything in order regardless. Re-introduce
    # per-queue routing + dedicated worker pools when traffic warrants
    # priority isolation (roughly: >10 req/sec sustained).
    #
    # Broker-specific — see `_broker_transport_options` for the SQS vs Redis
    # branch. Broker choice is env-driven via CELERY_BROKER_URL.
    broker_transport_options=_broker_transport_options(),
    # Nobody calls `.get()` on task results anywhere in the codebase — every
    # dispatch is fire-and-forget via `.delay()`. The default result-backend
    # writes are pure waste (SET per task completion + eventual EXPIRE).
    # Turning off cuts the result-backend slice entirely.
    task_ignore_result=True,
    beat_schedule={
        # Back to 30s for SVC-344. The sweep now also moves PAID bookings
        # nobody took into their 90s second offer and then refunds them; each
        # step waits for a sweep, so the cadence adds straight onto how long a
        # paying customer waits (worst case ~4.5 min at 30s, ~5.5 at 60s). One
        # bulk UPDATE plus a small SELECT per run.
        "expire-stale-bookings": {
            "task": "app.tasks.cleanup_tasks.expire_stale_bookings",
            "schedule": 30.0,
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
