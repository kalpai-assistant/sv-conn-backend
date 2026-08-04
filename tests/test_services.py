"""Catalog router coverage — public listing + admin-only CRUD.

app/routers/services.py has zero direct tests. The read endpoints are
consumer-facing (every home page load hits them), and the write endpoints
are the ONLY way the catalog changes — a lapsed admin gate would let any
consumer add / delete services.
"""

from uuid import uuid4

import pytest

from app.models.category import Category
from app.models.service import Service
from tests.factories.user_factory import UserFactory

# ---------------------------------------------------------------------------
# GET /categories + GET /services (public)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_categories_is_public(client, db_session):
    """No auth header — should still work. Categories are the entry point
    on the consumer home page before login."""
    suffix = uuid4().hex[:8]
    cat = Category(
        name=f"Cleaning {suffix}",
        slug=f"cleaning-{suffix}",
        is_active=True,
        sort_order=1,
    )
    db_session.add(cat)
    await db_session.flush()

    response = await client.get("/api/v1/categories")

    assert response.status_code == 200, response.text
    slugs = [c["slug"] for c in response.json()]
    assert f"cleaning-{suffix}" in slugs


@pytest.mark.asyncio
async def test_list_categories_hides_inactive(client, db_session):
    """Soft-deleted (is_active=False) categories must not appear in the
    public listing — that's the whole point of soft delete."""
    suffix = uuid4().hex[:8]
    hidden = Category(
        name=f"Retired {suffix}",
        slug=f"retired-{suffix}",
        is_active=False,
        sort_order=99,
    )
    db_session.add(hidden)
    await db_session.flush()

    response = await client.get("/api/v1/categories")

    assert response.status_code == 200, response.text
    slugs = [c["slug"] for c in response.json()]
    assert f"retired-{suffix}" not in slugs


@pytest.mark.asyncio
async def test_list_services_filters_by_category(client, db_session):
    """?category_id=X returns only services in X. Consumer app uses this
    when the user taps a category chip."""
    suffix = uuid4().hex[:8]
    cat_a = Category(name=f"A {suffix}", slug=f"a-{suffix}", is_active=True, sort_order=1)
    cat_b = Category(name=f"B {suffix}", slug=f"b-{suffix}", is_active=True, sort_order=2)
    db_session.add_all([cat_a, cat_b])
    await db_session.flush()

    svc_a = Service(
        category_id=cat_a.id,
        name=f"In-A {suffix}",
        base_price=100.0,
        duration_minutes=30,
        is_active=True,
    )
    svc_b = Service(
        category_id=cat_b.id,
        name=f"In-B {suffix}",
        base_price=200.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add_all([svc_a, svc_b])
    await db_session.flush()

    response = await client.get(f"/api/v1/services?category_id={cat_a.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    names = [s["name"] for s in body]
    assert f"In-A {suffix}" in names
    assert f"In-B {suffix}" not in names


# ---------------------------------------------------------------------------
# POST /services (admin-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_service_as_admin(client, auth_headers, db_session):
    admin = await UserFactory.create(db_session, role="admin")
    suffix = uuid4().hex[:8]
    cat = Category(name=f"Cat {suffix}", slug=f"cat-{suffix}", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()

    response = await client.post(
        "/api/v1/services",
        json={
            "category_id": str(cat.id),
            "name": "Deep Clean",
            "base_price": 500.0,
            "duration_minutes": 90,
            "description": "3-hour thorough clean",
        },
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Deep Clean"
    assert body["base_price"] == 500.0


@pytest.mark.asyncio
async def test_create_service_rejects_non_admin(client, auth_headers, db_session):
    """The whole reason the endpoint has an admin gate: a consumer must
    not be able to add themselves as a service provider, or spam the
    catalog with junk."""
    consumer = await UserFactory.create(db_session, role="consumer")

    response = await client.post(
        "/api/v1/services",
        json={
            "category_id": str(uuid4()),
            "name": "Fake Service",
            "base_price": 1.0,
            "duration_minutes": 5,
        },
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# PATCH /services/{id} + DELETE /services/{id} (admin-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_service_rejects_non_admin(client, auth_headers, db_session):
    """A provider changing service prices at will would let them
    price-gouge any booking that quotes off the catalog."""
    provider = await UserFactory.create(db_session, role="provider")

    response = await client.patch(
        f"/api/v1/services/{uuid4()}",
        json={"base_price": 999999.0},
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_delete_service_as_admin_soft_deletes(client, auth_headers, db_session):
    """Admin DELETE flips is_active=False (soft delete, preserves booking
    FKs). The service disappears from public GET /services after."""
    admin = await UserFactory.create(db_session, role="admin")
    suffix = uuid4().hex[:8]
    cat = Category(name=f"Cat {suffix}", slug=f"cat-{suffix}", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name=f"Soon-gone {suffix}",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(svc)
    await db_session.flush()

    response = await client.delete(
        f"/api/v1/services/{svc.id}",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 204, response.text

    # After soft-delete: not in the public listing.
    listing = await client.get(f"/api/v1/services?category_id={cat.id}")
    names = [s["name"] for s in listing.json()]
    assert f"Soon-gone {suffix}" not in names

    # But the row itself still exists (soft delete, not row removal) — this
    # matters because bookings that reference the service via FK must not
    # dangle.
    from app.models.service import Service as ServiceModel

    still_there = await db_session.get(ServiceModel, svc.id)
    assert still_there is not None
    assert still_there.is_active is False


@pytest.mark.asyncio
async def test_delete_service_rejects_non_admin(client, auth_headers, db_session):
    consumer = await UserFactory.create(db_session, role="consumer")

    response = await client.delete(
        f"/api/v1/services/{uuid4()}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 403, response.text
