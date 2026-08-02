"""SVC-161 — POST /downloads/sms-link tests.

Public endpoint (no auth). Two things matter:

1. **The feature-flag gate.** Default OFF because sending a URL through
   MSG91's OTP template produces user-visible garbage until a dedicated
   template is approved. Test that OFF returns 501 and ON reaches the
   SMS client.
2. **The rate limit.** IP-based, 3/hour. Test that the 4th request
   from the same "IP" (mocked via header) gets 429.

Standalone-style — the endpoint doesn't touch the DB, so we build a
minimal FastAPI app instead of using the full `client` fixture. Keeps
the tests fast and avoids cross-test coupling.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.config import get_settings
from app.dependencies import get_redis
from app.exceptions import register_exception_handlers
from app.routers.downloads import router


@pytest.fixture
async def redis_client():
    """Per-test Redis client — bypasses the module-global for the same
    event-loop-close reason documented in conftest.py."""
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        # Clear any rate-limit keys from previous tests.
        keys = await client.keys("rl:ip:download_sms:*")
        if keys:
            await client.delete(*keys)
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def app(redis_client):
    a = FastAPI()
    a.include_router(router, prefix="/api/v1")
    a.dependency_overrides[get_redis] = lambda: redis_client
    # RateLimitExceeded → 429 handler wiring; without this, the exception
    # bubbles up as a 500 and the rate-limit test can't assert 429.
    register_exception_handlers(a)
    return a


async def _post(app, body: dict, headers: dict | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.post("/api/v1/downloads/sms-link", json=body, headers=headers)


# ---------------------------------------------------------------------------
# Feature flag gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sms_link_returns_501_when_flag_off(app, monkeypatch):
    """Default flag state — endpoint refuses cleanly, doesn't call SMS."""
    monkeypatch.setattr(get_settings(), "SMS_DOWNLOAD_LINK_ENABLED", False)

    fake_sms = AsyncMock()
    with patch("app.routers.downloads.get_sms_client", return_value=fake_sms):
        r = await _post(app, {"phone": "+919876543210"})

    assert r.status_code == 501, r.text
    fake_sms.send_otp.assert_not_called()


@pytest.mark.asyncio
async def test_sms_link_fires_sms_when_flag_on(app, monkeypatch):
    """Flag on → SMS client called with the download URL."""
    monkeypatch.setattr(get_settings(), "SMS_DOWNLOAD_LINK_ENABLED", True)

    fake_sms = AsyncMock()
    with patch("app.routers.downloads.get_sms_client", return_value=fake_sms):
        r = await _post(
            app,
            {"phone": "+919876543210"},
            headers={"X-Forwarded-For": "203.0.113.1"},  # unique IP so no rate limit
        )

    assert r.status_code == 204, r.text
    fake_sms.send_otp.assert_awaited_once_with("+919876543210", "https://eazyserv.com/download")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_phone",
    [
        "9876543210",  # no +91 prefix
        "+9199999999",  # only 9 digits
        "+91199999999",  # starts with 1 (invalid Indian mobile prefix)
        "+15551234567",  # US format
        "not-a-phone",
    ],
)
async def test_sms_link_rejects_invalid_phone(app, bad_phone):
    """Every non-E.164-India format fails validation before hitting SMS."""
    fake_sms = AsyncMock()
    with patch("app.routers.downloads.get_sms_client", return_value=fake_sms):
        r = await _post(app, {"phone": bad_phone})

    assert r.status_code == 422, r.text
    fake_sms.send_otp.assert_not_called()


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sms_link_rate_limits_by_ip(app, monkeypatch):
    """4th request from the same IP within the window → 429."""
    monkeypatch.setattr(get_settings(), "SMS_DOWNLOAD_LINK_ENABLED", True)

    ip_header = {"X-Forwarded-For": "203.0.113.42"}
    fake_sms = AsyncMock()

    with patch("app.routers.downloads.get_sms_client", return_value=fake_sms):
        # First 3 succeed
        for _ in range(3):
            r = await _post(app, {"phone": "+919876543210"}, headers=ip_header)
            assert r.status_code == 204, r.text

        # 4th is rate-limited
        r4 = await _post(app, {"phone": "+919876543210"}, headers=ip_header)
        assert r4.status_code == 429, r4.text
        # Retry-After should be set (from Redis TTL) — not asserting exact
        # value since the TTL depends on when the window opened.
        assert "retry_after" in r4.json() or "message" in r4.json()


@pytest.mark.asyncio
async def test_sms_link_rate_limit_is_per_ip(app, monkeypatch):
    """Different IPs get independent counters."""
    monkeypatch.setattr(get_settings(), "SMS_DOWNLOAD_LINK_ENABLED", True)

    fake_sms = AsyncMock()
    with patch("app.routers.downloads.get_sms_client", return_value=fake_sms):
        # Exhaust IP A
        for _ in range(3):
            await _post(
                app,
                {"phone": "+919876543210"},
                headers={"X-Forwarded-For": "203.0.113.10"},
            )

        # IP B still succeeds
        r = await _post(
            app,
            {"phone": "+919876543211"},
            headers={"X-Forwarded-For": "203.0.113.11"},
        )
        assert r.status_code == 204, r.text
