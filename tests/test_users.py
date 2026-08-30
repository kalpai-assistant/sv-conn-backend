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


# ---------------------------------------------------------------------------
# Address coordinates + is_default semantics (SVC-197)
#
# Coordinates matter for the provider match — the geo query in
# booking_service uses the PostGIS point, and an address without one is
# unroutable. The `is_default` flag is what the booking flow reads when the
# consumer doesn't pick an address explicitly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_address_with_lat_lng_persists_postgis_point(client, auth_headers, db_session):
    """When the frontend geocodes and sends lat/lng, they must round-trip as a
    real PostGIS POINT — not a NULL location that would silently exclude this
    address from every nearby-provider query."""
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.post(
        "/api/v1/users/me/addresses",
        json={
            "label": "home",
            "full_address": "MG Road, Bangalore",
            "lat": 12.9716,
            "lng": 77.5946,
        },
        headers=auth_headers(str(user.id), "consumer"),
    )
    assert response.status_code == 201, response.text
    body = response.json()

    persisted = await db_session.get(Address, uuid.UUID(body["id"]))
    assert persisted is not None
    # The `lat`/`lng` properties (backed by point_lat_lng on `location`) must
    # return the values we sent — a bug in the WKT builder or the property
    # unpacker would flip them silently.
    assert persisted.lat == pytest.approx(12.9716)
    assert persisted.lng == pytest.approx(77.5946)


@pytest.mark.asyncio
async def test_add_address_without_lat_lng_leaves_location_null(client, auth_headers, db_session):
    """Free-text addresses (no geocode) legitimately have NULL location — the
    schema treats them as fallback, not as an error. Callers must handle that."""
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.post(
        "/api/v1/users/me/addresses",
        json={"label": "home", "full_address": "Flat 3, Some Lane"},
        headers=auth_headers(str(user.id), "consumer"),
    )
    assert response.status_code == 201, response.text
    persisted = await db_session.get(Address, uuid.UUID(response.json()["id"]))
    assert persisted.location is None
    assert persisted.lat is None
    assert persisted.lng is None


@pytest.mark.asyncio
async def test_add_default_address_unsets_previous_default(client, auth_headers, db_session):
    """A user can have exactly one `is_default` address. Adding a new default
    must flip the previous one to False in the same transaction — otherwise
    the booking flow's `first-is_default` lookup returns whichever the DB
    happens to list first."""
    user = await UserFactory.create(db_session, role="consumer")

    # Seed a first "default" address directly.
    first = Address(user_id=user.id, label="home", full_address="First place", is_default=True)
    db_session.add(first)
    await db_session.flush()

    response = await client.post(
        "/api/v1/users/me/addresses",
        json={
            "label": "work",
            "full_address": "Second place",
            "is_default": True,
        },
        headers=auth_headers(str(user.id), "consumer"),
    )
    assert response.status_code == 201, response.text

    # The previously-default row must have been demoted.
    await db_session.refresh(first)
    assert first.is_default is False
    new_default = await db_session.get(Address, uuid.UUID(response.json()["id"]))
    assert new_default.is_default is True


# ---------------------------------------------------------------------------
# SVC-242 — deleting the default promotes another address to default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_default_promotes_oldest_remaining(client, auth_headers, db_session):
    """SVC-242: deleting the default address leaves the user with no default,
    which breaks the booking flow's `first-is_default` lookup. The oldest
    remaining address should be promoted so the next booking still auto-fills.
    """
    import asyncio

    user = await UserFactory.create(db_session, role="consumer")

    old = Address(user_id=user.id, label="home", full_address="Old place", is_default=True)
    db_session.add(old)
    await db_session.flush()

    # Ensure a distinct created_at so the ORDER BY is deterministic.
    await asyncio.sleep(0.01)

    mid = Address(user_id=user.id, label="work", full_address="Mid place", is_default=False)
    db_session.add(mid)
    await db_session.flush()

    await asyncio.sleep(0.01)

    new = Address(user_id=user.id, label="other", full_address="New place", is_default=False)
    db_session.add(new)
    await db_session.flush()

    # Delete the current default.
    response = await client.delete(
        f"/api/v1/users/me/addresses/{old.id}",
        headers=auth_headers(str(user.id), "consumer"),
    )
    assert response.status_code == 204, response.text

    # The oldest of the remaining two (mid) should now be default.
    await db_session.refresh(mid)
    await db_session.refresh(new)
    assert mid.is_default is True
    assert new.is_default is False


@pytest.mark.asyncio
async def test_delete_default_when_only_one_address_is_noop(client, auth_headers, db_session):
    """SVC-242 boundary: deleting the ONLY address happens to be the default,
    but there's nothing to promote. Must not error or leave state inconsistent.
    """
    user = await UserFactory.create(db_session, role="consumer")

    only = Address(user_id=user.id, label="home", full_address="Only place", is_default=True)
    db_session.add(only)
    await db_session.flush()
    only_id = only.id

    response = await client.delete(
        f"/api/v1/users/me/addresses/{only_id}",
        headers=auth_headers(str(user.id), "consumer"),
    )
    assert response.status_code == 204, response.text

    # Row is gone; no phantom row was created.
    still_there = await db_session.get(Address, only_id)
    assert still_there is None


@pytest.mark.asyncio
async def test_delete_non_default_does_not_touch_default(client, auth_headers, db_session):
    """SVC-242 regression: deleting a non-default address leaves the existing
    default untouched. Only the delete-default path promotes.
    """
    user = await UserFactory.create(db_session, role="consumer")

    default = Address(user_id=user.id, label="home", full_address="Home", is_default=True)
    non_default = Address(user_id=user.id, label="work", full_address="Work", is_default=False)
    db_session.add_all([default, non_default])
    await db_session.flush()

    response = await client.delete(
        f"/api/v1/users/me/addresses/{non_default.id}",
        headers=auth_headers(str(user.id), "consumer"),
    )
    assert response.status_code == 204, response.text

    await db_session.refresh(default)
    assert default.is_default is True
