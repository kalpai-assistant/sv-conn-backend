"""
Redis-based sliding-window rate limiter middleware — exact from LLD.
"""

import logging

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# SVC-185 (G12): webhook paths are exempt too. Razorpay and RazorpayX deliver
# from a small, fixed set of source IPs all hitting the same path, so every
# webhook shares one counter — the limiter is a per-(IP, path) bucket. If it
# ever tripped, the endpoint would 429; Razorpay reads a non-2xx as a delivery
# failure and re-sends on its full retry schedule. The payout leg partly
# survives that via nightly reconciliation, but the collect leg does not — a
# dropped ``payment.captured`` is lost permanently. Signature verification
# already gates the endpoints (SVC-39 G3), so the per-IP quota adds nothing.
_SKIP_PATHS = frozenset(
    {
        "/health",
        "/healthz",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/api/v1/payments/webhook",
        "/api/v1/payments/payout-webhook",
    }
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limiter using Redis INCR + EXPIRE.
    Applied to all paths except health/docs endpoints.
    """

    def __init__(self, app, redis: Redis, requests_per_minute: int = 60):
        super().__init__(app)
        self.redis = redis
        self.rpm = requests_per_minute

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{client_ip}:{request.url.path}"

        # Fail open, not closed.
        #
        # This middleware runs on every request before routing, so any Redis
        # error here took down 100% of the API — including endpoints that
        # never touch Redis. On 2026-08-24 the Upstash request quota was
        # exhausted, INCR started returning an error, and every single
        # endpoint 500'd for ~13 hours: catalogue, bookings, profile, all of
        # it. The exception escaped to Starlette's ServerErrorMiddleware,
        # which sits above CORSMiddleware, so the 500s carried no CORS headers
        # either and browsers reported an opaque "Failed to fetch".
        #
        # Rate limiting is a protection measure, not a correctness one. Losing
        # it briefly is a far smaller problem than losing the whole service,
        # so an unreachable limiter now lets traffic through and says so
        # loudly. WARNING rather than ERROR because the request itself
        # succeeded — but it should still be alertable, since a limiter that
        # is silently open is a real exposure.
        try:
            current = await self.redis.incr(key)
            if current == 1:
                await self.redis.expire(key, 60)
        except RedisError as exc:
            logger.warning(
                "Rate limiter unavailable, allowing request through: %s: %s",
                type(exc).__name__,
                exc,
            )
            return await call_next(request)

        if current > self.rpm:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again in a minute.",
            )

        return await call_next(request)
