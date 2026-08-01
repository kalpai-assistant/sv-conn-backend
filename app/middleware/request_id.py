"""
Request ID middleware: injects a unique X-Request-ID on every request/response.
Enables distributed tracing and log correlation.

SVC-46: also binds the ID into a contextvar so every structlog record emitted
during the request carries it automatically — no need to pass request_id
through every service call.

SVC-47: also tags Sentry with the request_id so a Sentry issue cross-links
to the exact log lines via Better Stack search.
"""
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.logging_config import request_id_var

_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    If the client provides X-Request-ID, propagate it.
    Otherwise, generate a fresh UUID v4.
    The ID is attached to request state, response headers, the
    request_id contextvar (picked up by structlog), and the current
    Sentry scope (as a tag) — so a Sentry issue can be cross-referenced
    against Better Stack log lines by grepping the ID.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        _tag_sentry(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[_HEADER] = request_id
        return response


def _tag_sentry(request_id: str) -> None:
    """Attach request_id as a Sentry tag if the SDK is installed + initialised.

    Guarded because sentry_sdk is an optional runtime dep (SENTRY_DSN empty
    → SDK not initialised → set_tag is still a safe no-op, but the import
    itself is cheap). Kept out of hot path via defensive local import.
    """
    try:
        import sentry_sdk

        sentry_sdk.set_tag("request_id", request_id)
    except Exception:
        # Sentry issues should never break the request path.
        pass
