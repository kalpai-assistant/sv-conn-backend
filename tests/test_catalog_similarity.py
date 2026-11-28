"""SVC-437: duplicate detection (pg_trgm) and ``POST /catalog/similarity``.

Every test runs on real Postgres with pg_trgm. Each one makes its own
categories and passes ``category_id``, so rows other tests leave behind can't
change the result.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.provider_profile import ProviderProfile
from app.models.service import Service
from app.models.service_request import ServiceRequest, ServiceRequestStatus
from app.models.user import User
from app.services.catalog_similarity import (
    CompositeSimilarityDetector,
    PendingRequestOverlapDetector,
    TrgmSimilarityDetector,
)
from tests.factories.user_factory import UserFactory

URL = "/api/v1/catalog/similarity"


def _detector(db: AsyncSession) -> CompositeSimilarityDetector:
    return CompositeSimilarityDetector(
        [TrgmSimilarityDetector(db), PendingRequestOverlapDetector(db)]
    )


async def _category(db: AsyncSession, parent: Category | None = None) -> Category:
    slug = f"svc437-{uuid.uuid4().hex[:10]}"
    category = Category(name=slug, slug=slug, parent_id=parent.id if parent else None)
    db.add(category)
    await db.flush()
    return category


async def _service(
    db: AsyncSession,
    category: Category,
    name: str,
    description: str | None = None,
    is_active: bool = True,
) -> Service:
    service = Service(
        category_id=category.id,
        name=name,
        description=description,
        base_price=Decimal("499.00"),
        is_active=is_active,
    )
    db.add(service)
    await db.flush()
    return service


async def _sp(db: AsyncSession) -> User:
    user = await UserFactory.create(db, role="provider")
    db.add(ProviderProfile(user_id=user.id))
    await db.flush()
    return user


async def _request(
    db: AsyncSession,
    sp: User,
    category: Category,
    name: str,
    status: ServiceRequestStatus = ServiceRequestStatus.PENDING,
    description: str = "A secret description from the SP.",
) -> ServiceRequest:
    request = ServiceRequest(
        sp_id=sp.id,
        proposed_name=name,
        proposed_description=description,
        proposed_category_id=category.id,
        proposed_price=Decimal("777.00"),
        proposed_price_unit="PER_VISIT",
        status=status.value,
    )
    db.add(request)
    await db.flush()
    return request


async def _similarity(db: AsyncSession, a: str, b: str) -> float:
    return float(await db.scalar(select(func.similarity(a, b))))


# -- pg_trgm pairs (ticket correction 1) ---------------------------------------


async def test_reordered_words_match_above_half(db_session):
    """FAILURE CAUGHT: a threshold or weight change (or a swap to word_similarity)
    stops "Clean windows" matching "Window cleaning", the ticket's headline case."""
    category = await _category(db_session)
    service = await _service(db_session, category, "Window cleaning")

    hits = await _detector(db_session).find_similar("Clean windows", category_id=category.id)

    assert [h.service_id for h in hits] == [service.id]
    assert hits[0].score > 0.5
    assert hits[0].source == "catalog"
    assert hits[0].category_name == category.name


async def test_unrelated_service_is_not_returned(db_session):
    """FAILURE CAUGHT: loosened thresholds (or an always-true OR) flood the SP's
    suggestions with unrelated services."""
    category = await _category(db_session)
    await _service(db_session, category, "AC repair")

    assert await _similarity(db_session, "Sofa cleaning", "AC repair") < 0.3
    assert await _detector(db_session).find_similar("Sofa cleaning", category_id=category.id) == []


async def test_known_trgm_limit_deep_cleaning_matches_ac_cleaning(db_session):
    """FAILURE CAUGHT: someone "fixes" the ticket's impossible negative case by
    tuning thresholds until this pair drops out, which breaks real matches.

    This is pg_trgm's known limit: the shared word "cleaning" scores about 0.53.
    The embeddings implementation should flip this test to "not returned"."""
    category = await _category(db_session)
    service = await _service(db_session, category, "AC cleaning")

    hits = await _detector(db_session).find_similar("Deep cleaning", category_id=category.id)

    assert [h.service_id for h in hits] == [service.id]
    assert hits[0].score == pytest.approx(0.53, abs=0.01)


# -- pending-request overlap ----------------------------------------------------


