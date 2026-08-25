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
# send_otp integration — the bypass short-circuits SMS, Redis AND the limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_otp_bypass_touches_no_redis_and_skips_sms(monkeypatch):
    """Happy path: whitelisted phone → nothing stored, nothing sent.

    The stored value used to be `settings.E2E_TEST_OTP` — a constant the
    verifier already knows. Round-tripping it through Redis verified nothing
    about the caller, so the write is gone and `verify_otp` compares against
    the setting directly.
    """
    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    fake_sms = AsyncMock()
    fake_redis = AsyncMock()
    with (
        patch("app.services.auth_service.store_otp", new=AsyncMock()) as mock_store,
        patch("app.services.auth_service.get_sms_client", return_value=fake_sms),
    ):
        svc = AuthService(db=None, redis=fake_redis)
        await svc.send_otp("+919999900001")

    mock_store.assert_not_awaited()
    fake_sms.send_otp.assert_not_called()
    # The point of the change: usable when Redis is unavailable.
    fake_redis.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_otp_bypass_works_when_redis_is_down(monkeypatch):
    """The reason this path stopped touching Redis.

    On 2026-08-24 the Upstash request quota was exhausted and every Redis
    command raised. The bypass died at `is_rate_limited()` before it ever
    reached the whitelist check — so the one login route that needs no SMS
    provider was unusable exactly when it was most needed.
    """
    from redis.exceptions import ResponseError

    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    exploding_redis = AsyncMock()
    exploding_redis.incr.side_effect = ResponseError("max requests limit exceeded")
    exploding_redis.get.side_effect = ResponseError("max requests limit exceeded")

    with (
        patch(
            "app.services.auth_service.is_rate_limited",
            new=AsyncMock(side_effect=ResponseError("max requests limit exceeded")),
        ),
        patch("app.services.auth_service.get_sms_client", return_value=AsyncMock()),
    ):
        svc = AuthService(db=None, redis=exploding_redis)
        # Must not raise: the bypass is checked before anything touches Redis.
        await svc.send_otp("+919999900001")


@pytest.mark.asyncio
async def test_non_bypass_phone_still_rate_limited(monkeypatch):
    """Removing the limiter from the bypass must not remove it for everyone.

    Mutation guard: deleting the `is_rate_limited` call outright would still
    satisfy every bypass test above.
    """
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
            await svc.send_otp("+919876543210")


@pytest.mark.asyncio
async def test_send_otp_non_bypass_phone_uses_normal_path(monkeypatch):
    """Non-whitelisted phone: generates a random OTP + calls the SMS client."""
    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    fake_sms = AsyncMock()
    with (
        patch("app.services.auth_service.is_rate_limited", new=AsyncMock(return_value=False)),
        patch("app.services.auth_service.store_otp", new=AsyncMock()) as mock_store,
        patch(
            "app.services.auth_service.generate_otp",
            return_value="998877",
        ),
        patch("app.services.auth_service.get_sms_client", return_value=fake_sms),
    ):
        svc = AuthService(db=None, redis=AsyncMock())
        await svc.send_otp("+919876543210")

    # Random OTP stored, SMS sent.
    assert mock_store.await_args.args[2] == "998877"
    fake_sms.send_otp.assert_awaited_once_with("+919876543210", "998877")


# ---------------------------------------------------------------------------
# verify_otp — the bypass counterpart, compared against the constant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_bypass_accepts_configured_otp_without_redis(monkeypatch):
    """A whitelisted phone verifies against `settings.E2E_TEST_OTP` directly."""
    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    exploding_redis = AsyncMock()
    exploding_redis.get.side_effect = AssertionError(
        "verify must not read Redis for a bypass phone"
    )

    with (
        patch("app.services.auth_service.verify_otp", new=AsyncMock()) as mock_verify,
        patch("app.services.auth_service.get_sms_client", return_value=AsyncMock()),
    ):
        svc = AuthService(db=None, redis=exploding_redis)
        svc._upsert_user = AsyncMock(return_value=(object(), False))
        svc._build_token_response = lambda user, is_new: {
            "access_token": "t",
            "is_new_user": is_new,
        }

        result = await svc.verify_otp("+919999900001", "123456", "consumer")

    assert result["access_token"] == "t"
    mock_verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_bypass_rejects_a_wrong_code(monkeypatch):
    """The bypass is not "any code works" — the constant still has to match.

    Without this, a bypass that accepted anything would pass every other
    test in this file.
    """
    from app.exceptions import InvalidOTP

    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    with patch("app.services.auth_service.get_sms_client", return_value=AsyncMock()):
        svc = AuthService(db=None, redis=AsyncMock())
        with pytest.raises(InvalidOTP):
            await svc.verify_otp("+919999900001", "000000", "consumer")


@pytest.mark.asyncio
async def test_verify_non_bypass_phone_still_reads_redis(monkeypatch):
    """Everyone else keeps the real, stored-OTP path.

    Mutation guard: a bypass that ignored the whitelist would turn
    E2E_TEST_OTP into a master key for every account on the system.
    """
    _configure(monkeypatch, env="staging", whitelist="+919999900001", otp="123456")

    with (
        patch(
            "app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)
        ) as mock_verify,
        patch("app.services.auth_service.get_sms_client", return_value=AsyncMock()),
    ):
        svc = AuthService(db=None, redis=AsyncMock())
        svc._upsert_user = AsyncMock(return_value=(object(), False))
        svc._build_token_response = lambda user, is_new: {"access_token": "t"}

        await svc.verify_otp("+919876543210", "998877", "consumer")

    mock_verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_in_production_never_accepts_the_constant(monkeypatch):
    """The gate that matters most: in production the bypass does not exist,
    so the configured constant is just a wrong OTP like any other."""
    from app.exceptions import InvalidOTP

    _configure(monkeypatch, env="production", whitelist="+919999900001", otp="123456")

    with (
        patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=False)),
        patch("app.services.auth_service.get_sms_client", return_value=AsyncMock()),
    ):
        svc = AuthService(db=None, redis=AsyncMock())
        with pytest.raises(InvalidOTP):
            await svc.verify_otp("+919999900001", "123456", "consumer")
