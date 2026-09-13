"""Coverage for the SVC-291 related-services surfaces.

Two shapes to protect:

  1. The API — `GET /services/{id}` returns three named buckets with the
     shape SVC-292's frontend renders (`hook`, `upsell`, `related`), and
     inactive related services never leak onto a rail.
  2. The seed script's ranking — same-sub-cat picks fill `upsell` then
     `related`; cross-sub-cat picks under the same L1 fill `hook`; a
     service under a leaf L1 (no children) has no `hook` picks by
     construction.

Both are load-bearing for Phase 2b — if the API's field names drift, the
frontend renders nothing; if the seed picks the wrong pool, the rails
render irrelevant services.
"""

from uuid import uuid4

import pytest

from app.models.category import Category
from app.models.service import Service
from app.models.service_relationship import RelationshipType, ServiceRelationship
from scripts.seed_service_relationships import _pick_edges


async def _seed_l1_with_two_l2s(db_session, suffix: str):
    """Build a small catalogue: one L1, two L2s under it, four services in
    L2-A and two in L2-B. Return the ids the tests need.

    Kept as a helper because every test in this module starts from the same
    shape — the interesting differences are what the assertions look at, not
    how the fixture is arranged.
    """
    l1 = Category(name=f"L1 {suffix}", slug=f"l1-{suffix}", is_active=True, sort_order=1)
    db_session.add(l1)
    await db_session.flush()

    l2_a = Category(
        name=f"L2A {suffix}",
        slug=f"l2a-{suffix}",
        is_active=True,
        sort_order=1,
        parent_id=l1.id,
    )
    l2_b = Category(
        name=f"L2B {suffix}",
        slug=f"l2b-{suffix}",
        is_active=True,
        sort_order=2,
        parent_id=l1.id,
    )
    db_session.add_all([l2_a, l2_b])
    await db_session.flush()

    # L2-A: source + three siblings, prices chosen so ranking is unambiguous.
    source = Service(
        category_id=l2_a.id,
        name=f"A-source {suffix}",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    sibling_hi = Service(
        category_id=l2_a.id,
        name=f"A-sibling-hi {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    sibling_mid = Service(
        category_id=l2_a.id,
        name=f"A-sibling-mid {suffix}",
        base_price=300.0,
        duration_minutes=60,
        is_active=True,
    )
    sibling_lo = Service(
        category_id=l2_a.id,
        name=f"A-sibling-lo {suffix}",
        base_price=200.0,
        duration_minutes=60,
        is_active=True,
    )
    # L2-B: two cross-domain services.
    cross_hi = Service(
        category_id=l2_b.id,
        name=f"B-cross-hi {suffix}",
        base_price=400.0,
        duration_minutes=60,
        is_active=True,
    )
    cross_lo = Service(
        category_id=l2_b.id,
        name=f"B-cross-lo {suffix}",
        base_price=150.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add_all([source, sibling_hi, sibling_mid, sibling_lo, cross_hi, cross_lo])
    await db_session.flush()

    return {
        "l1": l1,
        "l2_a": l2_a,
        "l2_b": l2_b,
        "source": source,
        "sibling_hi": sibling_hi,
        "sibling_mid": sibling_mid,
        "sibling_lo": sibling_lo,
        "cross_hi": cross_hi,
        "cross_lo": cross_lo,
    }


# ---------------------------------------------------------------------------
# GET /services/{id} — API shape + inactive filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_service_returns_three_related_buckets(client, db_session):
    """The endpoint groups edges by relationship_type into the three named
    buckets Phase 2b's frontend renders. Every edge type has at least one
    entry so the field is not just present-but-empty."""
    suffix = uuid4().hex[:6]
    ctx = await _seed_l1_with_two_l2s(db_session, suffix)
    source = ctx["source"]

    db_session.add_all(
        [
            ServiceRelationship(
                source_service_id=source.id,
                related_service_id=ctx["sibling_hi"].id,
                relationship_type=RelationshipType.UPSELL.value,
                sort_order=0,
            ),
            ServiceRelationship(
                source_service_id=source.id,
                related_service_id=ctx["sibling_lo"].id,
                relationship_type=RelationshipType.RELATED.value,
                sort_order=0,
            ),
            ServiceRelationship(
                source_service_id=source.id,
                related_service_id=ctx["cross_hi"].id,
                relationship_type=RelationshipType.HOOK.value,
                sort_order=0,
            ),
        ]
    )
    await db_session.flush()

    response = await client.get(f"/api/v1/services/{source.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(source.id)
    related = body["related"]
    assert {"hook", "upsell", "related"} <= related.keys()
    assert [r["id"] for r in related["upsell"]] == [str(ctx["sibling_hi"].id)]
    assert [r["id"] for r in related["related"]] == [str(ctx["sibling_lo"].id)]
    assert [r["id"] for r in related["hook"]] == [str(ctx["cross_hi"].id)]
    # Prefetched fields — the frontend renders the rail without a second
    # request; assert the shape is complete.
    upsell_entry = related["upsell"][0]
    assert upsell_entry["name"] == ctx["sibling_hi"].name
    assert upsell_entry["base_price"] == 500.0


@pytest.mark.asyncio
async def test_get_service_hides_inactive_related(client, db_session):
    """A soft-deleted related service must not surface on any rail — same
    invariant as list_services which excludes is_active=False from the
    catalogue."""
    suffix = uuid4().hex[:6]
    ctx = await _seed_l1_with_two_l2s(db_session, suffix)

    ctx["sibling_hi"].is_active = False
    db_session.add(
        ServiceRelationship(
            source_service_id=ctx["source"].id,
            related_service_id=ctx["sibling_hi"].id,
            relationship_type=RelationshipType.UPSELL.value,
            sort_order=0,
        )
    )
    await db_session.flush()

    response = await client.get(f"/api/v1/services/{ctx['source'].id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["related"]["upsell"] == []


@pytest.mark.asyncio
async def test_get_service_404_when_missing(client, db_session):
    """A cold hit on an id that isn't in the catalogue is a 404 — same
    class as the existing ServiceNotFound path on PATCH / DELETE."""
    response = await client.get(f"/api/v1/services/{uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Seed ranking — pure function, no DB assertions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_upsell_picks_same_l2_top_by_price(db_session):
    """`upsell` picks the top N SAME-sub-cat services by price DESC."""
    suffix = uuid4().hex[:6]
    ctx = await _seed_l1_with_two_l2s(db_session, suffix)
    services = [
        ctx["source"],
        ctx["sibling_hi"],
        ctx["sibling_mid"],
        ctx["sibling_lo"],
        ctx["cross_hi"],
    ]
    parent_of = {ctx["l2_a"].id: ctx["l1"].id, ctx["l2_b"].id: ctx["l1"].id, ctx["l1"].id: None}

    edges = _pick_edges(ctx["source"], services, parent_of, n_upsell=2, n_related=0, n_hook=0)

    upsell = [e for e in edges if e[1] == RelationshipType.UPSELL.value]
    assert [e[0] for e in upsell] == [ctx["sibling_hi"].id, ctx["sibling_mid"].id]
    # Sort order is 0-based and matches the pick order.
    assert [e[2] for e in upsell] == [0, 1]


@pytest.mark.asyncio
async def test_seed_related_continues_where_upsell_left_off(db_session):
    """`related` picks up the same-sub-cat rank list after upsell's slice —
    no duplicate rows between the two buckets."""
    suffix = uuid4().hex[:6]
    ctx = await _seed_l1_with_two_l2s(db_session, suffix)
    services = [
        ctx["source"],
        ctx["sibling_hi"],
        ctx["sibling_mid"],
        ctx["sibling_lo"],
        ctx["cross_hi"],
    ]
    parent_of = {ctx["l2_a"].id: ctx["l1"].id, ctx["l2_b"].id: ctx["l1"].id, ctx["l1"].id: None}

    edges = _pick_edges(ctx["source"], services, parent_of, n_upsell=1, n_related=2, n_hook=0)

    by_type: dict[str, list] = {"upsell": [], "related": []}
    for rel_id, rel_type, _ in edges:
        if rel_type in by_type:
            by_type[rel_type].append(rel_id)
    assert by_type["upsell"] == [ctx["sibling_hi"].id]
    assert by_type["related"] == [ctx["sibling_mid"].id, ctx["sibling_lo"].id]
    # Zero overlap between the two buckets — a service must not appear in
    # both, or a rail renders it twice.
    assert set(by_type["upsell"]).isdisjoint(by_type["related"])


@pytest.mark.asyncio
async def test_seed_hook_picks_from_other_l2s_under_same_l1(db_session):
    """`hook` picks the top N services from OTHER sub-categories under the
    SAME L1 — never from the source's own sub-cat."""
    suffix = uuid4().hex[:6]
    ctx = await _seed_l1_with_two_l2s(db_session, suffix)
    services = [
        ctx["source"],
        ctx["sibling_hi"],
        ctx["cross_hi"],
        ctx["cross_lo"],
    ]
    parent_of = {ctx["l2_a"].id: ctx["l1"].id, ctx["l2_b"].id: ctx["l1"].id, ctx["l1"].id: None}

    edges = _pick_edges(ctx["source"], services, parent_of, n_upsell=0, n_related=0, n_hook=2)

    hook_ids = [e[0] for e in edges if e[1] == RelationshipType.HOOK.value]
    # Order is price DESC — cross_hi (400) before cross_lo (150).
    assert hook_ids == [ctx["cross_hi"].id, ctx["cross_lo"].id]
    # And crucially: no same-sub-cat services in the hook bucket.
    assert ctx["sibling_hi"].id not in hook_ids


@pytest.mark.asyncio
async def test_seed_leaf_l1_service_has_no_hook_rows(db_session):
    """A service under an L1 that has no children (Miscellaneous) can't
    have "other sub-cats under the same L1" — the algorithm produces zero
    hook rows for it rather than inventing cross-L1 picks."""
    suffix = uuid4().hex[:6]
    l1 = Category(
        name=f"Solo L1 {suffix}",
        slug=f"solo-l1-{suffix}",
        is_active=True,
        sort_order=1,
    )
    db_session.add(l1)
    await db_session.flush()
    source = Service(
        category_id=l1.id,
        name=f"Leaf svc {suffix}",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    peer = Service(
        category_id=l1.id,
        name=f"Peer svc {suffix}",
        base_price=200.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add_all([source, peer])
    await db_session.flush()

    parent_of = {l1.id: None}
    edges = _pick_edges(source, [source, peer], parent_of, n_upsell=1, n_related=0, n_hook=3)

    # The source's L1 IS its category, so `parent_of[source.category_id]` is
    # None and the hook branch is skipped by construction.
    assert all(e[1] != RelationshipType.HOOK.value for e in edges)
    # But upsell still fires because `peer` is in the same category as source.
    assert [e[0] for e in edges if e[1] == RelationshipType.UPSELL.value] == [peer.id]
