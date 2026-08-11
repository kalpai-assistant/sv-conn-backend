"""
Passive JWT auth middleware.
Attaches decoded token to request.state for logging and observability.
Authorization enforcement happens in dependencies.py (get_current_user),
not here — this middleware never rejects requests.
"""

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.utils.security import decode_jwt

_AUTH_HEADER = "Authorization"
_BEARER_PREFIX = "Bearer "


class JWTAttachMiddleware(BaseHTTPMiddleware):
    """Decode JWT (if present) and attach to request.state.token_payload."""

    async def dispatch(self, request: Request, call_next):
        request.state.token_payload = None
        auth = request.headers.get(_AUTH_HEADER, "")
        if auth.startswith(_BEARER_PREFIX):
            token = auth[len(_BEARER_PREFIX) :]
            request.state.token_payload = decode_jwt(token)
        return await call_next(request)
