"""
SVC-428: the admin pricing config surface.

- ``GET /admin/pricing/categories``: every category with the six fields, each L1
  followed by its L2s, inactive rows included;
- ``PATCH /admin/pricing/categories/{id}``: partial, validated against the row as
  it would be saved, one audit row holding only the changed fields;
- ``GET`` / ``PATCH /admin/pricing/config``: the four keys, weights summing to 1;
- a refused value is a 422 ``PRICING_INVALID`` naming the field and changes nothing;
- a save that changes nothing writes no audit row;
- admin and ops edit, support reads;
- the public ``GET /categories`` shape is unchanged.

The suite's schema comes from ``create_all``, so the migration's seed rows aren't
here: ``seed_config`` writes them inside the test's own transaction. Category
reads are scoped to this test's rows, since other files commit categories.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.admin_action import AdminAction
from app.models.category import Category
from app.models.pricing_config import PricingConfig
from app.models.user import AdminRole
from tests.test_admin_console_auth import _admin, _session

A = "/api/v1/admin/pricing"
ERROR_CODE = "PRICING_INVALID"
WEIGHTS = {
    "rating": 0.35,
    "price_vs_ref": 0.25,
    "distance": 0.20,
    "reliability": 0.15,
    "activity": 0.05,
}
SEED_CONFIG = {
    "ranking_weights": WEIGHTS,
    "boost_window_days": 14,
    "boost_window_jobs": 10,
    "sheet_ttl_seconds": 600,
}
CATEGORY_ROW_FIELDS = {
    "id",
    "name",
    "slug",
    "parent_id",
    "parent_name",
    "sort_order",
    "is_active",
    "is_sensitive",
    "reference_price",
    "bounds_pct",
    "platform_commission_pct",
    "platform_commission_min",
    "commission_gst_pct",
}
SEEDED_AT = datetime(2026, 11, 1, tzinfo=UTC)


async def seed_config(db) -> None:
    """The migration's four rows, as of ``SEEDED_AT``."""
    for key, value in SEED_CONFIG.items():
        await db.merge(
            PricingConfig(key=key, value_json=value, updated_at=SEEDED_AT, updated_by=None)
        )
    await db.flush()


async def category(db, *, parent: Category | None = None, **fields) -> Category:
    tag = uuid.uuid4().hex[:10]
    row = Category(
        name=fields.pop("name", f"Pricing {tag}"),
        slug=f"pricing-{tag}",
        is_active=fields.pop("is_active", True),
        sort_order=fields.pop("sort_order", 0),
        parent_id=parent.id if parent else None,
        **fields,
    )
    db.add(row)
    await db.flush()
    return row


async def _rows(db, admin) -> list[AdminAction]:
    return list(
        (await db.scalars(select(AdminAction).where(AdminAction.admin_user_id == admin.id))).all()
    )


def _assert_refused(r, field: str) -> None:
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == ERROR_CODE
    assert body["details"]["field"] == field
    assert body["message"]


@pytest.fixture
async def admin(db_session):
    return await _admin(db_session)


@pytest.fixture
async def ops(db_session):
    return await _admin(db_session, AdminRole.OPS)


@pytest.fixture
async def support(db_session):
    return await _admin(db_session, AdminRole.SUPPORT)


# ---------------------------------------------------------------------------
# GET /admin/pricing/categories
# ---------------------------------------------------------------------------


