"""Admin router coverage — the endpoints not touched by test_kyc.

test_kyc.py already covers the KYC review + verify + reject surface.
This file pins the rest of admin.py:

- GET /admin/users            (list, pagination, RBAC)
- PATCH /admin/users/{id}/deactivate  (happy path, 204, silent-no-op-if-missing, RBAC)
- GET /admin/bookings         (list, status filter, RBAC)

Every endpoint gets an explicit non-admin 403 test — RBAC is only useful
if we're sure it fires. Cheap insurance against a future refactor that
drops the `require_role` dependency by accident.
"""

import pytest

from app.models.user import User
from tests.factories.user_factory import UserFactory

# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users_returns_all_users_for_admin(client, auth_headers, db_session):
    """Admin lists users — returns everyone in the DB (including themselves)."""
    admin = await UserFactory.create(db_session, role="admin")
    await UserFactory.create(db_session, role="consumer")
    await UserFactory.create(db_session, role="provider")

    response = await client.get(
        "/api/v1/admin/users",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    ids = [u["id"] for u in body]
    assert str(admin.id) in ids
    assert len(body) >= 3


@pytest.mark.asyncio
async def test_list_users_paginates(client, auth_headers, db_session):
    """per_page=1 → response has exactly one entry, page=2 has the next."""
    admin = await UserFactory.create(db_session, role="admin")
    await UserFactory.create(db_session, role="consumer")
    await UserFactory.create(db_session, role="consumer")

    page_1 = await client.get(
        "/api/v1/admin/users?page=1&per_page=1",
        headers=auth_headers(str(admin.id), "admin"),
    )
    page_2 = await client.get(
        "/api/v1/admin/users?page=2&per_page=1",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert page_1.status_code == 200 and page_2.status_code == 200
    assert len(page_1.json()) == 1
    assert len(page_2.json()) == 1
    assert page_1.json()[0]["id"] != page_2.json()[0]["id"]


@pytest.mark.asyncio
async def test_list_users_rejects_non_admin(client, auth_headers, db_session):
    """Consumer + provider both 403 — proves the require_role gate fires,
    not just that admins get 200."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")

    r_c = await client.get(
        "/api/v1/admin/users",
        headers=auth_headers(str(consumer.id), "consumer"),
    )
    r_p = await client.get(
        "/api/v1/admin/users",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert r_c.status_code == 403, r_c.text
    assert r_p.status_code == 403, r_p.text


# ---------------------------------------------------------------------------
# PATCH /admin/users/{user_id}/deactivate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deactivate_user_sets_is_active_false(client, auth_headers, db_session):
    """Happy path — is_active flips to False, 204 body."""
    admin = await UserFactory.create(db_session, role="admin")
    target = await UserFactory.create(db_session, role="consumer", is_active=True)

    response = await client.patch(
        f"/api/v1/admin/users/{target.id}/deactivate",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 204, response.text
    # Endpoint commits, so a fresh SELECT is authoritative.
    refreshed = await db_session.get(User, target.id)
    assert refreshed is not None
    await db_session.refresh(refreshed)
    assert refreshed.is_active is False


@pytest.mark.asyncio
async def test_deactivate_unknown_user_is_a_silent_204(client, auth_headers, db_session):
    """The router deliberately silent-no-ops when the user id doesn't exist —
    admin actions on missing users shouldn't 404 (e.g. deleted-elsewhere race).
    Locking in the behaviour."""
    import uuid

    admin = await UserFactory.create(db_session, role="admin")

    response = await client.patch(
        f"/api/v1/admin/users/{uuid.uuid4()}/deactivate",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 204, response.text


@pytest.mark.asyncio
async def test_deactivate_rejects_non_admin(client, auth_headers, db_session):
    """A consumer must not be able to deactivate their own account (or
    anyone's) via the admin endpoint — that would be a full account-nuke
    escalation without any confirmation."""
    consumer = await UserFactory.create(db_session, role="consumer")
    target = await UserFactory.create(db_session, role="consumer")

    response = await client.patch(
        f"/api/v1/admin/users/{target.id}/deactivate",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 403, response.text
    # And crucially the target is still active — the gate ran before the write.
    await db_session.refresh(target)
    assert target.is_active is True


# ---------------------------------------------------------------------------
# GET /admin/bookings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_bookings_returns_all_across_users(client, auth_headers, db_session):
    """Admin sees every booking regardless of who created it. Distinct from
    GET /bookings which is user-scoped."""
    from app.models.address import Address
    from app.models.booking import BookingStatus
    from app.models.category import Category
    from app.models.service import Service
    from tests.factories.booking_factory import BookingFactory

    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")

    cat = Category(name="Repair", slug="repair", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name="Fix Something",
        base_price=200.0,
        duration_minutes=45,
        is_active=True,
    )
    db_session.add(svc)
    addr = Address(user_id=consumer.id, label="home", full_address="1 Test St")
    db_session.add(addr)
    await db_session.flush()

    await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )
    await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.COMPLETED,
    )

    response = await client.get(
        "/api/v1/admin/bookings",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 2
    assert len(body["bookings"]) >= 2


@pytest.mark.asyncio
async def test_list_bookings_filters_by_status(client, auth_headers, db_session):
    """`?status=completed` narrows to only completed bookings."""
    from app.models.address import Address
    from app.models.booking import BookingStatus
    from app.models.category import Category
    from app.models.service import Service
    from tests.factories.booking_factory import BookingFactory

    admin = await UserFactory.create(db_session, role="admin")
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")

    cat = Category(name="Salon", slug="salon", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name="Haircut",
        base_price=300.0,
        duration_minutes=30,
        is_active=True,
    )
    db_session.add(svc)
    addr = Address(user_id=consumer.id, label="home", full_address="2 Test St")
    db_session.add(addr)
    await db_session.flush()

    await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )
    await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=addr.id,
        status=BookingStatus.COMPLETED,
    )

    response = await client.get(
        "/api/v1/admin/bookings?status=completed",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # Every returned booking must be completed — the filter is the whole point.
    for booking in body["bookings"]:
        assert booking["status"] == "completed"


@pytest.mark.asyncio
async def test_list_bookings_rejects_non_admin(client, auth_headers, db_session):
    """A provider looking at /admin/bookings would see every consumer's
    booking including OTPs — must 403."""
    provider = await UserFactory.create(db_session, role="provider")

    response = await client.get(
        "/api/v1/admin/bookings",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 403, response.text
