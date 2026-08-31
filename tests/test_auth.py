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


# ---------------------------------------------------------------------------
# SVC-240 — verify_otp stamps last_login_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_otp_stamps_last_login_at(client, db_session):
    """SVC-240: successful verify sets `users.last_login_at` to (roughly) now.
    A previous sign-in stamp is overwritten each time; NULL exists only for
    users who have never signed in since the column was added.
    """
    from datetime import UTC, datetime

    from app.models.user import User

    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/api/v1/auth/verify-otp",
            json={"phone": "+919876500240", "otp": "123456", "role": "consumer"},
        )
    assert response.status_code == 200

    user_id = response.json()["user_id"]
    user = await db_session.get(User, uuid.UUID(user_id))
    await db_session.refresh(user)
    assert user.last_login_at is not None
    # Within a few seconds of "now"; test-machine clock skew is tolerated.
    delta = abs((datetime.now(UTC) - user.last_login_at).total_seconds())
    assert delta < 10, f"last_login_at drifted {delta}s from now"


# ---------------------------------------------------------------------------
# SVC-253 — Policy A: phone bound to a single role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_otp_rejects_role_mismatch(client, db_session):
    """SVC-253: a phone that already exists as consumer cannot re-authenticate
    as provider (or vice versa). 400 with a message that names the existing
    role so the caller knows to switch, not the vague "already registered".
    """
    user = await UserFactory.create(db_session, role="consumer", phone="+919876500253")
    await db_session.commit()
    assert user.role.value == "consumer"

    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/api/v1/auth/verify-otp",
            json={"phone": "+919876500253", "otp": "123456", "role": "provider"},
        )

    assert response.status_code == 400, response.text
    body = response.json()
    # The role name must appear so the caller can tell the user what to do —
    # a bare "already registered" would leave a UI stuck with no guidance.
    assert "consumer" in (body.get("message") or body.get("detail", ""))


@pytest.mark.asyncio
async def test_verify_otp_same_role_succeeds(client, db_session):
    """SVC-253 regression: a user re-signing-in with the same role still works.
    Only *different* role attempts are refused."""
    user = await UserFactory.create(db_session, role="consumer", phone="+919876500254")
    await db_session.commit()

    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/api/v1/auth/verify-otp",
            json={"phone": "+919876500254", "otp": "123456", "role": "consumer"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == str(user.id)


# ---------------------------------------------------------------------------
# SVC-241 — logout + refresh-token blacklist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_blacklists_refresh_and_prevents_refresh(client, db_session, redis_client):
    """SVC-241: POST /auth/logout blacklists the refresh token's jti; a
    subsequent POST /auth/refresh with the same token returns 401.
    """
    user = await UserFactory.create(db_session, role="consumer")
    await db_session.commit()

    # Mint via the actual factory so the jti + type are set.
    refresh = create_refresh_token({"sub": str(user.id), "role": "consumer", "type": "refresh"})

    # Sanity: works before logout.
    ok = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert ok.status_code == 200, ok.text

    # Log out.
    resp = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert resp.status_code == 204

    # Now the same token is 401.
    denied = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert denied.status_code == 401
    assert "revoked" in denied.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_logout_is_idempotent_on_garbage_token(client):
    """SVC-241: logout on a malformed / expired token succeeds silently.
    A client that lost its refresh token in a crash should still be able to
    press "sign out" and not see a 4xx that reads like their fault.
    """
    resp = await client.post("/api/v1/auth/logout", json={"refresh_token": "not.a.jwt"})
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_refresh_token_carries_jti_claim(db_session):
    """SVC-241 wire test: every refresh token minted by `create_refresh_token`
    carries a `jti` claim. Without it, blacklist-by-jti has nothing to key on
    and logout is a no-op — this test catches a regression that would silently
    disable the whole revocation story.
    """
    from app.utils.security import decode_jwt

    token = create_refresh_token({"sub": "abc", "role": "consumer", "type": "refresh"})
    payload = decode_jwt(token)
    assert payload is not None
    assert payload.get("jti"), "refresh token must carry a jti"
    # Fresh jti per mint — two tokens must have different ids.
    other = create_refresh_token({"sub": "abc", "role": "consumer", "type": "refresh"})
    assert decode_jwt(other)["jti"] != payload["jti"]
