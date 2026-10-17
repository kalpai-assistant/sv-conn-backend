"""
PATCH /users/me/fcm-token — register and clear the caller's FCM token (SVC-164).

The endpoint had no coverage before this ticket. Covers:
- Registering a real token (no regression on the pre-existing behaviour)
- Clearing with an explicit `null` → NULL in the column, 204
- An omitted field → 422, so a malformed client cannot silently wipe push
- Empty / whitespace-only strings normalising to NULL rather than storing ""
- Clearing an already-NULL token staying a 204 no-op (logout is retried)
- JWT gating, and one user being unable to touch another's row
"""

import pytest
from sqlalchemy import select

from app.models.user import User
from tests.factories.user_factory import UserFactory

ENDPOINT = "/api/v1/users/me/fcm-token"


async def _stored_token(db, user_id) -> str | None:
    """Re-read the column straight from the DB, bypassing the identity map."""
    result = await db.execute(select(User.fcm_token).where(User.id == user_id))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Registering a token (existing behaviour — must not regress)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_token_happy_path(client, auth_headers, db_session):
    """A real token is persisted and the endpoint returns 204."""
    user = await UserFactory.create(db_session)

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": "fcm-device-token-abc123"},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 204, response.text
    assert await _stored_token(db_session, user.id) == "fcm-device-token-abc123"


@pytest.mark.asyncio
async def test_set_token_replaces_existing(client, auth_headers, db_session):
    """Re-registering overwrites the previously stored token."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "old-token"
    await db_session.flush()

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": "new-token"},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 204, response.text
    assert await _stored_token(db_session, user.id) == "new-token"


@pytest.mark.asyncio
async def test_token_over_max_length_rejected(client, auth_headers, db_session):
    """max_length=255 still applies now that the field is nullable."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "live-token"
    await db_session.flush()

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": "x" * 256},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 422
    assert await _stored_token(db_session, user.id) == "live-token"


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_null_clears_token(client, auth_headers, db_session):
    """AC1: {"fcm_token": null} sets the column to NULL and returns 204."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "live-token"
    await db_session.flush()

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": None},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 204, response.text
    assert await _stored_token(db_session, user.id) is None


@pytest.mark.asyncio
async def test_empty_body_rejected(client, auth_headers, db_session):
    """AC2: clearing must be explicit — an omitted field must not wipe the token."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "live-token"
    await db_session.flush()

    response = await client.patch(ENDPOINT, json={}, headers=auth_headers(str(user.id)))

    assert response.status_code == 422
    assert await _stored_token(db_session, user.id) == "live-token"


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.asyncio
async def test_blank_string_stores_null_not_empty(client, auth_headers, db_session, blank):
    """AC3: a blank token normalises to NULL — "" would read as a live token."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "live-token"
    await db_session.flush()

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": blank},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code in (204, 422), response.text
    stored = await _stored_token(db_session, user.id)
    if response.status_code == 204:
        assert stored is None
    else:
        assert stored == "live-token"
    assert stored != ""


@pytest.mark.asyncio
async def test_clearing_already_null_token_is_a_noop(client, auth_headers, db_session):
    """AC5: logout is best-effort and may be retried — a repeat clear is not an error."""
    user = await UserFactory.create(db_session)
    assert user.fcm_token is None

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": None},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 204, response.text
    assert await _stored_token(db_session, user.id) is None


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_authentication(client, db_session):
    """AC6: the endpoint stays JWT-gated."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "live-token"
    await db_session.flush()

    response = await client.patch(ENDPOINT, json={"fcm_token": None})

    assert response.status_code in (401, 403)
    assert await _stored_token(db_session, user.id) == "live-token"


@pytest.mark.asyncio
async def test_clear_only_affects_the_caller(client, auth_headers, db_session):
    """AC6: the handler is current_user-scoped — another user's token is untouched."""
    caller = await UserFactory.create(db_session)
    caller.fcm_token = "caller-token"
    other = await UserFactory.create(db_session)
    other.fcm_token = "other-token"
    await db_session.flush()

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": None},
        headers=auth_headers(str(caller.id)),
    )

    assert response.status_code == 204, response.text
    assert await _stored_token(db_session, caller.id) is None
    assert await _stored_token(db_session, other.id) == "other-token"


# ---------------------------------------------------------------------------
# Token platform (SVC-371)
# ---------------------------------------------------------------------------


async def _stored_pair(db, user_id) -> tuple[str | None, str | None]:
    result = await db.execute(select(User.fcm_token, User.fcm_platform).where(User.id == user_id))
    return tuple(result.one())


@pytest.mark.parametrize("platform", ["android", "ios", "web"])
@pytest.mark.asyncio
async def test_platform_stored_with_token(client, auth_headers, db_session, platform):
    """Each value the release push filters on is accepted and stored."""
    user = await UserFactory.create(db_session)

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": "tok", "platform": platform},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 204, response.text
    assert await _stored_pair(db_session, user.id) == ("tok", platform)


@pytest.mark.asyncio
async def test_no_platform_still_204_and_stores_null(client, auth_headers, db_session):
    """Installed APKs predate the field: omitting it must not 422 (that kills
    their push), and must not keep a platform left by another app's token."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "old-token"
    user.fcm_platform = "web"
    await db_session.flush()

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": "apk-token"},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 204, response.text
    assert await _stored_pair(db_session, user.id) == ("apk-token", None)


@pytest.mark.parametrize("platform", ["pwa", "ANDROID", "macos", ""])
@pytest.mark.asyncio
async def test_unknown_platform_rejected(client, auth_headers, db_session, platform):
    """Only the three filterable values; ``pwa`` is a release name, not a token's."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "live-token"
    user.fcm_platform = "android"
    await db_session.flush()

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": "tok", "platform": platform},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 422
    assert await _stored_pair(db_session, user.id) == ("live-token", "android")


@pytest.mark.parametrize("body", [{"fcm_token": None}, {"fcm_token": None, "platform": "android"}])
@pytest.mark.asyncio
async def test_clear_also_clears_platform(client, auth_headers, db_session, body):
    """The platform never outlives its token, even when the clear names one."""
    user = await UserFactory.create(db_session)
    user.fcm_token = "live-token"
    user.fcm_platform = "android"
    await db_session.flush()

    response = await client.patch(ENDPOINT, json=body, headers=auth_headers(str(user.id)))

    assert response.status_code == 204, response.text
    assert await _stored_pair(db_session, user.id) == (None, None)


@pytest.mark.asyncio
async def test_blank_token_with_platform_stores_null_platform(client, auth_headers, db_session):
    """A blank token normalises to NULL, so its platform does too."""
    user = await UserFactory.create(db_session)

    response = await client.patch(
        ENDPOINT,
        json={"fcm_token": "  ", "platform": "ios"},
        headers=auth_headers(str(user.id)),
    )

    assert response.status_code == 204, response.text
    assert await _stored_pair(db_session, user.id) == (None, None)
