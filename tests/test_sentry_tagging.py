"""
Sentry tag/user wiring tests (SC-47).

The tagging calls are cheap but easy to break silently — a broken tag
means Sentry issues don't cross-link to Better Stack logs, and by the
time you notice you're already debugging a real incident. Pin the
behaviour so refactors don't drop it.

Both hooks are guarded to never raise — if Sentry SDK is missing or
misconfigured, the tag/user call is a silent no-op. That's what these
tests verify: happy path calls through, unhappy path doesn't blow up.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.dependencies import _tag_sentry_user
from app.middleware.request_id import RequestIDMiddleware, _tag_sentry

# ---------------------------------------------------------------------------
# _tag_sentry — request_id tagging
# ---------------------------------------------------------------------------


def test_tag_sentry_calls_set_tag_with_request_id():
    """The hook forwards the request_id verbatim to sentry_sdk.set_tag."""
    fake = MagicMock()
    with patch.dict("sys.modules", {"sentry_sdk": fake}):
        _tag_sentry("req-abc-123")
    fake.set_tag.assert_called_once_with("request_id", "req-abc-123")


def test_tag_sentry_swallows_import_error():
    """No sentry_sdk installed → call is a no-op (must never raise)."""
    with patch.dict("sys.modules", {"sentry_sdk": None}):
        # Should not raise
        _tag_sentry("req-abc-123")


def test_tag_sentry_swallows_set_tag_error():
    """SDK installed but misconfigured → still no-op, not a 500."""
    fake = MagicMock()
    fake.set_tag.side_effect = RuntimeError("sentry broken")
    with patch.dict("sys.modules", {"sentry_sdk": fake}):
        _tag_sentry("req-xyz")  # must not raise


# ---------------------------------------------------------------------------
# _tag_sentry_user — user context
# ---------------------------------------------------------------------------


def test_tag_sentry_user_passes_id_and_role_only():
    """Only id + role reach Sentry — NEVER phone or email (send_default_pii=False)."""
    fake = MagicMock()
    with patch.dict("sys.modules", {"sentry_sdk": fake}):
        _tag_sentry_user("u-123", "consumer")
    fake.set_user.assert_called_once_with({"id": "u-123", "role": "consumer"})


def test_tag_sentry_user_never_raises():
    """Any Sentry SDK failure is swallowed — auth must not break on Sentry."""
    fake = MagicMock()
    fake.set_user.side_effect = ValueError("bad scope")
    with patch.dict("sys.modules", {"sentry_sdk": fake}):
        _tag_sentry_user("u-999", "admin")  # must not raise


# ---------------------------------------------------------------------------
# End-to-end: RequestIDMiddleware actually calls the tagger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_middleware_tags_sentry_with_generated_id():
    """A request without X-Request-ID → middleware generates one → Sentry gets it."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/ping")
    async def _ping():
        return {"ok": True}

    fake = MagicMock()
    with patch.dict("sys.modules", {"sentry_sdk": fake}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/ping")

    assert r.status_code == 200
    # The generated ID landed in both the response header and Sentry.
    generated_id = r.headers["X-Request-ID"]
    fake.set_tag.assert_called_once_with("request_id", generated_id)


@pytest.mark.asyncio
async def test_middleware_tags_sentry_with_client_provided_id():
    """A client-supplied X-Request-ID → Sentry sees the same value."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/ping")
    async def _ping():
        return {"ok": True}

    fake = MagicMock()
    with patch.dict("sys.modules", {"sentry_sdk": fake}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/ping", headers={"X-Request-ID": "client-supplied-rid"})

    assert r.headers["X-Request-ID"] == "client-supplied-rid"
    fake.set_tag.assert_called_once_with("request_id", "client-supplied-rid")
