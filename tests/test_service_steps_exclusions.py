"""Coverage for the SVC-340 steps + exclusions columns.

Both are nullable JSONB lists, shown only on the service detail page. The
invariants: the detail route returns them in order when set and present-and-null
when unset (the frontend hides the section on null), and the list route leaves
them out so the home-page payload stays lean.
"""

from uuid import uuid4

import pytest

from app.models.category import Category
from app.models.service import Service


async def _make_service(db_session, **overrides):
    suffix = uuid4().hex[:6]
    cat = Category(name=f"Cat {suffix}", slug=f"cat-{suffix}", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    service = Service(
        category_id=cat.id,
        name=f"Svc {suffix}",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
        **overrides,
    )
    db_session.add(service)
    await db_session.flush()
    return service


@pytest.mark.asyncio
async def test_get_service_detail_returns_steps_and_exclusions_in_order(client, db_session):
    steps = ["Arrives with tools", "Inspects the area", "Does the work", "Cleans up"]
    exclusions = ["Does not include spare parts", "Does not include civil work"]
    service = await _make_service(db_session, steps=steps, exclusions=exclusions)

    response = await client.get(f"/api/v1/services/{service.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["steps"] == steps
    assert body["exclusions"] == exclusions


@pytest.mark.asyncio
async def test_get_service_detail_returns_null_when_unset(client, db_session):
    """Present-and-null, not missing: null is how the frontend knows to hide
    the section."""
    service = await _make_service(db_session)

    response = await client.get(f"/api/v1/services/{service.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "steps" in body and body["steps"] is None
    assert "exclusions" in body and body["exclusions"] is None


@pytest.mark.asyncio
async def test_list_services_leaves_out_steps_and_exclusions(client, db_session):
    service = await _make_service(
        db_session, steps=["Arrives with tools"], exclusions=["Does not include parts"]
    )

    response = await client.get(f"/api/v1/services?category_id={service.category_id}")

    assert response.status_code == 200, response.text
    matched = [s for s in response.json() if s["id"] == str(service.id)]
    assert matched, "seeded service missing from list response"
    assert "steps" not in matched[0]
    assert "exclusions" not in matched[0]
