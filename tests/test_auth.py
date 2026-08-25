"""
Auth integration tests.
Covers: send-otp rate limiting, verify-otp happy path, token refresh.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_send_otp_success(client):
    """Should return 200 and a message when phone is valid."""
    with (
        patch("app.services.auth_service.is_rate_limited", new=AsyncMock(return_value=False)),
        patch("app.services.auth_service.store_otp", new=AsyncMock()),
        patch("app.utils.sms_client.MSG91Client.send_otp", new=AsyncMock()),
    ):
        response = await client.post(
            "/api/v1/auth/send-otp",
            json={"phone": "+919876543210"},
        )
    assert response.status_code == 200
    assert response.json()["message"] == "OTP sent successfully"


@pytest.mark.asyncio
async def test_send_otp_rate_limited(client):
    """Should return 429 when the phone has exceeded the hourly OTP limit.

    Belt-and-suspenders on the phone choice: PR #133 moved the E2E bypass
    ahead of `is_rate_limited`, so a phone that happens to be in the
    caller's local `E2E_TEST_PHONES` short-circuits before the mock fires
    and the test silently 200s. Patching `_is_e2e_test_phone` here makes
    the assertion robust to any local env — matches the intent of the test
    (exercising the limiter path), independent of what the current .env
    lists as bypass numbers."""
    with (
        patch(
            "app.services.auth_service.AuthService._is_e2e_test_phone",
            return_value=False,
        ),
        patch("app.services.auth_service.is_rate_limited", new=AsyncMock(return_value=True)),
    ):
        response = await client.post(
            "/api/v1/auth/send-otp",
            json={"phone": "+919876543210"},
        )
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_send_otp_invalid_phone(client):
    """Should return 422 for a phone number that doesn't match the +91 pattern."""
    response = await client.post(
        "/api/v1/auth/send-otp",
        json={"phone": "9876543210"},  # Missing +91 prefix
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_verify_otp_invalid(client):
    """Should return 400 when OTP does not match the stored value."""
    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=False)):
        response = await client.post(
            "/api/v1/auth/verify-otp",
            json={"phone": "+919876543210", "otp": "000000", "role": "consumer"},
        )
    assert response.status_code == 400
    assert "OTP" in response.json()["message"]


@pytest.mark.asyncio
async def test_verify_otp_success(client, db_session):
    """Should return a valid token pair for a correct OTP."""
    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/api/v1/auth/verify-otp",
            json={"phone": "+919876543210", "otp": "123456", "role": "consumer"},
        )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["role"] == "consumer"