async def test_categories_list_groups_l2_under_l1_with_every_field(client, db_session, support):
    first = await category(db_session, sort_order=900, name="Pricing A first")
    second = await category(db_session, sort_order=901, name="Pricing B second")
    b_child = await category(db_session, parent=second, sort_order=0)
    a_late = await category(db_session, parent=first, sort_order=5, is_active=False)
    a_early = await category(
        db_session,
        parent=first,
        sort_order=1,
        is_sensitive=True,
        bounds_pct=Decimal("25.00"),
        reference_price=Decimal("499.50"),
    )
    mine = {c.id for c in (first, second, b_child, a_late, a_early)}

    r = await client.get(f"{A}/categories", headers=_session(support))
    assert r.status_code == 200, r.text
    rows = [row for row in r.json() if uuid.UUID(row["id"]) in mine]

    assert [uuid.UUID(row["id"]) for row in rows] == [
        first.id,
        a_early.id,
        a_late.id,
        second.id,
        b_child.id,
    ]
    assert all(set(row) == CATEGORY_ROW_FIELDS for row in rows)
    l1, early, late = rows[0], rows[1], rows[2]
    assert (l1["parent_id"], l1["parent_name"]) == (None, None)
    assert (early["parent_id"], early["parent_name"]) == (str(first.id), "Pricing A first")
    assert late["is_active"] is False
    # Numbers, not Decimal strings.
    assert early["reference_price"] == 499.5
    assert early["bounds_pct"] == 25.0
    assert (early["is_sensitive"], l1["is_sensitive"]) == (True, False)
    assert (
        l1["platform_commission_pct"],
        l1["platform_commission_min"],
        l1["commission_gst_pct"],
    ) == (20.0, 30.0, 18.0)
    assert (l1["reference_price"], l1["bounds_pct"]) == (None, None)


async def test_public_categories_shape_is_unchanged(client, db_session):
    cat = await category(db_session, is_sensitive=True, bounds_pct=Decimal("20"))
    r = await client.get("/api/v1/categories")
    assert r.status_code == 200, r.text
    [row] = [row for row in r.json() if row["id"] == str(cat.id)]
    assert set(row) == {
        "id",
        "name",
        "slug",
        "icon_url",
        # SVC-430 adds the icon key; the pricing columns still stay off it.
        "icon_key",
        "description",
        "sort_order",
        "parent_id",
    }


# ---------------------------------------------------------------------------
# PATCH /admin/pricing/categories/{id}
# ---------------------------------------------------------------------------