async def test_pending_shows_other_sps_request_only(db_session):
    """FAILURE CAUGHT: the pending detector dropped from the composite, `sp_id !=`
    flipped to `=`, or the status filter lost, so the caller sees its own request
    or resolved ones as duplicates."""
    category = await _category(db_session)
    caller, other = await _sp(db_session), await _sp(db_session)
    wanted = await _request(db_session, other, category, "Balcony grille cleaning")
    await _request(db_session, caller, category, "Balcony grille cleaning")
    for status in (
        ServiceRequestStatus.APPROVED,
        ServiceRequestStatus.REJECTED_WITH_REASON,
        ServiceRequestStatus.REJECTED_WITH_RECOMMENDATION,
    ):
        await _request(db_session, other, category, "Balcony grille cleaning", status)

    hits = await _detector(db_session).find_similar(
        "Balcony grille cleaning", category_id=category.id, requesting_sp_id=caller.id
    )

    assert [(h.request_id, h.service_id, h.source) for h in hits] == [
        (wanted.id, None, "pending_request")
    ]
    assert hits[0].service_name == "Balcony grille cleaning"
    assert hits[0].category_name == category.name


async def test_pending_with_no_requesting_sp_includes_every_pending(db_session):
    """FAILURE CAUGHT: `requesting_sp_id=None` building `sp_id != NULL`, which
    matches nothing, so the future admin caller silently gets no pending hits."""
    category = await _category(db_session)
    a, b = await _sp(db_session), await _sp(db_session)
    ra = await _request(db_session, a, category, "Chimney deep cleaning")
    rb = await _request(db_session, b, category, "Chimney deep cleaning")

    hits = await _detector(db_session).find_similar(
        "Chimney deep cleaning", category_id=category.id
    )

    assert {h.request_id for h in hits} == {ra.id, rb.id}


# -- filters --------------------------------------------------------------------


async def test_category_filter_l1_includes_children_l2_excludes_siblings(db_session):
    """FAILURE CAUGHT: an equality-only filter makes an L1 tile return nothing
    (360/363 beta services hang off an L2), or a widened filter leaks siblings.
    Checked for both detectors."""
    l1 = await _category(db_session)
    l2, sibling = await _category(db_session, l1), await _category(db_session, l1)
    in_l2 = await _service(db_session, l2, "Geyser installation")
    in_sibling = await _service(db_session, sibling, "Geyser installation")
    in_l1 = await _service(db_session, l1, "Geyser installation")
    other_sp = await _sp(db_session)
    pending_l2 = await _request(db_session, other_sp, l2, "Geyser installation")
    detector = _detector(db_session)

    by_l1 = await detector.find_similar("Geyser installation", category_id=l1.id, limit=10)
    by_l2 = await detector.find_similar("Geyser installation", category_id=l2.id, limit=10)

    assert {h.service_id for h in by_l1 if h.service_id} == {in_l2.id, in_sibling.id, in_l1.id}
    assert {h.request_id for h in by_l1 if h.request_id} == {pending_l2.id}
    assert {h.service_id or h.request_id for h in by_l2} == {in_l2.id, pending_l2.id}


async def test_inactive_service_is_not_returned(db_session):
    """FAILURE CAUGHT: the `is_active` filter lost, so soft-deleted services are
    suggested as duplicates."""
    category = await _category(db_session)
    active = await _service(db_session, category, "Water tank cleaning")
    await _service(db_session, category, "Water tank cleaning", is_active=False)

    hits = await _detector(db_session).find_similar("Water tank cleaning", category_id=category.id)

    assert [h.service_id for h in hits] == [active.id]


async def test_composite_merges_by_score_and_caps_overall_limit(db_session):
    """FAILURE CAUGHT: `limit` applied per detector (up to 2x limit returned), or
    the two lists concatenated without sorting by score."""
    category = await _category(db_session)
    other_sp = await _sp(db_session)
    exact = await _service(db_session, category, "Kitchen sink repair")
    pending = await _request(db_session, other_sp, category, "Kitchen sink repairs")
    await _service(db_session, category, "Kitchen tap repair")
    await _request(db_session, other_sp, category, "Bathroom sink repair")

    hits = await _detector(db_session).find_similar(
        "Kitchen sink repair", category_id=category.id, limit=2
    )

    assert len(hits) == 2
    assert [h.service_id or h.request_id for h in hits] == [exact.id, pending.id]
    assert hits[0].score >= hits[1].score


