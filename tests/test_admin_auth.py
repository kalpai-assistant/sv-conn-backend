"""The admin phone login (SVC-345) is retired by SVC-422: its two routes answer 410,
and an admin's number can't get a token through the consumer path either.
"""

from unittest.mock import AsyncMock, patch

import pytest

from tests.factories.user_factory import UserFactory


@pytest.mark.parametrize(
    "path",
    ["/api/v1/auth/admin/send-otp", "/api/v1/auth/admin/verify-otp"],
)
@pytest.mark.parametrize(
    "body",
    [{}, {"phone": "+919999900001"}, {"phone": "+919999900001", "otp": "123456"}],
)
async def test_old_admin_phone_routes_are_gone(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 410, response.text
    assert response.json()["code"] == "ADMIN_PHONE_LOGIN_GONE"


async def test_old_admin_phone_route_gives_a_real_admin_no_token(client, db_session):
    """Super Admin at their real number, with an OTP that would have verified."""
    admin = await UserFactory.create(db_session, role="admin")
    await db_session.commit()
    with patch("app.services.auth_service.verify_otp", new=AsyncMock(return_value=True)):
        response = await client.post(
            "/api/v1/auth/admin/verify-otp", json={"phone": admin.phone, "otp": "123456"}
        )
    assert response.status_code == 410
    assert "access_token" not in response.json()


@pytest.mark.asyncio
async def test_consumer_verify_otp_rejects_admin_phone_with_role_consumer(client, db_session):
    """An admin phone on the consumer path must 400 ROLE_MISMATCH. With the admin
    phone login gone, this is what keeps that number from getting a token at all.
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
    pattern on VerifyOTPRequest.role deliberately omits "admin"."""
    response = await client.post(
        "/api/v1/auth/verify-otp",
        json={"phone": "+919876543210", "otp": "123456", "role": "admin"},
    )
    assert response.status_code == 422, response.text
