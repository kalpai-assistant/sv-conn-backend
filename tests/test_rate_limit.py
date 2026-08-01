"""
Rate-limit dependency tests (SC-35).

Two layers:

1. Unit: `rate_limit()` factory behaviour with an AsyncMock redis — validates
   the counter/TTL logic and the RateLimitExceeded contract in isolation.

2. Integration: a small in-memory FakeAsyncRedis wired via
   dependency_overrides — validates the HTTP surface: 429 status, `Retry-After`
   header, and that the limiter runs BEFORE the endpoint handler (over-limit
   requests never touch the DB).

The auth OTP path has its own counter (predates SC-35). Its Retry-After
behaviour is covered in the auth tests; only the RateLimitExceeded contract
is duplicated here.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, status
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user, get_redis
from app.exceptions import RateLimitExceeded, register_exception_handlers
from app.utils.rate_limiter import rate_limit

# ---------------------------------------------------------------------------
# Unit tests — hit the dependency function directly with mocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_hit_sets_expire():
    """First hit in a window: INCR returns 1, EXPIRE is called once with the window."""
    redis = AsyncMock()
    redis.incr.return_value = 1
    user = AsyncMock()
    user.id = uuid.uuid4()

    dep = rate_limit("test_ns", max_requests=5, window_seconds=60)
    await dep(current_user=user, redis=redis)

    redis.incr.assert_awaited_once_with(f"rl:test_ns:{user.id}")
    redis.expire.assert_awaited_once_with(f"rl:test_ns:{user.id}", 60)


@pytest.mark.asyncio
async def test_within_limit_does_not_call_expire():
    """Hits 2..N don't reset the window — EXPIRE only fires on the first hit."""
    redis = AsyncMock()
    redis.incr.return_value = 3
    user = AsyncMock()
    user.id = uuid.uuid4()

    dep = rate_limit("test_ns", max_requests=5, window_seconds=60)
    await dep(current_user=user, redis=redis)

    redis.expire.assert_not_called()


@pytest.mark.asyncio
async def test_over_limit_raises_with_ttl_as_retry_after():
    """Counter > max → RateLimitExceeded carries the current key TTL."""
    redis = AsyncMock()
    redis.incr.return_value = 6  # over max=5
    redis.ttl.return_value = 42
    user = AsyncMock()
    user.id = uuid.uuid4()

    dep = rate_limit("test_ns", max_requests=5, window_seconds=60)
    with pytest.raises(RateLimitExceeded) as exc_info:
        await dep(current_user=user, redis=redis)

    assert exc_info.value.retry_after == 42
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_over_limit_with_missing_ttl_defaults_to_60():
    """Key vanished between INCR and TTL (rare race) → fallback of 60s."""
    redis = AsyncMock()
    redis.incr.return_value = 11
    redis.ttl.return_value = -2  # key doesn't exist
    user = AsyncMock()
    user.id = uuid.uuid4()

    dep = rate_limit("test_ns", max_requests=10, window_seconds=3600)
    with pytest.raises(RateLimitExceeded) as exc_info:
        await dep(current_user=user, redis=redis)

    assert exc_info.value.retry_after == 60


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


@pytest.mark.asyncio
async def test_endpoint_returns_429_with_retry_after_header():
    """
    End-to-end: after `max` allowed requests, the (max+1)th returns 429 with
    a `Retry-After` header. Also verifies the endpoint body never runs on the
    over-limit request.
    """
    app = FastAPI()
    register_exception_handlers(app)

    handler_calls = 0

    def fake_user():
        user = AsyncMock()
        user.id = uuid.uuid4()
        return user

    fake_user_instance = fake_user()
    fake_redis = _FakeAsyncRedis()

    app.dependency_overrides[get_current_user] = lambda: fake_user_instance
    app.dependency_overrides[get_redis] = lambda: fake_redis

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
        assert r.json()["message"] == "Too many requests"

    # Endpoint handler ran exactly 3 times — over-limit never touched it.
    assert handler_calls == 3