async def test_description_match_scores_at_weight(db_session):
    """FAILURE CAUGHT: the description weight dropped (a description-only match
    would outrank a name match) or the description threshold lost."""
    category = await _category(db_session)
    described = await _service(
        db_session,
        category,
        "Pest control",
        description="Cockroach and ant treatment for the whole kitchen",
    )

    hits = await _detector(db_session).find_similar(
        "Insect removal",
        "Cockroach and ant treatment for the whole kitchen",
        category_id=category.id,
    )

    assert [h.service_id for h in hits] == [described.id]
    assert hits[0].score == pytest.approx(0.6)


# -- endpoint -------------------------------------------------------------------


async def test_endpoint_hides_pending_owner_description_and_price(client, db_session, auth_headers):
    """FAILURE CAUGHT: a pending hit leaking the other SP's id, description or
    price over HTTP. Also checks the hit shape the SP app reads."""
    category = await _category(db_session)
    caller, other = await _sp(db_session), await _sp(db_session)
    request = await _request(db_session, other, category, "Sofa shampooing")

    response = await client.post(
        URL,
        json={"proposed_name": "Sofa shampooing", "category_id": str(category.id)},
        headers=auth_headers(str(caller.id), "provider"),
    )

    assert response.status_code == 200
    [hit] = response.json()
    assert hit == {
        "service_id": None,
        "request_id": str(request.id),
        "service_name": "Sofa shampooing",
        "category_name": category.name,
        "score": pytest.approx(1.0),
        "source": "pending_request",
    }
    body = response.text
    assert str(other.id) not in body
    assert "secret description" not in body
    # "777.0", not "777": random hex in an id or slug can contain "777".
    assert "777.0" not in body


async def test_endpoint_short_name_returns_empty_list(client, db_session, auth_headers):
    """FAILURE CAUGHT: a 1-2 character name 422s (breaking type-ahead) or runs a
    query, which here would return the 3-letter service below."""
    category = await _category(db_session)
    await _service(db_session, category, "AC")
    caller = await _sp(db_session)

    response = await client.post(
        URL,
        json={"proposed_name": " AC ", "category_id": str(category.id)},
        headers=auth_headers(str(caller.id), "provider"),
    )

    assert response.status_code == 200
    assert response.json() == []


async def test_endpoint_requires_token_and_provider_role(
    client, db_session, redis_client, auth_headers
):
    """FAILURE CAUGHT: the auth or role dependency dropped, so anyone can probe
    other SPs' pending requests; or the rate limit runs before the role check,
    so a consumer's refused calls still burn a bucket."""
    consumer = await UserFactory.create(db_session, role="consumer")
    body = {"proposed_name": "Window cleaning"}

    assert (await client.post(URL, json=body)).status_code == 401
    response = await client.post(URL, json=body, headers=auth_headers(str(consumer.id)))
    assert response.status_code == 403
    assert await redis_client.exists(f"rl:catalog_similarity:{consumer.id}") == 0


async def test_endpoint_31st_call_in_a_minute_is_429(
    client, db_session, redis_client, auth_headers
):
    """FAILURE CAUGHT: the rate limit lost, or left at the 3600 s default window
    (the TTL check is what tells the two apart)."""
    caller = await _sp(db_session)
    headers = auth_headers(str(caller.id), "provider")
    body = {"proposed_name": "ab"}  # short: no DB work, only the limit

    for _ in range(30):
        assert (await client.post(URL, json=body, headers=headers)).status_code == 200
    response = await client.post(URL, json=body, headers=headers)

    assert response.status_code == 429
    assert 0 < await redis_client.ttl(f"rl:catalog_similarity:{caller.id}") <= 60


async def test_endpoint_refuses_description_over_the_submit_cap(client, db_session, auth_headers):
    """FAILURE CAUGHT: the description cap dropped, so one SP can send megabytes
    that every row's similarity() re-scans, 30 times a minute."""
    from app.schemas.service_request import DESCRIPTION_MAX

    caller = await _sp(db_session)
    headers = auth_headers(str(caller.id), "provider")
    at_cap = {"proposed_name": "ab", "proposed_description": "x" * DESCRIPTION_MAX}
    over = {**at_cap, "proposed_description": "x" * (DESCRIPTION_MAX + 1)}

    assert (await client.post(URL, json=at_cap, headers=headers)).status_code == 200
    assert (await client.post(URL, json=over, headers=headers)).status_code == 422
