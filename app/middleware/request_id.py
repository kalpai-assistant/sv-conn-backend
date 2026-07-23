"""
Request ID middleware: injects a unique X-Request-ID on every request/response.
Enables distributed tracing and log correlation.

SVC-46: also binds the ID into a contextvar so every structlog record emitted
during the request carries it automatically — no need to pass request_id
through every service call.
"""
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request

from app.logging_config import request_id_var

_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    If the client provides X-Request-ID, propagate it.
    Otherwise, generate a fresh UUID v4.
    The ID is attached to request state, response headers, and the
    request_id contextvar (picked up by structlog).
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[_HEADER] = request_id
        return response
