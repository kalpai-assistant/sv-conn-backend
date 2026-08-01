"""
Health probe tests (SVC-48).

Two endpoints, two contracts:

- `/health` is dumb — 200 no matter what, no dependencies touched.
- `/healthz` pings DB + Redis with a tight timeout; returns 200 with per-
  dependency status when all green, 503 when any fail.

Deliberately standalone from the shared `client` fixture — that fixture builds
the whole app + hits a real DB, which is overkill (and brittle) for testing
the health router. Each test builds a minimal FastAPI app with the health
router mounted and dependencies mocked to the exact behaviour under test.
"""

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_db, get_redis
from app.routers import health


class _FakeRedis:
    """Minimal async Redis stub for the health check."""

    def __init__(self, *, ping_result=True, ping_raises: Exception | None = None):
        self._result = ping_result
        self._raises = ping_raises

    async def ping(self):
        if self._raises:
            raise self._raises
        return self._result


class _FakeSession:
    """Minimal AsyncSession stub — only implements what `_ping_database` uses."""

    def __init__(self, *, raises: Exception | None = None, sleep: float = 0):
        self._raises = raises
        self._sleep = sleep

    async def execute(self, _stmt):
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raises:
            raise self._raises
        return None  # readiness check doesn't inspect the result


def _build_app(*, db=None, redis=None) -> FastAPI:
    """Fresh app with health router mounted + overridable dependencies."""
    app = FastAPI()
    app.include_router(health.router)
    app.dependency_overrides[get_db] = lambda: (db or _FakeSession())
    app.dependency_overrides[get_redis] = lambda: (redis or _FakeRedis())
    return app


async def _get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get(path)


# ---------------------------------------------------------------------------
# /health — liveness (never fails, no deps touched)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liveness_always_ok():
    r = await _get(_build_app(), "/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /healthz — readiness (real dependency pings)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_all_ok():
    """DB + Redis both respond → 200 with per-check status."""
    r = await _get(_build_app(), "/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "redis": "ok"}
    assert "timestamp" in body


@pytest.mark.asyncio
async def test_readiness_redis_down_returns_503():
    """Redis unreachable → 503, body still enumerates each check."""
    app = _build_app(redis=_FakeRedis(ping_raises=ConnectionError("redis unreachable")))
    r = await _get(app, "/healthz")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"].startswith("error:ConnectionError")


@pytest.mark.asyncio
async def test_readiness_database_down_returns_503():
    """DB unreachable → 503, redis reported as ok."""
    app = _build_app(db=_FakeSession(raises=RuntimeError("db unreachable")))
    r = await _get(app, "/healthz")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["database"].startswith("error:RuntimeError")


@pytest.mark.asyncio
async def test_readiness_both_down_reports_both():
    """When both are down, both are enumerated (not short-circuit)."""
    app = _build_app(
        db=_FakeSession(raises=RuntimeError("db")),
        redis=_FakeRedis(ping_raises=ConnectionError("redis")),
    )
    r = await _get(app, "/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["database"].startswith("error:")
    assert body["checks"]["redis"].startswith("error:")


@pytest.mark.asyncio
async def test_readiness_redis_ping_returns_unexpected_value():
    """A truthy-but-wrong redis ping is treated as failure (defensive)."""
    app = _build_app(redis=_FakeRedis(ping_result="unexpected"))
    r = await _get(app, "/healthz")

    assert r.status_code == 503
    assert r.json()["checks"]["redis"].startswith("error:RuntimeError")


@pytest.mark.asyncio
async def test_readiness_check_times_out():
    """A check that hangs beyond the timeout returns 'error:timeout', not 500."""
    original = health._CHECK_TIMEOUT_SECONDS
    health._CHECK_TIMEOUT_SECONDS = 0.1
    try:
        # DB sleeps longer than the timeout.
        app = _build_app(db=_FakeSession(sleep=5))
        r = await _get(app, "/healthz")
    finally:
        health._CHECK_TIMEOUT_SECONDS = original

    assert r.status_code == 503
    assert r.json()["checks"]["database"] == "error:timeout"


@pytest.mark.asyncio
async def test_readiness_error_message_is_bounded():
    """Long exception messages are truncated (~100 chars) — no PII flood in body."""
    long_msg = "x" * 500
    app = _build_app(redis=_FakeRedis(ping_raises=RuntimeError(long_msg)))
    r = await _get(app, "/healthz")

    # The check field is "error:RuntimeError:xxxx..." — first 100 chars of msg
    check = r.json()["checks"]["redis"]
    assert check.startswith("error:RuntimeError:")
    # Total length bounded: "error:RuntimeError:" (19) + 100 msg = 119ish
    assert len(check) <= 200
