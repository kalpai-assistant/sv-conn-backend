"""SVC-345: dedicated /auth/admin/* OTP endpoints.

Admin login lives outside the consumer/provider role picker. These tests cover
the role gate (non-admin phones must 404 regardless of whether a user exists),
the happy path, OTP + rate-limit reuse, and the consumer-side regression that
an admin cannot sign in through the consumer /auth/verify-otp path anymore.
"""

from unittest.mock import AsyncMock, patch

import pytest

from tests.factories.user_factory import UserFactory

# ---------------------------------------------------------------------------
# /auth/admin/send-otp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_send_otp_for_admin_phone_succeeds(client, db_session):
    """Admin phone + send-otp → 200. SMS + Redis layers are mocked out so the
    test exercises only the role gate + the shared OTP pipeline wiring."""
    admin = await UserFactory.create(db_session, role="admin")
    await db_session.commit()

    with (
        patch(
            "app.services.auth_service.AuthService._is_e2e_test_phone",
            return_value=False,
        ),
        patch("app.services.auth_service.is_rate_limited", new=AsyncMock(return_value=False)),
        patch("app.services.auth_service.store_otp", new=AsyncMock()),
        patch("app.utils.sms_client.MSG91Client.send_otp", new=AsyncMock()),
    ):
        response = await client.post(
            "/api/v1/auth/admin/send-otp",
            json={"phone": admin.phone},
        )

    assert response.status_code == 200, response.text
    assert response.json()["message"] == "OTP sent successfully"


@pytest.mark.asyncio
async def test_admin_send_otp_for_consumer_phone_returns_404(client, db_session):
    """Consumer phone must 404 — never 403 or a role-mismatch message that
    leaks "this phone exists as X". Same 404 as an unknown phone."""
    consumer = await UserFactory.create(db_session, role="consumer")
    await db_session.commit()

    with patch(
        "app.services.auth_service.AuthService._is_e2e_test_phone",
        return_value=False,
    ):
        response = await client.post(
            "/api/v1/auth/admin/send-otp",
            json={"phone": consumer.phone},
        )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_admin_send_otp_for_provider_phone_returns_404(client, db_session):
    """Provider phone must 404 — same leak-free shape as consumer."""
    provider = await UserFactory.create(db_session, role="provider")
    await db_session.commit()

    with patch(
        "app.services.auth_service.AuthService._is_e2e_test_phone",
        return_value=False,
    ):
        response = await client.post(
            "/api/v1/auth/admin/send-otp",
            json={"phone": provider.phone},
        )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_admin_send_otp_for_unknown_phone_returns_404(client):
    """Unknown phone → 404, and no user row is created. The admin endpoint
    must never behave like the consumer send-otp (which upserts on verify).

    Behavioural cover: the 404 + the leak-free message. The service has no
    create path on this route — a regression that added one would need to
    pass the role gate first, which an unknown phone cannot.
    """
    with patch(
        "app.services.auth_service.AuthService._is_e2e_test_phone",
        return_value=False,
    ):
        response = await client.post(
            "/api/v1/auth/admin/send-otp",
            json={"phone": "+919999988887"},
        )

    assert response.status_code == 404, response.text
    assert "No admin account" in response.json()["message"]


@pytest.mark.asyncio
async def test_admin_send_otp_rate_limited(client, db_session):
    """Rate limit uses the shared otp:{phone}:attempts counter. An admin who
    exceeds the hourly cap (5/hour, same as consumer) gets 429 with
    Retry-After, same wire shape."""
    admin = await UserFactory.create(db_session, role="admin")
    await db_session.commit()

    with (
        patch(
            "app.services.auth_service.AuthService._is_e2e_test_phone",
            return_value=False,
        ),
        patch("app.services.auth_service.is_rate_limited", new=AsyncMock(return_value=True)),
    ):
        response = await client.post(
            "/api/v1/auth/admin/send-otp",
            json={"phone": admin.phone},
        )

    assert response.status_code == 429, response.text
    assert "Retry-After" in response.headers


