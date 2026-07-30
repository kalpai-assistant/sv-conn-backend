"""
Redis-based sliding-window rate limiter middleware — exact from LLD.
"""
from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware

_SKIP_PATHS = frozenset({"/health", "/healthz", "/docs", "/openapi.json", "/redoc"})


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

        current = await self.redis.incr(key)
        if current == 1:
            await self.redis.expire(key, 60)

        if current > self.rpm:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again in a minute.",
            )

        return await call_next(request)
