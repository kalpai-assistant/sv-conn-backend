"""
Structured logging setup (SVC-46).

Bridges stdlib `logging` → structlog so existing `logger = logging.getLogger(...)`
call sites keep working, while every record picks up the current `request_id`
(from a contextvar bound by `RequestIDMiddleware`) plus a JSON renderer in
staging/prod. Dev keeps the pretty console renderer for eyeballing locally.

Why contextvar (not `extra=`): a request-scoped ID that flows through every
service/util log call without threading `request_id` through every function
signature. Async-safe (contextvars propagate across `await`).
"""
from __future__ import annotations

import contextvars
import logging
import sys
from typing import Any

import structlog

# Request-scoped correlation ID. Set by RequestIDMiddleware, read by the
# structlog processor below. Default None so logs from startup / background
# tasks don't crash — they just omit the field.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def _add_request_id(_logger: Any, _name: str, event_dict: dict) -> dict:
    rid = request_id_var.get()
    if rid is not None:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging(*, level: str = "INFO", json_logs: bool = False) -> None:
    """Wire stdlib logging → structlog. Idempotent — safe to call twice."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_request_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    # structlog-native loggers.
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # stdlib loggers routed through the same processor chain so existing
    # `logging.getLogger(__name__)` call sites emit the same structured records.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # Quiet the noisiest access log — uvicorn re-emits every request; the
    # structlog record from our middleware is enough.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
