"""
Request ID middleware: injects a unique X-Request-ID on every request/response.
Enables distributed tracing and log correlation.
"""
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request

_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    If the client provides X-Request-ID, propagate it.
    Otherwise, generate a fresh UUID v4.
    The ID is attached to both request state and response headers.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers[_HEADER] = request_id
        return response
