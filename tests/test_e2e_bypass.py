"""
E2E OTP bypass tests (SVC-52).

The bypass is a small production-path change but with high leverage — one
wrong condition and it becomes a backdoor. So every guard is tested
independently: production env, empty whitelist, empty test OTP, and the
happy path. Standalone (no DB fixture) — the logic under test is pure
and can be exercised without spinning up the full app.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.services.auth_service import AuthService


def _configure(monkeypatch, *, env: str, whitelist: str, otp: str) -> None:
    """Overwrite the module-level settings snapshot used by AuthService."""
    from app.services import auth_service as auth_mod

    monkeypatch.setattr(auth_mod.settings, "ENVIRONMENT", env)
    monkeypatch.setattr(auth_mod.settings, "E2E_TEST_PHONES", whitelist)
    monkeypatch.setattr(auth_mod.settings, "E2E_TEST_OTP", otp)


# ---------------------------------------------------------------------------
# _is_e2e_test_phone — the guard that gates every other branch
# ---------------------------------------------------------------------------


def test_bypass_active_when_all_conditions_met(monkeypatch):
    _configure(monkeypatch, env="staging", whitelist="+919999900001,+919999900002", otp="123456")
    assert AuthService._is_e2e_test_phone("+919999900001") is True
    assert AuthService._is_e2e_test_phone("+919999900002") is True


def test_bypass_refuses_in_production_even_when_whitelisted(monkeypatch):
    """The one condition that must NEVER be overrideable — safety in prod."""
    _configure(monkeypatch, env="production", whitelist="+919999900001", otp="123456")
    assert AuthService._is_e2e_test_phone("+919999900001") is False


def test_bypass_off_when_test_otp_is_empty(monkeypatch):
    """A whitelist without a configured OTP is a config error — fail closed."""
    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="")
    assert AuthService._is_e2e_test_phone("+919999900001") is False


def test_bypass_off_when_whitelist_is_empty(monkeypatch):
    """Empty whitelist = bypass off, regardless of what OTP is set."""
    _configure(monkeypatch, env="staging", whitelist="", otp="123456")
    assert AuthService._is_e2e_test_phone("+919999900001") is False


def test_bypass_ignores_non_whitelisted_phone(monkeypatch):
    """A real user's phone is never eligible even when the bypass is on."""
    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")
    assert AuthService._is_e2e_test_phone("+919876543210") is False


def test_whitelist_tolerates_whitespace_and_trailing_commas(monkeypatch):
    """Realistic env-var content — humans put spaces around commas."""
    _configure(
        monkeypatch,
        env="staging",
        whitelist=" +919999900001 , +919999900002 , ",
        otp="123456",
    )
    assert AuthService._is_e2e_test_phone("+919999900001") is True
    assert AuthService._is_e2e_test_phone("+919999900002") is True


# ---------------------------------------------------------------------------
# send_otp integration — the bypass short-circuits SMS but not rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_otp_bypass_stores_fixed_otp_and_skips_sms(monkeypatch):
    """Happy path: whitelisted phone → fixed OTP stored, SMS client not called."""
    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    fake_sms = AsyncMock()
    fake_redis = AsyncMock()
    with (
        patch("app.services.auth_service.is_rate_limited", new=AsyncMock(return_value=False)),
        patch("app.services.auth_service.store_otp", new=AsyncMock()) as mock_store,
        patch("app.services.auth_service.get_sms_client", return_value=fake_sms),
    ):
        svc = AuthService(db=None, redis=fake_redis)
        await svc.send_otp("+919999900001")

    mock_store.assert_awaited_once_with(fake_redis, "+919999900001", "123456")
    fake_sms.send_otp.assert_not_called()


@pytest.mark.asyncio
async def test_send_otp_bypass_still_respects_rate_limit(monkeypatch):
    """A bypass phone is not a rate-limit exemption — same abuse controls apply."""
    from app.exceptions import RateLimitExceeded

    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    with (
        patch("app.services.auth_service.is_rate_limited", new=AsyncMock(return_value=True)),
        patch(
            "app.services.auth_service.otp_retry_after_seconds",
            new=AsyncMock(return_value=42),
        ),
        patch("app.services.auth_service.get_sms_client", return_value=AsyncMock()),
    ):
        svc = AuthService(db=None, redis=AsyncMock())
        with pytest.raises(RateLimitExceeded):
            await svc.send_otp("+919999900001")


@pytest.mark.asyncio
async def test_send_otp_non_bypass_phone_uses_normal_path(monkeypatch):
    """Non-whitelisted phone: generates a random OTP + calls the SMS client."""
    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    fake_sms = AsyncMock()
    with (
        patch("app.services.auth_service.is_rate_limited", new=AsyncMock(return_value=False)),
        patch("app.services.auth_service.store_otp", new=AsyncMock()) as mock_store,
        patch(
            "app.services.auth_service.generate_otp", return_value="998877",
        ),
        patch("app.services.auth_service.get_sms_client", return_value=fake_sms),
    ):
        svc = AuthService(db=None, redis=AsyncMock())
        await svc.send_otp("+919876543210")

    # Random OTP stored, SMS sent.
    assert mock_store.await_args.args[2] == "998877"
    fake_sms.send_otp.assert_awaited_once_with("+919876543210", "998877")