async def test_one_cell_save_writes_one_audit_row_with_only_that_field(client, db_session, admin):
    parent = await category(db_session, name=f"Pricing parent {uuid.uuid4().hex[:6]}")
    cat = await category(db_session, parent=parent)

    r = await client.patch(
        f"{A}/categories/{cat.id}",
        json={"platform_commission_pct": 22.5},
        headers=_session(admin),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform_commission_pct"] == 22.5
    assert body["parent_name"] == parent.name
    assert set(body) == CATEGORY_ROW_FIELDS

    await db_session.refresh(cat)
    assert cat.platform_commission_pct == Decimal("22.50")
    [row] = await _rows(db_session, admin)
    assert (row.action, row.target_type, row.target_id) == (
        "pricing.category_update",
        "category",
        str(cat.id),
    )
    assert row.before_json == {"platform_commission_pct": 20.0}
    assert row.after_json == {"platform_commission_pct": 22.5}


async def test_sensitive_on_without_bounds_is_refused_and_changes_nothing(
    client, db_session, admin
):
    cat = await category(db_session)
    r = await client.patch(
        f"{A}/categories/{cat.id}", json={"is_sensitive": True}, headers=_session(admin)
    )
    _assert_refused(r, "bounds_pct")
    await db_session.refresh(cat)
    assert cat.is_sensitive is False
    assert await _rows(db_session, admin) == []


async def test_sensitive_on_with_bounds_in_one_save(client, db_session, admin):
    cat = await category(db_session)
    r = await client.patch(
        f"{A}/categories/{cat.id}",
        json={"is_sensitive": True, "bounds_pct": 25},
        headers=_session(admin),
    )
    assert r.status_code == 200, r.text
    assert (r.json()["is_sensitive"], r.json()["bounds_pct"]) == (True, 25.0)
    [row] = await _rows(db_session, admin)
    assert row.before_json == {"is_sensitive": False, "bounds_pct": None}
    assert row.after_json == {"is_sensitive": True, "bounds_pct": 25.0}


async def test_bounds_alone_on_a_sensitive_row_is_a_plain_edit(client, db_session, admin):
    cat = await category(db_session, is_sensitive=True, bounds_pct=Decimal("20"))
    r = await client.patch(
        f"{A}/categories/{cat.id}", json={"bounds_pct": 30.25}, headers=_session(admin)
    )
    assert r.status_code == 200, r.text
    [row] = await _rows(db_session, admin)
    assert (row.before_json, row.after_json) == ({"bounds_pct": 20.0}, {"bounds_pct": 30.25})


async def test_sensitive_off_nulls_the_bounds(client, db_session, admin):
    cat = await category(db_session, is_sensitive=True, bounds_pct=Decimal("25"))
    r = await client.patch(
        f"{A}/categories/{cat.id}", json={"is_sensitive": False}, headers=_session(admin)
    )
    assert r.status_code == 200, r.text
    assert (r.json()["is_sensitive"], r.json()["bounds_pct"]) == (False, None)
    await db_session.refresh(cat)
    assert (cat.is_sensitive, cat.bounds_pct) == (False, None)
    [row] = await _rows(db_session, admin)
    assert row.before_json == {"is_sensitive": True, "bounds_pct": 25.0}
    assert row.after_json == {"is_sensitive": False, "bounds_pct": None}


async def test_bounds_on_a_row_that_is_not_sensitive_is_refused(client, db_session, admin):
    cat = await category(db_session)
    for body in ({"bounds_pct": 20}, {"is_sensitive": False, "bounds_pct": 20}):
        r = await client.patch(f"{A}/categories/{cat.id}", json=body, headers=_session(admin))
        _assert_refused(r, "bounds_pct")
    assert await _rows(db_session, admin) == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"platform_commission_pct": 100.01}, "platform_commission_pct"),
        ({"platform_commission_pct": -1}, "platform_commission_pct"),
        ({"platform_commission_pct": None}, "platform_commission_pct"),
        ({"platform_commission_pct": 20.125}, "platform_commission_pct"),
        ({"commission_gst_pct": 101}, "commission_gst_pct"),
        ({"commission_gst_pct": -0.5}, "commission_gst_pct"),
        ({"platform_commission_min": -0.01}, "platform_commission_min"),
        ({"platform_commission_min": 100000000}, "platform_commission_min"),
        ({"platform_commission_min": None}, "platform_commission_min"),
        ({"reference_price": 0}, "reference_price"),
        ({"reference_price": -5}, "reference_price"),
        ({"reference_price": 100000000}, "reference_price"),
        ({"is_sensitive": True, "bounds_pct": 0}, "bounds_pct"),
        ({"is_sensitive": True, "bounds_pct": 100.5}, "bounds_pct"),
        ({"is_sensitive": True, "bounds_pct": None}, "bounds_pct"),
        ({"is_sensitive": None}, "is_sensitive"),
        ({}, "body"),
    ],
)
async def test_bad_category_values_are_refused(client, db_session, admin, body, field):
    cat = await category(db_session)
    r = await client.patch(f"{A}/categories/{cat.id}", json=body, headers=_session(admin))
    _assert_refused(r, field)
    assert await _rows(db_session, admin) == []


@pytest.mark.parametrize(
    "body",
    [
        {"is_sensitive": "yes"},
        {"is_sensitive": 1},
        {"platform_commission_pct": "lots"},
        {"name": "Renamed"},
    ],
)
async def test_wrong_types_and_unknown_fields_are_a_422(client, db_session, admin, body):
    cat = await category(db_session)
    r = await client.patch(f"{A}/categories/{cat.id}", json=body, headers=_session(admin))
    assert r.status_code == 422, r.text
    assert await _rows(db_session, admin) == []


async def test_edge_values_are_accepted_and_null_clears_the_reference_price(
    client, db_session, admin
):
    cat = await category(db_session, reference_price=Decimal("299"))
    r = await client.patch(
        f"{A}/categories/{cat.id}",
        json={
            "platform_commission_pct": 0,
            "commission_gst_pct": 100,
            "platform_commission_min": 0,
            "reference_price": None,
        },
        headers=_session(admin),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["platform_commission_pct"],
        body["commission_gst_pct"],
        body["platform_commission_min"],
        body["reference_price"],
    ) == (0.0, 100.0, 0.0, None)
    [row] = await _rows(db_session, admin)
    assert row.after_json == {
        "reference_price": None,
        "platform_commission_pct": 0.0,
        "platform_commission_min": 0.0,
        "commission_gst_pct": 100.0,
    }


