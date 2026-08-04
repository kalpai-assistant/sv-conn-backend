"""
Admin IP allowlist middleware tests (SVC-75).

Covers:
- No-op when the allowlist is empty (dev default)
- Non-admin paths pass through untouched
- Admin path from a listed IP allowed
- Admin path from an unlisted IP → 403
- CIDR match (subnet, not just single IPs)
- X-Forwarded-For only honoured when opted in (spoofing guard)
- Malformed CIDR entries are dropped, not crashing the app
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.admin_ip_allowlist import AdminIPAllowlistMiddleware, _parse_allowlist


def _build_app(**middleware_kwargs) -> FastAPI:
    """Bare app with the middleware attached + one route inside/outside /admin."""
    app = FastAPI()
    app.add_middleware(AdminIPAllowlistMiddleware, **middleware_kwargs)

    @app.get("/api/v1/admin/ping")
    async def admin_ping():
        return {"scope": "admin"}

    @app.get("/api/v1/bookings/")
    async def bookings():
        return {"scope": "public"}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


# ---------------------------------------------------------------------------
# _parse_allowlist — unit
# ---------------------------------------------------------------------------


def test_parse_allowlist_empty_returns_empty():
    assert _parse_allowlist("") == []
    assert _parse_allowlist("   ") == []
    assert _parse_allowlist(",,, ") == []


def test_parse_allowlist_accepts_cidr_and_bare_ips():
    result = _parse_allowlist("10.0.0.0/8, 203.0.113.4, 2001:db8::/32")
    assert len(result) == 3
    # Bare IPs are treated as /32 (IPv4) or /128 (IPv6).
    assert str(result[1]) == "203.0.113.4/32"


def test_parse_allowlist_drops_malformed_without_raising():
    """A typo shouldn't take the API down at startup — malformed entries drop."""
    result = _parse_allowlist("10.0.0.0/8, not-an-ip, 203.0.113.4")
    assert len(result) == 2  # bad entry dropped, the two valid ones survive


# ---------------------------------------------------------------------------
# Middleware — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_allowlist_is_no_op():
    """Dev-safe default: empty allowlist means no restriction on /admin/*."""
    app = _build_app(allowed_ips="", api_prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/v1/admin/ping")).status_code == 200
        assert (await c.get("/api/v1/bookings/")).status_code == 200


@pytest.mark.asyncio
async def test_non_admin_paths_never_checked_even_with_allowlist():
    """A public endpoint from an unlisted IP still succeeds."""
    app = _build_app(allowed_ips="10.99.99.99/32", api_prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/v1/bookings/")).status_code == 200
        assert (await c.get("/health")).status_code == 200


# ---------------------------------------------------------------------------
# Middleware — the actual gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_from_unlisted_ip_returns_403():
    """The whole point: unlisted IP can't reach /admin/*, even for the ping."""
    app = _build_app(allowed_ips="10.99.99.99/32", api_prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # ASGITransport reports client host as "127.0.0.1" by default — outside 10.99.99.99/32
        resp = await c.get("/api/v1/admin/ping")

    assert resp.status_code == 403
    # Deliberately generic — never leak which IPs are allowed.
    assert "restricted" in resp.json()["message"].lower()


@pytest.mark.asyncio
async def test_admin_from_listed_ip_allowed():
    """127.0.0.1/32 covers the ASGI test client's default host."""
    app = _build_app(allowed_ips="127.0.0.1/32", api_prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/admin/ping")

    assert resp.status_code == 200
    assert resp.json() == {"scope": "admin"}


@pytest.mark.asyncio
async def test_admin_cidr_subnet_match():
    """CIDR blocks match all IPs in the subnet — 127.0.0.1 falls inside 127.0.0.0/24."""
    app = _build_app(allowed_ips="127.0.0.0/24", api_prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/v1/admin/ping")).status_code == 200


# ---------------------------------------------------------------------------
# Forwarded-for handling — the spoofing concern
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forwarded_header_ignored_by_default():
    """Without trust_forwarded_for=True, X-Forwarded-For is ignored — a client
    can't fake being on an allowlisted IP by setting a header."""
    app = _build_app(
        allowed_ips="203.0.113.4/32",  # not the ASGI test host
        trust_forwarded_for=False,
        api_prefix="/api/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/admin/ping",
            headers={"X-Forwarded-For": "203.0.113.4"},  # spoofing attempt
        )
    # Blocked — real client IP (test host) isn't in the list; the spoofed header is ignored.
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_forwarded_header_honoured_when_opted_in():
    """Behind an ALB/CDN the real client IP arrives in X-Forwarded-For."""
    app = _build_app(
        allowed_ips="203.0.113.4/32",
        trust_forwarded_for=True,
        api_prefix="/api/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/admin/ping",
            headers={"X-Forwarded-For": "203.0.113.4"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_forwarded_header_uses_leftmost_client():
    """X-Forwarded-For: client, proxy1, proxy2 — client is leftmost."""
    app = _build_app(
        allowed_ips="203.0.113.4/32",
        trust_forwarded_for=True,
        api_prefix="/api/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/admin/ping",
            headers={"X-Forwarded-For": "203.0.113.4, 10.0.0.1, 10.0.0.2"},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Boot-time WARN when the allowlist is empty in staging/production.
#
# The audit flagged this specifically: an empty allowlist in prod leaves
# /admin/* reachable from any IP, gated only by JWT role. The old INFO log
# blended into boot chatter. Now WARN, and ops can wire an alert on the
# log line.
# ---------------------------------------------------------------------------


def test_empty_allowlist_in_production_logs_warning(caplog):
    """Constructing the middleware with environment='production' and no
    allowlist must emit a WARNING — that's the signal for the ops alert."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.middleware.admin_ip_allowlist")
    AdminIPAllowlistMiddleware(
        app=None,
        allowed_ips="",
        environment="production",
    )

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "no WARNING emitted for empty allowlist in production"
    assert "ADMIN_ALLOWED_IPS is empty" in warns[0].message
    assert "production" in warns[0].message


def test_empty_allowlist_in_staging_logs_warning(caplog):
    """Same escalation for staging — a staging leak is also a real leak
    (test data can be sensitive)."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.middleware.admin_ip_allowlist")
    AdminIPAllowlistMiddleware(
        app=None,
        allowed_ips="",
        environment="staging",
    )

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "no WARNING emitted for empty allowlist in staging"


def test_empty_allowlist_in_dev_does_not_warn(caplog):
    """Local dev must NOT be noisy — every contributor booting the API
    without setting ADMIN_ALLOWED_IPS is the norm. INFO is fine, WARN is not."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.middleware.admin_ip_allowlist")
    AdminIPAllowlistMiddleware(
        app=None,
        allowed_ips="",
        environment="dev",
    )

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warns, f"unexpected warning in dev: {[r.message for r in warns]}"


def test_non_empty_allowlist_does_not_warn_even_in_production(caplog):
    """When production HAS an allowlist configured, no warning — everything
    is fine, don't cry wolf."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.middleware.admin_ip_allowlist")
    AdminIPAllowlistMiddleware(
        app=None,
        allowed_ips="10.0.0.0/8",
        environment="production",
    )

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warns, f"unexpected warning when allowlist is set: {[r.message for r in warns]}"
