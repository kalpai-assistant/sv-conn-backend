"""
Log correlation tests (SVC-46).

Proves the piece that's easy to break silently: a request-scoped `request_id`
set by `RequestIDMiddleware` shows up in every log record emitted during the
request, without any service code having to pass it explicitly.
"""

import io
import json
import logging

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.logging_config import configure_logging, request_id_var
from app.middleware.request_id import RequestIDMiddleware


def _capture_json_logs():
    """Reconfigure logging with a JSON renderer into an in-memory buffer.

    Returns the buffer + a teardown callable — pytest cleanup restores the
    root logger. Must run AFTER configure_logging so our handler is the only
    one attached.
    """
    configure_logging(level="INFO", json_logs=True)
    buf = io.StringIO()
    root = logging.getLogger()
    # Swap the single handler installed by configure_logging with one that
    # writes to our buffer but keeps the same formatter (so we still get JSON).
    orig = root.handlers[0]
    handler = logging.StreamHandler(buf)
    handler.setFormatter(orig.formatter)
    root.handlers[:] = [handler]

    def restore():
        root.handlers[:] = [orig]

    return buf, restore


@pytest.mark.asyncio
async def test_request_id_appears_in_logs_emitted_inside_a_request():
    buf, restore = _capture_json_logs()
    try:
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)

        @app.get("/log")
        async def _log_route(request: Request):
            logging.getLogger("test.route").info("handling request")
            return {"ok": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/log", headers={"X-Request-ID": "test-rid-123"})

        assert r.status_code == 200
        assert r.headers["X-Request-ID"] == "test-rid-123"

        # Parse each JSON log line, find ours.
        records = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        ours = [r for r in records if r.get("event") == "handling request"]
        assert ours, f"log line missing; captured: {buf.getvalue()!r}"
        assert ours[0]["request_id"] == "test-rid-123"
    finally:
        restore()


@pytest.mark.asyncio
async def test_request_id_is_generated_when_client_omits_header():
    buf, restore = _capture_json_logs()
    try:
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)

        @app.get("/log")
        async def _log_route():
            logging.getLogger("test.route").info("no header")
            return {"ok": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/log")

        assert r.status_code == 200
        rid = r.headers["X-Request-ID"]
        assert len(rid) == 36  # UUID4

        records = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        ours = [r for r in records if r.get("event") == "no header"]
        assert ours and ours[0]["request_id"] == rid
    finally:
        restore()


def test_request_id_var_defaults_to_none_outside_a_request():
    """Startup / background logs must not crash when no request is active."""
    assert request_id_var.get() is None
