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
import re
import sys
from typing import Any

import structlog

# Request-scoped correlation ID. Set by RequestIDMiddleware in the API path
# and by the Celery task_prerun signal in workers (propagated via task
# headers). Default None so logs from startup / background init don't crash —
# they just omit the field.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# Task-scoped correlation ID. Set by the Celery task_prerun signal, unset
# after task_postrun. Independent from request_id so a task triggered by an
# HTTP request carries BOTH (request_id shows the originator; task_id shows
# which specific worker run emitted the log).
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "task_id", default=None
)


def _add_correlation_ids(_logger: Any, _name: str, event_dict: dict) -> dict:
    """Attach any set correlation IDs to every log record.

    Both fields are contextvar-backed so they flow through `await` chains
    and Celery's per-task context without needing to be threaded through
    call signatures.
    """
    rid = request_id_var.get()
    if rid is not None:
        event_dict["request_id"] = rid
    tid = task_id_var.get()
    if tid is not None:
        event_dict["task_id"] = tid
    return event_dict


# ---------------------------------------------------------------------------
# Secret redaction (SC-50 preventive control)
#
# The primary defense against secret leaks is code review + not writing them
# into log calls in the first place. This is a safety net: if a well-meaning
# `logger.info("payload=%s", payload)` includes a token or the audit key,
# the raw value never lands in aggregate logs (CloudWatch / Loki / Sentry).
#
# Two mechanisms, both cheap:
#   1. **Key-based**: any event_dict entry whose key matches a sensitive
#      name (case-insensitive substring — `otp`, `token`, `password`, etc)
#      gets its value replaced.
#   2. **Value-based**: string values matching a known-secret pattern (JWT,
#      Razorpay live key) get replaced regardless of their key.
#
# Redaction preserves the field so structure is unchanged — a reader still
# sees the key existed, just not the value.
# ---------------------------------------------------------------------------

_REDACTED = "***REDACTED***"

# Substrings that mark a key as sensitive. Match is case-insensitive; matches
# anywhere in the key name so `access_token`, `authTokenHash`, `RAZORPAY_SECRET`
# all get caught.
_SENSITIVE_KEY_SUBSTRINGS = frozenset({
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "otp",
    "encryption_key",
    "webhook_secret",
    # Razorpay signature — proves payment authenticity, mustn't leak.
    "razorpay_signature",
})

# Value patterns that MUST be redacted regardless of key. Ordered by frequency
# so cheap ones run first — the walk is called per log record.
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JWT: base64url segments separated by two dots. The `eyJ` prefix is the
    # base64-encoded `{"` that starts every JWT header — cheap first-char match.
    re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"),
    # Razorpay live key prefix. `rzp_test_` is fine to log; `rzp_live_` is not.
    re.compile(r"^rzp_live_[A-Za-z0-9]+$"),
    # AWS access key format: AKIA followed by 16 uppercase alphanumeric.
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
)


def _key_is_sensitive(key: str) -> bool:
    """True iff the key name contains any known sensitive substring."""
    key_lower = key.lower()
    return any(needle in key_lower for needle in _SENSITIVE_KEY_SUBSTRINGS)


def _value_is_sensitive(value: Any) -> bool:
    """True iff the value looks like a known secret pattern."""
    if not isinstance(value, str):
        return False
    return any(pat.match(value) for pat in _SENSITIVE_VALUE_PATTERNS)


def _redact(value: Any) -> Any:
    """Redact a value while preserving structure.

    Dicts + lists get walked recursively so a nested `{"headers":
    {"Authorization": "Bearer ..."}}` is caught. Scalars matching a
    sensitive pattern get replaced; everything else passes through.
    """
    if isinstance(value, dict):
        return {k: _REDACTED if _key_is_sensitive(k) else _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v) for v in value)
    if _value_is_sensitive(value):
        return _REDACTED
    return value


def _redact_secrets(_logger: Any, _name: str, event_dict: dict) -> dict:
    """Structlog processor: redact any key-based or value-based secret.

    Runs after correlation IDs are attached and before the renderer — so
    the redaction applies to every log destination (JSON, console, Sentry).
    """
    return {
        k: _REDACTED if _key_is_sensitive(k) else _redact(v)
        for k, v in event_dict.items()
    }


def configure_logging(*, level: str = "INFO", json_logs: bool = False) -> None:
    """Wire stdlib logging → structlog. Idempotent — safe to call twice."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_correlation_ids,
        # Redaction runs LAST among the transform processors so it catches
        # any secret injected by earlier processors too — not just call-site
        # kwargs. Runs before the renderer, so JSON + console + Sentry all
        # see the redacted form.
        _redact_secrets,
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
