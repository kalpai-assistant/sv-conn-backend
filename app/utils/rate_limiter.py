"""
Per-key rate-limit dependency (SC-35).

Redis-backed fixed-window counter. Independent of the global IP-based
middleware — this is per-user, per-endpoint. Two live policies:

- Booking creation: 10 / consumer / hour
- Payment endpoints: 20 / user / hour

The existing OTP path uses its own counter (`app.utils.otp.is_rate_limited`)
and predates this module. Both raise the same `RateLimitExceeded` exception
so the `Retry-After` header behaviour is consistent.

Fixed-window rather than sliding-window because Redis `INCR + EXPIRE` is one
round-trip and the burst pattern this creates at window boundaries is fine
at these limits — nobody notices whether "10/hour" is a rolling hour or a
calendar hour when the window is short.
"""

import time

from fastapi import Depends, Request, Response
from redis.asyncio import Redis

from app.dependencies import get_current_user, get_redis
from app.exceptions import RateLimitExceeded
from app.models.user import User


def _write_rate_headers(response: Response, limit: int, remaining: int, reset_at: int) -> None:
    """Attach the standard X-RateLimit-* triplet to a within-limit response
    (SVC-159). Uses the classic X-RateLimit-Limit / -Remaining / -Reset
    form (absolute Unix-seconds reset) rather than the IETF draft's
    single-header `RateLimit`. Nearly all client tooling speaks the classic
    form and the draft is still non-final."""
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
    response.headers["X-RateLimit-Reset"] = str(reset_at)


def ip_rate_limit(namespace: str, max_requests: int, window_seconds: int = 3600):
    """Public-endpoint rate limit — keyed by client IP, not user.

    Same shape as ``rate_limit`` but for endpoints without auth (SMS
    download-link, waitlist signups, etc). Uses ``request.client.host``
    as the key; when behind a load balancer that sets X-Forwarded-For,
    the ALB / CloudFront forwards a chain — the leftmost is the real
    client. The global ``RateLimitMiddleware`` (600/min) still applies
    as a floor; this is a much tighter limit for specific endpoints.

    Bots + shared-NAT edge case: users behind the same corporate NAT
    share an IP. 3/hour per NAT is fine for our use cases (SMS link
    sends, waitlist signups); anything tighter would break real users.
    """

    async def dependency(
        request: Request,
        response: Response,
        redis: Redis = Depends(get_redis),
    ) -> None:
        client_ip = _extract_client_ip(request)
        key = f"rl:ip:{namespace}:{client_ip}"

        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, window_seconds)

        ttl = await redis.ttl(key)
        # ttl can be -2 (key gone between INCR and TTL) or -1 (no expiry set —
        # shouldn't happen after the EXPIRE above, but defensive). Both fall
        # back to the full window as a conservative reset estimate.
        effective_ttl = ttl if ttl > 0 else window_seconds
        reset_at = int(time.time()) + effective_ttl

        if current > max_requests:
            raise RateLimitExceeded(
                retry_after=effective_ttl,
                limit=max_requests,
                reset_at=reset_at,
            )

        _write_rate_headers(
            response,
            limit=max_requests,
            remaining=max_requests - current,
            reset_at=reset_at,
        )

    return dependency


def _extract_client_ip(request: Request) -> str:
    """Best-effort client IP. Trusts X-Forwarded-For's leftmost only if
    the immediate socket peer is the ALB (in prod). In dev / local, the
    socket peer is the real client and we use that."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Leftmost = origin client; rightmost = trusted proxy.
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(namespace: str, max_requests: int, window_seconds: int = 3600):
    """
    Build a FastAPI dependency that limits `max_requests` per authenticated
    user in each `window_seconds` window, keyed by `namespace`.

    Attach via:

        @router.post("/", dependencies=[Depends(rate_limit("booking_create", 10))])

    On over-limit, raises `RateLimitExceeded` with `retry_after` set from the
    Redis key's remaining TTL — so clients get an accurate wait time rather
    than the full policy window.
    """

    async def dependency(
        response: Response,
        current_user: User = Depends(get_current_user),
        redis: Redis = Depends(get_redis),
    ) -> None:
        key = f"rl:{namespace}:{current_user.id}"

        # INCR is atomic; EXPIRE only lands on the first hit of a fresh window
        # (when INCR returned 1). Race-safe: two concurrent first-hits both see
        # `current == 1` and both call EXPIRE with the same TTL — idempotent.
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, window_seconds)

        ttl = await redis.ttl(key)
        # ttl < 1 = key vanished between INCR and TTL (rare race) or no expiry
        # was ever set (defensive — EXPIRE above should have handled it).
        # Fall back to the full window as a conservative reset estimate.
        effective_ttl = ttl if ttl > 0 else window_seconds
        reset_at = int(time.time()) + effective_ttl

        if current > max_requests:
            raise RateLimitExceeded(
                retry_after=effective_ttl,
                limit=max_requests,
                reset_at=reset_at,
            )

        _write_rate_headers(
            response,
            limit=max_requests,
            remaining=max_requests - current,
            reset_at=reset_at,
        )

    return dependency
