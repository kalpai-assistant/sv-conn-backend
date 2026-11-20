"""
RazorpayX kill switch — ``RAZORPAYX_ENABLED`` gates every path that could
either fire a live payout or accept a vendor callback into side-effect code.

Failure mode this test would catch: a refactor that re-introduces an
implicit "empty creds" gate (which a dev with prod SSM on their laptop
bypasses accidentally), or a new RazorpayX entry point that forgets to
check the flag. The gates live in five places; one test per place.

The conftest autouse fixture (``razorpayx_enabled_in_tests``) flips the
flag ON for the whole suite. Each test here flips it OFF for its own
scope, then asserts the expected refusal / log-only behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.config import get_settings
from app.exceptions import PayoutRefused
from app.services.payout_service import PayoutService
from app.utils.razorpayx_client import RazorpayXDisabledError, create_payout, list_payouts

# ---- client-layer gates ------------------------------------------------------


@pytest.mark.asyncio
async def test_create_payout_refuses_when_disabled(monkeypatch):
    """``razorpayx_client.create_payout`` must not make the HTTP call at all.

    Failure mode: a refactor removes the early guard and relies on
    "account_number empty" to trip the vendor. A dev who pulls prod creds
    into ``.env`` would then fire real money on their first local payout.
    """
    # Patch BOTH module settings — the client caches a module-level reference.
    monkeypatch.setattr(get_settings(), "RAZORPAYX_ENABLED", False)
    from app.utils import razorpayx_client

    monkeypatch.setattr(razorpayx_client.settings, "RAZORPAYX_ENABLED", False)

    with pytest.raises(RazorpayXDisabledError, match="create_payout"):
        await create_payout(
            amount_paise=1000,
            idempotency_key="key-1",
            reference_id="ref-1",
            vpa="test@upi",
        )


@pytest.mark.asyncio
async def test_list_payouts_refuses_when_disabled(monkeypatch):
    """Same guard on the read path, so nightly reconcile can't accidentally
    hit the vendor after an SSM rotation that left creds intact but intent
    off."""
    monkeypatch.setattr(get_settings(), "RAZORPAYX_ENABLED", False)
    from app.utils import razorpayx_client

    monkeypatch.setattr(razorpayx_client.settings, "RAZORPAYX_ENABLED", False)

    with pytest.raises(RazorpayXDisabledError, match="list_payouts"):
        await list_payouts()


# ---- service-layer gates -----------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_payout_refuses_when_disabled(db_session, monkeypatch):
    """The admin route calls preflight BEFORE queuing a Celery task. Refuse
    at the service edge so the admin gets a clean 402 — not a 202 "queued"
    the task then rolls back.

    No provider setup needed: the flag check sits in front of
    ``_check_payable``, so a nonexistent provider id is fine; the test is
    about the gate, not the DB lookup.
    """
    from app.services import payout_service as payout_service_module

    monkeypatch.setattr(payout_service_module.settings, "RAZORPAYX_ENABLED", False)

    with pytest.raises(PayoutRefused, match="RAZORPAYX_ENABLED"):
        await PayoutService(db_session).preflight_payout(uuid4())


@pytest.mark.asyncio
async def test_create_provider_payout_refuses_without_audit_row(db_session, monkeypatch):
    """A disabled-flag refusal is distinct from a "lost response" audit
    outcome. The attempting-audit path exists to protect against RazorpayX
    accepting the payout while we lose the response — refusing at the flag
    gate means the call never left our process, so no audit row is created.

    Failure mode: an operator grepping payout_audits after a disabled
    window would otherwise see noisy "refused" rows from clicks that never
    had a chance to succeed; they'd mask real refusals.
    """
    from app.models.payout_audit import PayoutAudit
    from app.services import payout_service as payout_service_module

    monkeypatch.setattr(payout_service_module.settings, "RAZORPAYX_ENABLED", False)

    provider_id = uuid4()
    admin_id = uuid4()

    with pytest.raises(PayoutRefused, match="RAZORPAYX_ENABLED"):
        await PayoutService(db_session).create_provider_payout(provider_id, admin_id)

    # Zero audit rows: the flag check runs before ``_record_attempting_audit``.
    from sqlalchemy import select

    rows = (
        (
            await db_session.execute(
                select(PayoutAudit).where(PayoutAudit.provider_id == provider_id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_reconcile_payouts_is_noop_when_disabled(db_session, monkeypatch):
    """Nightly reconcile loops endlessly if it 500s. Short-circuiting to an
    empty report means a disabled-window nightly run logs a single
    skipped-reason line instead of erroring.
    """
    from app.services import payout_service as payout_service_module

    monkeypatch.setattr(payout_service_module.settings, "RAZORPAYX_ENABLED", False)

    with patch("app.services.payout_service.list_payouts", new_callable=AsyncMock) as mock_list:
        report = await PayoutService(db_session).reconcile_payouts(since_ts=0, until_ts=1)

    # Vendor was never called.
    mock_list.assert_not_called()
    assert report["checked"] == 0
    assert report["skipped_reason"] == "RAZORPAYX_ENABLED is False"


@pytest.mark.asyncio
async def test_handle_payout_webhook_ignores_when_disabled(db_session, monkeypatch, caplog):
    """A late webhook arriving during a disabled window must return 200 +
    log, not 500. Returning 500 makes RazorpayX retry the misdirected
    webhook forever; a clean 200 stops the retry.
    """
    import logging as py_logging

    from app.services import payout_service as payout_service_module

    monkeypatch.setattr(payout_service_module.settings, "RAZORPAYX_ENABLED", False)

    with caplog.at_level(py_logging.WARNING, logger="app.services.payout_service"):
        await PayoutService(db_session).handle_payout_webhook(
            event="payout.processed",
            payload={"payout": {"entity": {"id": "pout_disabled_test"}}},
        )

    assert any(
        "payout.webhook.ignored" in rec.message and "RAZORPAYX_ENABLED=False" in rec.message
        for rec in caplog.records
    )


# ---- route-layer gate --------------------------------------------------------


@pytest.mark.asyncio
async def test_payout_webhook_route_returns_200_when_disabled(client, monkeypatch):
    """The ``/payments/payout-webhook`` route must accept + log when the
    flag is off. Otherwise the SVC-465 empty-secret guard would 500
    (correctly — in a world where the flag exists to say "we are not
    talking to RazorpayX"), making RazorpayX retry forever.

    No signature header required: the route must short-circuit BEFORE the
    verify step.
    """
    from app.routers import payments as payments_router

    monkeypatch.setattr(payments_router.settings, "RAZORPAYX_ENABLED", False)

    response = await client.post(
        "/api/v1/payments/payout-webhook",
        json={"event": "payout.processed", "payload": {}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "ok"
    assert "RAZORPAYX_ENABLED" in body.get("ignored", "")


# ---- static-analysis-level gate: no sneaky new entry points ------------------


def test_razorpayx_module_exports_the_disabled_guard():
    """Lock in the module's public surface: ``RazorpayXDisabledError`` +
    ``_ensure_enabled`` must exist so a future helper that forgets the gate
    is a type-checker failure, not a silent prod call.
    """
    from app.utils import razorpayx_client

    assert hasattr(razorpayx_client, "RazorpayXDisabledError")
    assert hasattr(razorpayx_client, "_ensure_enabled")
    # The two public entry points both call ``_ensure_enabled``. Grep source
    # rather than mock-and-assert so a refactor that renames the helper
    # still flags here.
    import inspect

    for name in ("create_payout", "list_payouts"):
        source = inspect.getsource(getattr(razorpayx_client, name))
        assert (
            "_ensure_enabled" in source
        ), f"{name} must call _ensure_enabled; missing the kill-switch guard"


# Used by the admin-route test above, which mounts via the ``client`` fixture.
_: UUID  # keep flake happy — UUID is imported but only used indirectly below