async def test_a_save_that_changes_nothing_writes_no_row(client, db_session, admin):
    cat = await category(db_session, is_sensitive=True, bounds_pct=Decimal("25.00"))
    r = await client.patch(
        f"{A}/categories/{cat.id}",
        # Same values in another spelling: 20 is 20.00, and bounds already 25.
        json={"platform_commission_pct": 20, "is_sensitive": True, "bounds_pct": 25.0},
        headers=_session(admin),
    )
    assert r.status_code == 200, r.text
    assert r.json()["bounds_pct"] == 25.0
    assert await _rows(db_session, admin) == []


async def test_unknown_category_is_a_404(client, db_session, admin):
    r = await client.patch(
        f"{A}/categories/{uuid.uuid4()}",
        json={"platform_commission_pct": 21},
        headers=_session(admin),
    )
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "CATEGORY_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET / PATCH /admin/pricing/config
# ---------------------------------------------------------------------------


async def test_config_list_has_every_key_in_order(client, db_session, support):
    await seed_config(db_session)
    r = await client.get(f"{A}/config", headers=_session(support))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["key"] for row in rows] == list(SEED_CONFIG)
    for row in rows:
        assert set(row) == {"key", "value", "updated_at", "updated_by"}
        assert row["value"] == SEED_CONFIG[row["key"]]
        assert row["updated_by"] is None
        assert datetime.fromisoformat(row["updated_at"]) == SEEDED_AT


async def test_weights_save_sets_who_and_when_and_audits(client, db_session, ops):
    await seed_config(db_session)
    new = {**WEIGHTS, "rating": 0.30, "activity": 0.10}
    r = await client.patch(
        f"{A}/config/ranking_weights", json={"value": new}, headers=_session(ops)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] == new
    assert body["updated_by"] == str(ops.id)
    assert datetime.fromisoformat(body["updated_at"]) > SEEDED_AT + timedelta(days=1)

    row = await db_session.get(PricingConfig, "ranking_weights")
    await db_session.refresh(row)
    assert (row.value_json, row.updated_by) == (new, ops.id)
    [audit] = await _rows(db_session, ops)
    assert (audit.action, audit.target_type, audit.target_id) == (
        "pricing.config_update",
        "pricing_config",
        "ranking_weights",
    )
    assert audit.before_json == {"value_json": WEIGHTS}
    assert audit.after_json == {"value_json": new}


@pytest.mark.parametrize(
    "value",
    [
        {**WEIGHTS, "rating": 0.30},  # sums to 0.95
        {**WEIGHTS, "rating": 0.37},  # sums to 1.02
        {k: v for k, v in WEIGHTS.items() if k != "activity"},  # missing key
        {**WEIGHTS, "activity": 0.0, "speed": 0.05},  # unexpected key
        {**WEIGHTS, "rating": 1.2, "price_vs_ref": -0.6},  # out of range, sums to 1
        {**WEIGHTS, "activity": -0.05, "rating": 0.45},  # negative, sums to 1
        {**WEIGHTS, "activity": True, "rating": -0.6},  # a bool is not a weight
        {**WEIGHTS, "activity": "0.05"},
        [0.35, 0.25, 0.20, 0.15, 0.05],
        1,
        None,
    ],
)
async def test_bad_weights_are_refused(client, db_session, admin, value):
    await seed_config(db_session)
    r = await client.patch(
        f"{A}/config/ranking_weights", json={"value": value}, headers=_session(admin)
    )
    _assert_refused(r, "value")
    row = await db_session.get(PricingConfig, "ranking_weights")
    await db_session.refresh(row)
    assert row.value_json == WEIGHTS
    assert await _rows(db_session, admin) == []


async def test_a_bad_sum_says_what_the_sum_is(client, db_session, admin):
    await seed_config(db_session)
    r = await client.patch(
        f"{A}/config/ranking_weights",
        json={"value": {**WEIGHTS, "rating": 0.30}},
        headers=_session(admin),
    )
    _assert_refused(r, "value")
    assert r.json()["details"]["sum"] == 0.95
    assert "0.95" in r.json()["message"]


