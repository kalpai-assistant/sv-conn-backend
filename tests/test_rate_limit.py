"""
Rate-limit dependency tests (SC-35 + SVC-159 headers).

Two layers:

1. Unit: `rate_limit()` factory behaviour with an AsyncMock redis — validates
   the counter/TTL logic, the RateLimitExceeded contract, and the
   X-RateLimit-* headers written to the Response object (SVC-159).

2. Integration: a small in-memory FakeAsyncRedis wired via
   dependency_overrides — validates the HTTP surface: 429 status,
   Retry-After header, X-RateLimit-* headers on both within-limit AND
   over-limit responses, and that the limiter runs BEFORE the endpoint
   handler.

The auth OTP path has its own counter (predates SC-35). Its Retry-After
behaviour is covered in the auth tests; only the RateLimitExceeded contract
is duplicated here.
"""

import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, Response, status
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.dependencies import get_current_user, get_redis
from app.exceptions import RateLimitExceeded, register_exception_handlers
from app.middleware.rate_limit import RateLimitMiddleware
from app.utils.rate_limiter import rate_limit


def _mock_response() -> Response:
    """Minimal FastAPI Response with a headers dict — enough for the
    limiter to write into."""
    return Response()


# ---------------------------------------------------------------------------
# Unit tests — hit the dependency function directly with mocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_hit_sets_expire():
    """First hit in a window: INCR returns 1, EXPIRE is called once with the window."""
    redis = AsyncMock()
    redis.incr.return_value = 1
    redis.ttl.return_value = 60
    user = AsyncMock()
    user.id = uuid.uuid4()
    response = _mock_response()

    dep = rate_limit("test_ns", max_requests=5, window_seconds=60)
    await dep(response=response, current_user=user, redis=redis)

    redis.incr.assert_awaited_once_with(f"rl:test_ns:{user.id}")
    redis.expire.assert_awaited_once_with(f"rl:test_ns:{user.id}", 60)


@pytest.mark.asyncio
async def test_within_limit_does_not_call_expire():
    """Hits 2..N don't reset the window — EXPIRE only fires on the first hit."""
    redis = AsyncMock()
    redis.incr.return_value = 3
    redis.ttl.return_value = 45
    user = AsyncMock()
    user.id = uuid.uuid4()
    response = _mock_response()

    dep = rate_limit("test_ns", max_requests=5, window_seconds=60)
    await dep(response=response, current_user=user, redis=redis)

    redis.expire.assert_not_called()


@pytest.mark.asyncio
async def test_within_limit_writes_headers():
    """SVC-159: X-RateLimit-Limit / -Remaining / -Reset must be set on the
    response object for within-limit hits so clients can see how close they
    are to the ceiling BEFORE hitting 429."""
    redis = AsyncMock()
    redis.incr.return_value = 3  # 3rd hit out of 5
    redis.ttl.return_value = 45
    user = AsyncMock()
    user.id = uuid.uuid4()
    response = _mock_response()

    dep = rate_limit("test_ns", max_requests=5, window_seconds=60)
    now = int(time.time())
    await dep(response=response, current_user=user, redis=redis)

    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "2"  # 5 - 3
    # Reset is now + TTL. Allow a 2-second window for test scheduling jitter.
    reset = int(response.headers["X-RateLimit-Reset"])
    assert now + 43 <= reset <= now + 47


@pytest.mark.asyncio
async def test_over_limit_raises_with_ttl_as_retry_after():
    """Counter > max → RateLimitExceeded carries the current key TTL + policy
    context (SVC-159) so the handler can emit consistent X-RateLimit-* headers
    on the 429 response."""
    redis = AsyncMock()
    redis.incr.return_value = 6  # over max=5
    redis.ttl.return_value = 42
    user = AsyncMock()
    user.id = uuid.uuid4()
    response = _mock_response()

    dep = rate_limit("test_ns", max_requests=5, window_seconds=60)
    with pytest.raises(RateLimitExceeded) as exc_info:
        await dep(response=response, current_user=user, redis=redis)

    assert exc_info.value.retry_after == 42
    assert exc_info.value.limit == 5
    assert exc_info.value.reset_at is not None
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_over_limit_with_missing_ttl_falls_back_to_window():
    """Key vanished between INCR and TTL (rare race) → fallback to the full
    window rather than 0 or a negative retry_after."""
    redis = AsyncMock()
    redis.incr.return_value = 11
    redis.ttl.return_value = -2  # key doesn't exist
    user = AsyncMock()
    user.id = uuid.uuid4()
    response = _mock_response()

    dep = rate_limit("test_ns", max_requests=10, window_seconds=3600)
    with pytest.raises(RateLimitExceeded) as exc_info:
        await dep(response=response, current_user=user, redis=redis)

    assert exc_info.value.retry_after == 3600
    assert exc_info.value.limit == 10


# ---------------------------------------------------------------------------
# Integration test — real FastAPI app with fake Redis
# ---------------------------------------------------------------------------


