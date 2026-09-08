"""
Celery log correlation (SVC-46 follow-up).

Ties worker log records to the HTTP request (if any) that enqueued them, plus
adds the Celery task ID so a specific worker run is identifiable in aggregate
logs, plus lifecycle events (success / failure / retry) so debugging
"did that task actually finish?" is a grep, not a debate.

Three boundaries to bridge:

1. **Publisher → broker**: `before_task_publish` reads the current
   `request_id_var` (set by `RequestIDMiddleware` when we're inside an HTTP
   handler) and stuffs it into the task's headers. When there's no active
   request (Beat schedule, admin script), the header is omitted.

2. **Broker → worker**: `task_prerun` reads the header back off the running
   task and binds it into the worker's `request_id_var`. It also binds
   `task_id_var` unconditionally so *every* task log carries the task id,
   even Beat-scheduled ones with no originator request.

3. **Task lifecycle**: `task_success`, `task_failure`, `task_retry` emit
   one structured log line each so we can tell from CloudWatch alone
   whether a task completed cleanly, crashed (with the exception), or is
   retrying. Celery's default event stream doesn't reach our JSON logs.

`task_postrun` resets contextvars — a worker process handles many tasks; the
contextvars must not leak between them (contextvars are per-thread by
default, but the celery prefork pool reuses threads).

Registration happens by importing this module from `celery_app`; the signal
decorators wire themselves.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Any

from celery.signals import (
    before_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    task_success,
)

from app.logging_config import request_id_var, task_id_var

_HEADER_KEY = "request_id"
_task_start_var: ContextVar[float | None] = ContextVar("task_start", default=None)

logger = logging.getLogger(__name__)


@before_task_publish.connect
def _propagate_request_id(headers: dict | None = None, **_: Any) -> None:
    """Enqueuing side: stamp the current request_id onto outgoing task headers.

    Runs in the publisher process (the FastAPI worker). Silent when there's
    no active request — e.g. a Beat-scheduled or admin-shell dispatch.
    """
    if headers is None:
        return
    rid = request_id_var.get()
    if rid is not None:
        headers[_HEADER_KEY] = rid


@task_prerun.connect
def _bind_correlation_ids(task_id: str | None = None, task=None, **_: Any) -> None:
    """Worker side: bind task_id + (if present) request_id from task headers.

    Celery exposes headers on `task.request.get("request_id")` — the same
    slot the publisher wrote to. Missing header = task was published without
    an originating request (Beat / admin script), so we bind task_id only.

    Also stamps a monotonic start time so success/failure/retry can report
    duration_ms without touching wall-clock.
    """
    if task_id is not None:
        task_id_var.set(task_id)
    if task is not None:
        rid = task.request.get(_HEADER_KEY)
        if rid:
            request_id_var.set(rid)
    _task_start_var.set(time.monotonic())


@task_postrun.connect
def _reset_correlation_ids(**_: Any) -> None:
    """Reset contextvars after each task.

    Celery's prefork pool reuses worker threads across tasks; without this
    reset, task N+1's logs would carry task N's IDs whenever the header
    wasn't set on N+1.
    """
    task_id_var.set(None)
    request_id_var.set(None)
    _task_start_var.set(None)


def _duration_ms() -> float | None:
    start = _task_start_var.get()
    return None if start is None else round((time.monotonic() - start) * 1000, 1)


@task_success.connect
def _log_task_success(sender=None, result=None, **_: Any) -> None:
    """One-line completion log. Fires per-task in the worker process."""
    logger.info(
        "task.success task=%s duration_ms=%s",
        sender.name if sender else "<unknown>",
        _duration_ms(),
    )


@task_failure.connect
def _log_task_failure(
    sender=None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    einfo=None,
    **_: Any,
) -> None:
    """One-line failure log with the exception. Fires when a task raises
    (before Celery decides whether to retry). ``einfo.traceback`` is the
    formatted Python traceback — logged verbatim so a grep for "task.failure"
    in CloudWatch surfaces both the exception type and the failing line.
    """
    logger.error(
        "task.failure task=%s task_id=%s duration_ms=%s exc_type=%s exc=%s\n%s",
        sender.name if sender else "<unknown>",
        task_id,
        _duration_ms(),
        type(exception).__name__ if exception is not None else None,
        exception,
        getattr(einfo, "traceback", "") if einfo is not None else "",
    )


@task_retry.connect
def _log_task_retry(sender=None, request=None, reason=None, **_: Any) -> None:
    """Task raised an exception AND Celery decided to retry. ``request.retries``
    is the retry index (0 on first try, incremented before each subsequent).
    """
    logger.warning(
        "task.retry task=%s attempt=%s reason=%s",
        sender.name if sender else "<unknown>",
        getattr(request, "retries", None),
        reason,
    )