@pytest.mark.parametrize("rating", [0.34, 0.36])
async def test_weights_within_the_tolerance_are_accepted(client, db_session, admin, rating):
    """1.00 +/- 0.01: 0.99 and 1.01 pass."""
    await seed_config(db_session)
    r = await client.patch(
        f"{A}/config/ranking_weights",
        json={"value": {**WEIGHTS, "rating": rating}},
        headers=_session(admin),
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("key", ["boost_window_days", "boost_window_jobs", "sheet_ttl_seconds"])
@pytest.mark.parametrize("value", [0, -1, 14.0, 1.5, True, "14", None, {"days": 14}])
async def test_whole_number_keys_refuse_anything_else(client, db_session, admin, key, value):
    await seed_config(db_session)
    r = await client.patch(f"{A}/config/{key}", json={"value": value}, headers=_session(admin))
    _assert_refused(r, "value")
    assert await _rows(db_session, admin) == []


@pytest.mark.parametrize("key", ["boost_window_days", "boost_window_jobs", "sheet_ttl_seconds"])
async def test_whole_number_keys_save(client, db_session, admin, key):
    await seed_config(db_session)
    r = await client.patch(f"{A}/config/{key}", json={"value": 30}, headers=_session(admin))
    assert r.status_code == 200, r.text
    assert r.json()["value"] == 30
    [audit] = await _rows(db_session, admin)
    assert (audit.before_json, audit.after_json) == (
        {"value_json": SEED_CONFIG[key]},
        {"value_json": 30},
    )


async def test_a_config_save_that_changes_nothing_writes_no_row(client, db_session, admin):
    await seed_config(db_session)
    r = await client.patch(
        f"{A}/config/sheet_ttl_seconds", json={"value": 600}, headers=_session(admin)
    )
    assert r.status_code == 200, r.text
    assert r.json()["updated_by"] is None
    assert await _rows(db_session, admin) == []


async def test_unknown_config_key_is_a_404(client, db_session, admin):
    await seed_config(db_session)
    r = await client.patch(
        f"{A}/config/surge_multiplier", json={"value": 2}, headers=_session(admin)
    )
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "PRICING_CONFIG_NOT_FOUND"


async def test_config_body_needs_value_and_nothing_else(client, db_session, admin):
    await seed_config(db_session)
    for body in ({}, {"value": 30, "key": "sheet_ttl_seconds"}):
        r = await client.patch(f"{A}/config/sheet_ttl_seconds", json=body, headers=_session(admin))
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


async def test_support_reads_but_cannot_edit(client, db_session, support):
    await seed_config(db_session)
    cat = await category(db_session)
    headers = _session(support)

    assert (await client.get(f"{A}/categories", headers=headers)).status_code == 200
    assert (await client.get(f"{A}/config", headers=headers)).status_code == 200
    r = await client.patch(
        f"{A}/categories/{cat.id}", json={"platform_commission_pct": 25}, headers=headers
    )
    assert r.status_code == 403, r.text
    r = await client.patch(f"{A}/config/sheet_ttl_seconds", json={"value": 300}, headers=headers)
    assert r.status_code == 403, r.text

    await db_session.refresh(cat)
    assert cat.platform_commission_pct == Decimal("20.00")
    assert await _rows(db_session, support) == []


async def test_ops_edits_a_category_audited_as_ops(client, db_session, ops):
    cat = await category(db_session)
    r = await client.patch(
        f"{A}/categories/{cat.id}", json={"reference_price": 349}, headers=_session(ops)
    )
    assert r.status_code == 200, r.text
    [row] = await _rows(db_session, ops)
    assert (row.before_json, row.after_json) == (
        {"reference_price": None},
        {"reference_price": 349.0},
    )


async def test_no_token_is_a_401(client, db_session):
    assert (await client.get(f"{A}/categories")).status_code == 401
    assert (
        await client.patch(f"{A}/config/sheet_ttl_seconds", json={"value": 1})
    ).status_code == 401
