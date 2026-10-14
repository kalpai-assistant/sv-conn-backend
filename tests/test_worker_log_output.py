"""
Worker logs must reach the process's real stdout (SVC-349).

Celery's prefork worker replaces ``sys.stdout`` with a ``LoggingProxy`` before
forking (``worker_redirect_stdouts``, on by default). Each child then calls
``configure_logging`` from ``worker_process_init``. A handler built on
``sys.stdout`` at that point writes into the proxy, which feeds the text back
into logging, and the proxy's recursion guard drops it without a trace.

That is how every app log line from the worker (``broadcast.recipients``,
``booking.unbroadcastable``, every ``logger.exception``) and even Celery's own
"Task … succeeded" went missing from CloudWatch from SC-46 until SVC-349. Only
the main process's plain-text lines survived, so the log group looked alive.

Standalone: no DB, no Redis.
"""

import json
import logging
import sys

import pytest

from app.logging_config import configure_logging
from app.tasks.celery_app import celery_app


@pytest.fixture
def restore_logging():
    """configure_logging and the redirect are process-global; put both back."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, sys.stdout, sys.stderr)
    yield
    root.handlers[:], level, sys.stdout, sys.stderr = saved
    root.setLevel(level)


def test_a_worker_child_log_line_reaches_the_real_stdout(capfd, restore_logging):
    # What the worker's main process does before forking its pool…
    celery_app.log.redirect_stdouts_to_logger(
        logging.getLogger("celery.redirected"), logging.WARNING
    )
    # …and what each child then does in worker_process_init.
    configure_logging(level="INFO", json_logs=True)

    logging.getLogger("app.tasks.notification_tasks").warning(
        "dispatch.no_providers booking_id=%s reason=%s", "b-1", "none_online"
    )
    sys.__stdout__.flush()

    out = capfd.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert {
        "event": "dispatch.no_providers booking_id=b-1 reason=none_online",
        "level": "warning",
        "logger": "app.tasks.notification_tasks",
    }.items() <= next(iter(records), {}).items(), out