class _FakeAsyncRedis:
    """Minimal in-memory async Redis stub covering the rate limiter's needs."""

    def __init__(self):
        self._store: dict[str, int] = {}
        self._ttl: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self._store[key] = self._store.get(key, 0) + 1
        return self._store[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self._ttl[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self._ttl.get(key, -2)


def _build_app(fake_user, fake_redis):
    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_redis] = lambda: fake_redis
    return app


@pytest.mark.asyncio
async def test_endpoint_returns_429_with_retry_after_header():
    """End-to-end: after `max` allowed requests, the (max+1)th returns 429
    with a `Retry-After` header AND the same X-RateLimit-* triplet. Also
    verifies the endpoint body never runs on the over-limit request."""
    fake_user = AsyncMock()
    fake_user.id = uuid.uuid4()
    fake_redis = _FakeAsyncRedis()
    app = _build_app(fake_user, fake_redis)

    handler_calls = 0

    @app.post(
        "/limited",
        dependencies=[Depends(rate_limit("test_endpoint", max_requests=3, window_seconds=120))],
    )
    async def limited():
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(3):
            r = await client.post("/limited")
            assert r.status_code == 200, r.text
        # 4th call — over the limit
        r = await client.post("/limited")
        assert r.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert r.headers.get("Retry-After") == "120"
        # SVC-159: same policy visible on 429 as on 200.
        assert r.headers.get("X-RateLimit-Limit") == "3"
        assert r.headers.get("X-RateLimit-Remaining") == "0"
        assert r.headers.get("X-RateLimit-Reset") is not None
        assert r.json()["message"] == "Too many requests"

    # Endpoint handler ran exactly 3 times — over-limit never touched it.
    assert handler_calls == 3


# ---------------------------------------------------------------------------
# SVC-185 (G12) — webhook paths are exempt from the per-IP middleware limiter
# ---------------------------------------------------------------------------


def test_webhook_paths_are_exempt_from_per_ip_middleware_limiter():
    """SVC-185 (G12): Razorpay webhooks must not share a per-IP bucket with
    browsing traffic. If either 429s, Razorpay reads a non-2xx as a delivery
    failure and re-sends on its full retry schedule — the collect leg has no
    reconciliation backstop, so a dropped ``payment.captured`` is lost.

    Structural pin: the two webhook paths appear in the middleware's SKIP
    set. Semantic verification (hammer the endpoint 700+ times, expect no
    429) is what the middleware guarantees follows from this — pinning the
    membership is enough to catch the regression cheaply.
    """
    from app.middleware.rate_limit import _SKIP_PATHS

    assert "/api/v1/payments/webhook" in _SKIP_PATHS
    assert "/api/v1/payments/payout-webhook" in _SKIP_PATHS


@pytest.mark.asyncio
async def test_within_limit_response_carries_rate_headers():
    """SVC-159 core: a 200 response includes X-RateLimit-* so clients can
    read where they stand without waiting for a 429. Also checks Remaining
    decrements across successive hits."""
    fake_user = AsyncMock()
    fake_user.id = uuid.uuid4()
    fake_redis = _FakeAsyncRedis()
    app = _build_app(fake_user, fake_redis)

    @app.post(
        "/limited",
        dependencies=[Depends(rate_limit("test_endpoint2", max_requests=5, window_seconds=60))],
    )
    async def limited():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post("/limited")
        r2 = await client.post("/limited")

    assert r1.headers["X-RateLimit-Limit"] == "5"
    assert r1.headers["X-RateLimit-Remaining"] == "4"

    assert r2.headers["X-RateLimit-Limit"] == "5"
    assert r2.headers["X-RateLimit-Remaining"] == "3"

    # Reset is Unix seconds — must be in the future, within a minute or two.
    now = int(time.time())
    for r in (r1, r2):
        reset = int(r.headers["X-RateLimit-Reset"])
        assert now <= reset <= now + 60 + 5  # allow small jitter


# ---------------------------------------------------------------------------
# Middleware: behaviour when Redis itself is unavailable
# ---------------------------------------------------------------------------


class _BrokenRedis:
    """Every command fails, the way Upstash behaves past its request quota."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def incr(self, key: str) -> int:
        raise self._exc

    async def expire(self, key: str, seconds: int) -> bool:
        raise self._exc


def _build_middleware_app(redis, rpm: int = 60) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RateLimitMiddleware, redis=redis, requests_per_minute=rpm)

    @app.get("/api/v1/categories")
    async def _categories():
        return [{"slug": "plumber"}]

    return app


@pytest.mark.parametrize(
    "exc",
    [
        # The 2026-08-24 beta outage: Upstash rejects commands past the plan's
        # request quota. A ResponseError, not a connection failure — so
        # catching only ConnectionError would not have helped.
        ResponseError("max requests limit exceeded. Limit: 500000, Usage: 500000."),
        RedisConnectionError("Connection refused"),
        RedisTimeoutError("Timeout reading from socket"),
    ],
    ids=["quota_exhausted", "unreachable", "timeout"],
)
@pytest.mark.asyncio
async def test_middleware_fails_open_when_redis_is_unavailable(exc):
    """A dead limiter must not take the API down with it.

    This middleware runs on every request before routing, so an exception
    here 500s endpoints that never touch Redis. That is exactly what happened
    on 2026-08-24: the Upstash quota ran out and the entire backend — the
    catalogue, bookings, profile — returned 500 for ~13 hours. Rate limiting
    is a protection measure, not a correctness one; losing it briefly beats
    losing the service.
    """
    app = _build_middleware_app(_BrokenRedis(exc))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/v1/categories")

    assert response.status_code == 200, (
        f"{type(exc).__name__} from the limiter must not fail the request; "
        f"got {response.status_code}"
    )
    assert response.json() == [{"slug": "plumber"}]


@pytest.mark.asyncio
async def test_middleware_still_counts_when_redis_is_healthy():
    """The fail-open path must not swallow a working limiter's counting.

    Mutation guard: deleting the `incr` call entirely would still pass the
    fail-open test above, so pin that a healthy Redis is actually driven.
    """
    calls: list[str] = []

    class _CountingRedis:
        async def incr(self, key: str) -> int:
            calls.append(key)
            return len(calls)

        async def expire(self, key: str, seconds: int) -> bool:
            return True

    app = _build_middleware_app(_CountingRedis())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for _ in range(3):
            assert (await client.get("/api/v1/categories")).status_code == 200

    assert len(calls) == 3, "healthy Redis should still be incremented once per request"
    assert all(c.endswith(":/api/v1/categories") for c in calls)
