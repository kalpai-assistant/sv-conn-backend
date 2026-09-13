"""Coverage for the SVC-293 banner URL columns.

Both columns are nullable and additive — the invariant we protect is that
the API round-trips them accurately: NULL when unset, the stored URL when
set, and they appear on both the list route and the detail route. Frontend's
5-state fallback chain reads a missing / null field as "fall through," so a
mistakenly-omitted field in the response would silently downgrade every
service with curated media to the sub-category banner.
"""

from uuid import uuid4

import pytest

from app.models.category import Category
from app.models.service import Service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# GET /services/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_service_detail_returns_banner_urls_when_set(client, db_session):
    """When the columns carry URLs, the detail endpoint returns them verbatim —
    the frontend embeds these strings directly in `<img src>` / `<video src>`,
    so any transformation here would silently break media rendering."""
    service = await _make_service(
        db_session,
        banner_image_url="https://cdn.eazyserv.com/services-media/abc/banner.webp",
        banner_video_url="https://cdn.eazyserv.com/services-media/abc/banner.mp4",
    )

    response = await client.get(f"/api/v1/services/{service.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["banner_image_url"] == "https://cdn.eazyserv.com/services-media/abc/banner.webp"
    assert body["banner_video_url"] == "https://cdn.eazyserv.com/services-media/abc/banner.mp4"


@pytest.mark.asyncio
async def test_get_service_detail_returns_null_when_unset(client, db_session):
    """The default state — no curated media. Both fields must be present-and-null
    (not missing), so the frontend's `?? null` fallback path resolves correctly
    across every service, whether or not it has media."""
    service = await _make_service(db_session)

    response = await client.get(f"/api/v1/services/{service.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "banner_image_url" in body
    assert "banner_video_url" in body
    assert body["banner_image_url"] is None
    assert body["banner_video_url"] is None


# ---------------------------------------------------------------------------
# GET /services (list) — same fields must appear on the list route too, so a
# preloaded home doesn't have to hit the per-service endpoint for every rail
# entry to know whether it has media.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_services_carries_banner_urls(client, db_session):
    """Rails on Home should be able to decide "show video vs image vs
    fallback" from the already-cached list. If this field were only on the
    detail route, every rail would have to fetch every service to know."""
    service = await _make_service(
        db_session,
        banner_image_url="https://cdn.eazyserv.com/services-media/xyz/banner.webp",
    )

    response = await client.get(f"/api/v1/services?category_id={service.category_id}")

    assert response.status_code == 200, response.text
    matched = [s for s in response.json() if s["id"] == str(service.id)]
    assert matched, "seeded service missing from list response"
    row = matched[0]
    assert row["banner_image_url"] == "https://cdn.eazyserv.com/services-media/xyz/banner.webp"
    assert row["banner_video_url"] is None
