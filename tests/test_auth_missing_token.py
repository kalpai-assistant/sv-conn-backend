"""
"Who are you?" is a 401, "you may not" is a 403.

FastAPI's ``HTTPBearer`` answers a missing Authorization header with 403. The
app signs out only on a 401, so a client calling with no token (a session the
frontend thought was still alive, found on device during SVC-367) was told
nothing it could act on, and retried forever. A wrong role stays a 403.
"""

import pytest

from tests.factories.user_factory import UserFactory

ME = "/api/v1/users/me"
ADMIN_ONLY = "/api/v1/admin/providers/kyc/pending"


def _assert_bearer_401(resp):
    assert resp.status_code == 401, resp.text
    assert resp.headers.get("www-authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_no_authorization_header_is_a_401(client):
    _assert_bearer_401(await client.get(ME))


@pytest.mark.asyncio
async def test_a_header_that_is_not_bearer_is_a_401(client):
    _assert_bearer_401(await client.get(ME, headers={"Authorization": "Basic dXNlcjpwdw=="}))


@pytest.mark.asyncio
async def test_an_invalid_token_is_a_401(client):
    _assert_bearer_401(await client.get(ME, headers={"Authorization": "Bearer not-a-jwt"}))


@pytest.mark.asyncio
async def test_an_inactive_user_is_a_401(client, db_session, auth_headers):
    user = await UserFactory.create(db_session, role="consumer", is_active=False)
    _assert_bearer_401(await client.get(ME, headers=auth_headers(str(user.id), "consumer")))


@pytest.mark.asyncio
async def test_a_wrong_role_is_still_a_403(client, db_session, auth_headers):
    user = await UserFactory.create(db_session, role="consumer")
    resp = await client.get(ADMIN_ONLY, headers=auth_headers(str(user.id), "consumer"))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_a_valid_token_still_works(client, db_session, auth_headers):
    user = await UserFactory.create(db_session, role="consumer")
    resp = await client.get(ME, headers=auth_headers(str(user.id), "consumer"))
    assert resp.status_code == 200, resp.text
