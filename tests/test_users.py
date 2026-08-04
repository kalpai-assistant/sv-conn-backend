"""Users router coverage — the me/profile + addresses surface.

test_user_fcm_token.py already covers PATCH /me/fcm-token thoroughly.
This file pins the rest:

- GET  /users/me
- PATCH /users/me                (name + email)
- GET  /users/me/addresses
- POST /users/me/addresses       (create + validation)
- DELETE /users/me/addresses/{id}  (own-only, silent-no-op for foreign / unknown)

Consumer-facing endpoints; auth failures are 401. Address ops enforce
per-user scoping — a lapse here would let one user delete another's saved
addresses via a guessed UUID.
"""

import uuid

import pytest

from app.models.address import Address
from tests.factories.user_factory import UserFactory

# ---------------------------------------------------------------------------
# GET /users/me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_returns_caller(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer", name="Alice")

    response = await client.get(
        "/api/v1/users/me",
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(user.id)
    assert body["name"] == "Alice"


@pytest.mark.asyncio
async def test_get_me_requires_auth(client):
    """No token → auth-required response (Starlette's HTTPBearer defaults
    to 403 when the Authorization header is missing entirely, and 401 when
    the token itself is invalid — the well-known FastAPI quirk). We only
    care that the endpoint is gated; either code is fine."""
    response = await client.get("/api/v1/users/me")
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# PATCH /users/me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_me_changes_name_and_email(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer", name="Old Name")

    response = await client.patch(
        "/api/v1/users/me",
        json={"name": "New Name", "email": "new@example.com"},
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "New Name"
    assert body["email"] == "new@example.com"


@pytest.mark.asyncio
async def test_update_me_partial_only_touches_provided_fields(client, auth_headers, db_session):
    """PATCH semantics: sending only `name` must not wipe an existing email."""
    user = await UserFactory.create(db_session, role="consumer", name="Old")
    # Seed an email directly on the model (schema has no createuser-with-email path).
    user.email = "keep@example.com"
    await db_session.flush()

    response = await client.patch(
        "/api/v1/users/me",
        json={"name": "Updated Only"},
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Updated Only"
    assert body["email"] == "keep@example.com"


# ---------------------------------------------------------------------------
# GET /users/me/addresses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_addresses_only_returns_callers(client, auth_headers, db_session):
    """User A must not see User B's addresses. If this ever breaks,
    everyone's saved-address list leaks to the next authenticated request."""
    alice = await UserFactory.create(db_session, role="consumer")
    bob = await UserFactory.create(db_session, role="consumer")

    alice_addr = Address(user_id=alice.id, label="home", full_address="Alice's home")
    bob_addr = Address(user_id=bob.id, label="home", full_address="Bob's home")
    db_session.add_all([alice_addr, bob_addr])
    await db_session.flush()

    response = await client.get(
        "/api/v1/users/me/addresses",
        headers=auth_headers(str(alice.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    ids = [a["id"] for a in body]
    assert str(alice_addr.id) in ids
    assert str(bob_addr.id) not in ids


# ---------------------------------------------------------------------------
# POST /users/me/addresses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_address_persists_to_caller(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.post(
        "/api/v1/users/me/addresses",
        json={"label": "home", "full_address": "42 Test Lane"},
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["label"] == "home"
    assert body["full_address"] == "42 Test Lane"


@pytest.mark.asyncio
async def test_add_address_rejects_invalid_label(client, auth_headers, db_session):
    """label is regex-constrained to home|work|other. `friend` should 422."""
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.post(
        "/api/v1/users/me/addresses",
        json={"label": "friend", "full_address": "Somewhere"},
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_add_address_rejects_too_short_address(client, auth_headers, db_session):
    """full_address has min_length=5. `abc` → 422."""
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.post(
        "/api/v1/users/me/addresses",
        json={"label": "home", "full_address": "abc"},
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# DELETE /users/me/addresses/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_own_address_removes_it(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer")
    addr = Address(user_id=user.id, label="home", full_address="Somewhere")
    db_session.add(addr)
    await db_session.flush()

    response = await client.delete(
        f"/api/v1/users/me/addresses/{addr.id}",
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 204, response.text
    # Confirm the row is gone.
    still_there = await db_session.get(Address, addr.id)
    assert still_there is None


@pytest.mark.asyncio
async def test_delete_foreign_address_is_silent_no_op(client, auth_headers, db_session):
    """Alice tries to delete Bob's address by guessing his address id.
    Service scopes DELETE by user_id — Bob's row survives, Alice gets 204.
    A 403 would leak the existence of a foreign address; 204+no-op is safer."""
    alice = await UserFactory.create(db_session, role="consumer")
    bob = await UserFactory.create(db_session, role="consumer")
    bobs_addr = Address(user_id=bob.id, label="home", full_address="Bob's home")
    db_session.add(bobs_addr)
    await db_session.flush()

    response = await client.delete(
        f"/api/v1/users/me/addresses/{bobs_addr.id}",
        headers=auth_headers(str(alice.id), "consumer"),
    )

    assert response.status_code == 204, response.text
    # Bob's address is still there — this is the whole assertion.
    still_there = await db_session.get(Address, bobs_addr.id)
    assert still_there is not None


@pytest.mark.asyncio
async def test_delete_unknown_address_is_silent_204(client, auth_headers, db_session):
    """A deleted-then-retried request should also 204, not 404 — same
    reasoning as the deactivate_user endpoint."""
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.delete(
        f"/api/v1/users/me/addresses/{uuid.uuid4()}",
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 204, response.text
