"""
CORS config parsing tests (B4).

The CORS list is parsed from a comma-separated env var. Easy to break
silently — a subtle split-and-strip regression means either
false-negative CORS (real client rejected) or false-positive
(unintended origin allowed). Neither surfaces without a specific
request from a specific origin, so pin the behaviour here.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app


def _parse(raw: str) -> list[str]:
    """Reproduce the parsing logic from create_app()."""
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# ---------------------------------------------------------------------------
# Parser — pure function
# ---------------------------------------------------------------------------


class TestParser:
    def test_single_origin(self):
        assert _parse("https://a.example.com") == ["https://a.example.com"]

    def test_multiple_origins(self):
        assert _parse("https://a.com,https://b.com") == [
            "https://a.com",
            "https://b.com",
        ]

    def test_whitespace_tolerated(self):
        """Realistic env-var content — humans add spaces around commas."""
        assert _parse(" https://a.com , https://b.com , ") == [
            "https://a.com",
            "https://b.com",
        ]

    def test_empty_string_yields_empty_list(self):
        """No origins configured = CORS effectively disabled (blocks browser calls)."""
        assert _parse("") == []

    def test_trailing_commas_dropped(self):
        assert _parse("https://a.com,,,") == ["https://a.com"]


# ---------------------------------------------------------------------------
# End-to-end: CORSMiddleware honours the env-driven list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cors_allows_configured_origin(monkeypatch):
    """Origin on the allow-list → CORS headers returned."""
    from app import main as main_mod

    monkeypatch.setattr(
        main_mod.settings,
        "CORS_ALLOWED_ORIGINS",
        "https://app.serviceconnect.in,capacitor://localhost",
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.options(
            "/health",
            headers={
                "Origin": "https://app.serviceconnect.in",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert r.headers.get("access-control-allow-origin") == "https://app.serviceconnect.in"


@pytest.mark.asyncio
async def test_cors_rejects_unlisted_origin(monkeypatch):
    """Origin NOT on the allow-list → no allow-origin header echoed back."""
    from app import main as main_mod

    monkeypatch.setattr(
        main_mod.settings,
        "CORS_ALLOWED_ORIGINS",
        "https://app.serviceconnect.in",
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.options(
            "/health",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    # Starlette's CORS middleware omits the header for disallowed origins,
    # which the browser interprets as a rejection.
    assert r.headers.get("access-control-allow-origin") is None
