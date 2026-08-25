"""
Auth integration tests.
Covers: send-otp rate limiting, verify-otp happy path, token refresh.
"""

import uuid
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.utils.security import create_access_token, create_refresh_token
from tests.factories.user_factory import UserFactory


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


# ---------------------------------------------------------------------------
# POST /auth/refresh (SVC-196)
#
# Silent-refresh is what keeps a signed-in session alive across the 15-min
# access-token TTL. If any of these regress, users get logged out mid-flow
# and the app reads as "did I get signed out?" — the exact bug SVC-84 fixed
# on the frontend.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_returns_new_token_pair_from_valid_refresh(client, db_session):
    """A valid refresh_token mints a fresh access + refresh pair for the same user."""
    user = await UserFactory.create(db_session, role="consumer")
    await db_session.commit()

    refresh = create_refresh_token({"sub": str(user.id), "role": "consumer", "type": "refresh"})

    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["access_token"]
    assert data["refresh_token"]
    # Same user_id + role round-trip — the refresh path doesn't get to change either.
    assert data["user_id"] == str(user.id)
    assert data["role"] == "consumer"


@pytest.mark.asyncio
async def test_refresh_rejects_access_token(client, db_session):
    """The `type: refresh` claim is what separates the two token kinds. An access
    token (no `type`, or `type=access`) must be refused — otherwise a stolen
    short-lived access token could be traded for a long-lived refresh."""
    user = await UserFactory.create(db_session, role="consumer")
    await db_session.commit()

    access = create_access_token({"sub": str(user.id), "role": "consumer"})

    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": access})

    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_refresh_rejects_garbage_token(client):
    """A malformed / signature-invalid token → 401, not a 500 stack trace."""
    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "not.a.real.jwt"},
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_refresh_rejects_unknown_user(client):
    """A refresh token whose `sub` no longer resolves to a live user → 401.
    Deleted / deactivated users can't ride an old refresh back into the app."""
    refresh = create_refresh_token(
        {"sub": str(uuid.uuid4()), "role": "consumer", "type": "refresh"}
    )

    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})

    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_refresh_rejects_deactivated_user(client, db_session):
    """`is_active = False` is the soft-delete state — a refresh from a deactivated
    account must not be honoured. Same 401 as an unknown user; the endpoint
    deliberately doesn't distinguish (no user-enumeration oracle)."""
    user = await UserFactory.create(db_session, role="consumer", is_active=False)
    await db_session.commit()

    refresh = create_refresh_token({"sub": str(user.id), "role": "consumer", "type": "refresh"})

    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})

    assert response.status_code == 401, response.text
