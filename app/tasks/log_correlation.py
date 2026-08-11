"""
Celery log correlation (SVC-46 follow-up).

Ties worker log records to the HTTP request (if any) that enqueued them, plus
adds the Celery task ID so a specific worker run is identifiable in aggregate
logs.

Two boundaries to bridge:

1. **Publisher → broker**: `before_task_publish` reads the current
   `request_id_var` (set by `RequestIDMiddleware` when we're inside an HTTP
   handler) and stuffs it into the task's headers. When there's no active
   request (Beat schedule, admin script), the header is omitted.

2. **Broker → worker**: `task_prerun` reads the header back off the running
   task and binds it into the worker's `request_id_var`. It also binds
   `task_id_var` unconditionally so *every* task log carries the task id,
   even Beat-scheduled ones with no originator request.

`task_postrun` resets both — a worker process handles many tasks; the
contextvars must not leak between them (contextvars are per-thread by
default, but the celery prefork pool reuses threads).

Registration happens by importing this module from `celery_app`; the signal
decorators wire themselves.
"""

from __future__ import annotations

from typing import Any

from celery.signals import before_task_publish, task_postrun, task_prerun

from app.logging_config import request_id_var, task_id_var

_HEADER_KEY = "request_id"


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
    """
    if task_id is not None:
        task_id_var.set(task_id)
    if task is not None:
        rid = task.request.get(_HEADER_KEY)
        if rid:
            request_id_var.set(rid)


@task_postrun.connect
def _reset_correlation_ids(**_: Any) -> None:
    """Reset both contextvars after each task.

    Celery's prefork pool reuses worker threads across tasks; without this
    reset, task N+1's logs would carry task N's IDs whenever the header
    wasn't set on N+1.
    """
    task_id_var.set(None)
    request_id_var.set(None)
