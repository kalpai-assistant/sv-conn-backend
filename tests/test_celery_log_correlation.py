"""
Celery log-correlation tests (SVC-46 follow-up).

The signals are small but easy to get subtly wrong:

- A dropped `before_task_publish` header = worker logs orphaned from the
  request that triggered them.
- A missed `task_postrun` reset = task N's IDs leak into task N+1's logs
  once the pool reuses a worker thread — a nightmare to diagnose in
  aggregated logs.

Each behaviour is pinned with its own test rather than a single end-to-end,
so a regression tells you *which* boundary is broken.
"""
import io
import json
import logging
from types import SimpleNamespace

import pytest

from app.logging_config import (
    configure_logging,
    request_id_var,
    task_id_var,
)
from app.tasks.log_correlation import (
    _bind_correlation_ids,
    _propagate_request_id,
    _reset_correlation_ids,
)


@pytest.fixture(autouse=True)
def reset_contextvars():
    """Every test starts with both IDs unset; leaks between tests would mask bugs."""
    request_id_var.set(None)
    task_id_var.set(None)
    yield
    request_id_var.set(None)
    task_id_var.set(None)


# ---------------------------------------------------------------------------
# before_task_publish — the enqueuing side
# ---------------------------------------------------------------------------


def test_publish_stamps_current_request_id_onto_headers():
    """Inside an HTTP request → outgoing task carries that request_id."""
    request_id_var.set("req-abc-123")
    headers: dict = {}

    _propagate_request_id(headers=headers)

    assert headers == {"request_id": "req-abc-123"}


def test_publish_omits_header_when_no_active_request():
    """Beat / admin dispatch → no request_id → header not set (kept absent, not empty)."""
    headers: dict = {}

    _propagate_request_id(headers=headers)

    assert headers == {}


def test_publish_no_ops_when_headers_is_none():
    """Some Celery paths pass headers=None; must not crash."""
    request_id_var.set("req-xyz")
    _propagate_request_id(headers=None)  # no raise = pass


# ---------------------------------------------------------------------------
# task_prerun — the worker side
# ---------------------------------------------------------------------------


def _fake_task(request_id: str | None = None):
    """Minimal stand-in for a Celery Task with a `.request.get(key)` method."""
    return SimpleNamespace(
        request=SimpleNamespace(get=lambda key: request_id if key == "request_id" else None)
    )


def test_prerun_binds_task_id_unconditionally():
    """Every running task gets a task_id in its logs — even Beat-scheduled ones."""
    _bind_correlation_ids(task_id="task-uuid-1", task=_fake_task(request_id=None))

    assert task_id_var.get() == "task-uuid-1"
    assert request_id_var.get() is None


def test_prerun_binds_request_id_from_task_headers():
    """A task published with a request_id header runs with that ID bound."""
    _bind_correlation_ids(task_id="task-uuid-2", task=_fake_task(request_id="req-from-header"))

    assert task_id_var.get() == "task-uuid-2"
    assert request_id_var.get() == "req-from-header"


def test_prerun_leaves_request_id_unset_when_no_header():
    """No header → request_id stays None (Beat schedule doesn't fabricate an ID)."""
    _bind_correlation_ids(task_id="task-uuid-3", task=_fake_task(request_id=None))

    assert task_id_var.get() == "task-uuid-3"
    assert request_id_var.get() is None


# ---------------------------------------------------------------------------
# task_postrun — reset guarantees no leak between tasks in the same worker
# ---------------------------------------------------------------------------


def test_postrun_clears_both_ids():
    """After task completes, both contextvars must be None so task N+1 starts clean."""
    task_id_var.set("task-1")
    request_id_var.set("req-1")

    _reset_correlation_ids()

    assert task_id_var.get() is None
    assert request_id_var.get() is None


def test_prerun_postrun_isolates_two_sequential_tasks():
    """The end-to-end guarantee: task 2's context must not inherit task 1's IDs."""
    # Task 1 runs.
    _bind_correlation_ids(task_id="task-1", task=_fake_task(request_id="req-1"))
    assert request_id_var.get() == "req-1"
    _reset_correlation_ids()

    # Task 2 runs (Beat-scheduled, no request_id header).
    _bind_correlation_ids(task_id="task-2", task=_fake_task(request_id=None))
    assert task_id_var.get() == "task-2"
    # THIS is the assertion the reset exists for.
    assert request_id_var.get() is None


# ---------------------------------------------------------------------------
# End-to-end via structlog: correlation IDs show up in actual log records
# ---------------------------------------------------------------------------


def _capture_json_logs():
    """Reconfigure logging with a JSON renderer into an in-memory buffer.

    Same pattern as tests/test_logging_correlation.py — pytest's stdout
    capture doesn't play well with the StreamHandler(sys.stdout) that
    configure_logging installs at import time, so swap in a buffer-backed
    handler that keeps the same JSON formatter.
    """
    configure_logging(level="INFO", json_logs=True)
    buf = io.StringIO()
    root = logging.getLogger()
    orig = root.handlers[0]
    handler = logging.StreamHandler(buf)
    handler.setFormatter(orig.formatter)
    root.handlers[:] = [handler]

    def restore():
        root.handlers[:] = [orig]

    return buf, restore


def test_log_record_carries_task_id():
    """A record emitted inside task_prerun/postrun scope has task_id attached."""
    buf, restore = _capture_json_logs()
    try:
        _bind_correlation_ids(task_id="task-log-1", task=_fake_task(request_id="req-log-1"))
        logging.getLogger("test").info("inside task")
        _reset_correlation_ids()
    finally:
        restore()

    records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    ours = [r for r in records if r.get("event") == "inside task"]
    assert ours, f"log missing; captured: {buf.getvalue()!r}"
    assert ours[0]["task_id"] == "task-log-1"
    assert ours[0]["request_id"] == "req-log-1"


def test_log_record_outside_task_has_neither_id():
    """Startup / background logs (no task, no request) omit both fields cleanly."""
    buf, restore = _capture_json_logs()
    try:
        logging.getLogger("test").info("outside any task")
    finally:
        restore()

    records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    ours = [r for r in records if r.get("event") == "outside any task"]
    assert ours
    assert "task_id" not in ours[0]
    assert "request_id" not in ours[0]