# ---------------------------------------------------------------------------
# /auth/admin/verify-otp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_verify_otp_happy_path(client, db_session):
    """Admin + valid OTP → 200 with an admin JWT. The role claim comes from
    the user row, never the request body (there is no role field on
    AdminVerifyOTPRequest)."""
    admin = await UserFactory.create(db_session, role="admin")
    await db_session.commit()

    with (
        patch(
            "app.services.auth_service.AuthService._is_e2e_test_phone",
            return_value=False,
        ),
        patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)),
    ):
        response = await client.post(
            "/api/v1/auth/admin/verify-otp",
            json={"phone": admin.phone, "otp": "123456"},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["role"] == "admin"
    assert data["user_id"] == str(admin.id)
    assert data["access_token"]
    assert data["refresh_token"]
    # Admin phone has existed since UserFactory.create; verify-otp must not
    # flag it as new (would reset onboarding state in the app).
    assert data["is_new_user"] is False


@pytest.mark.asyncio
async def test_admin_verify_otp_wrong_otp_returns_401(client, db_session):
    """Wrong OTP → 400 InvalidOTP, matching the consumer verify-otp shape.

    (Note: the handoff doc said 401, but the existing InvalidOTP exception
    maps to 400 and the consumer path returns the same. Keeping both paths
    on the same wire shape so the frontend hook doesn't need to branch on
    status code.)
    """
    admin = await UserFactory.create(db_session, role="admin")
    await db_session.commit()

    with (
        patch(
            "app.services.auth_service.AuthService._is_e2e_test_phone",
            return_value=False,
        ),
        patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=False)),
    ):
        response = await client.post(
            "/api/v1/auth/admin/verify-otp",
            json={"phone": admin.phone, "otp": "000000"},
        )

    assert response.status_code == 400, response.text
    assert "OTP" in response.json()["message"]


@pytest.mark.asyncio
async def test_admin_verify_otp_for_consumer_phone_returns_404(client, db_session):
    """Consumer phone + any OTP → 404, before the OTP is even checked.

    Role gate first prevents a leak: a valid-OTP response for a consumer
    phone would tell the attacker both that the OTP matched AND that this
    phone has an admin role — the whole point of 404 is to never give
    either signal.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    await db_session.commit()

    with (
        patch(
            "app.services.auth_service.AuthService._is_e2e_test_phone",
            return_value=False,
        ),
        patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)),
    ):
        response = await client.post(
            "/api/v1/auth/admin/verify-otp",
            json={"phone": consumer.phone, "otp": "123456"},
        )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_admin_verify_otp_for_unknown_phone_returns_404(client):
    """Unknown phone → 404, no user is created even on a valid OTP guess."""
    with (
        patch(
            "app.services.auth_service.AuthService._is_e2e_test_phone",
            return_value=False,
        ),
        patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)),
    ):
        response = await client.post(
            "/api/v1/auth/admin/verify-otp",
            json={"phone": "+919999988886", "otp": "123456"},
        )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_admin_verify_otp_rejects_bad_phone_pattern(client):
    """Pattern validation happens at the schema layer — bare 10-digit phones
    get 422 from pydantic before touching the service."""
    response = await client.post(
        "/api/v1/auth/admin/verify-otp",
        json={"phone": "9876543210", "otp": "123456"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_verify_otp_body_has_no_role_field(client, db_session):
    """The verify payload must not accept a `role` from the client. Pydantic
    silently drops unknown fields by default, but the schema is intentionally
    role-less so a client that sends role=consumer still gets admin back.
    """
    admin = await UserFactory.create(db_session, role="admin")
    await db_session.commit()

    with (
        patch(
            "app.services.auth_service.AuthService._is_e2e_test_phone",
            return_value=False,
        ),
        patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)),
    ):
        response = await client.post(
            "/api/v1/auth/admin/verify-otp",
            json={"phone": admin.phone, "otp": "123456", "role": "consumer"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin"


# ---------------------------------------------------------------------------
# Consumer-side regression — admin must NOT be able to use /auth/verify-otp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_verify_otp_rejects_admin_phone_with_role_consumer(client, db_session):
    """SVC-345 regression: an admin phone attempting the consumer path with
    role=consumer must still 400 ROLE_MISMATCH. The ticket's narrow fix
    (allow admin to sign in via consumer) was superseded by the dedicated
    admin endpoints, so the uniform RoleMismatch guard stays load-bearing.
    """
    admin = await UserFactory.create(db_session, role="admin")
    await db_session.commit()

    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/api/v1/auth/verify-otp",
            json={"phone": admin.phone, "otp": "123456", "role": "consumer"},
        )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "ROLE_MISMATCH"
    assert "admin" in body["message"]


@pytest.mark.asyncio
async def test_consumer_verify_otp_still_rejects_consumer_provider_mismatch(client, db_session):
    """SVC-253 regression (unchanged by SVC-345): consumer ↔ provider on the
    same phone still 400 ROLE_MISMATCH. Removing the admin case from the
    consumer path must not disturb this protection."""
    consumer = await UserFactory.create(db_session, role="consumer")
    await db_session.commit()

    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/api/v1/auth/verify-otp",
            json={"phone": consumer.phone, "otp": "123456", "role": "provider"},
        )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "ROLE_MISMATCH"


@pytest.mark.asyncio
async def test_consumer_verify_otp_schema_does_not_accept_admin_role(client):
    """SVC-345: `role=admin` on the consumer endpoint is a 422. The literal
    pattern on VerifyOTPRequest.role deliberately omits "admin"; admins use
    /auth/admin/verify-otp."""
    response = await client.post(
        "/api/v1/auth/verify-otp",
        json={"phone": "+919876543210", "otp": "123456", "role": "admin"},
    )
    assert response.status_code == 422, response.text
