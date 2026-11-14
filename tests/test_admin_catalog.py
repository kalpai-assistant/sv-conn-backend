"""
SVC-430: the admin catalog surface (/admin/catalog/*).

- the icon list is pinned;
- ``GET /admin/catalog``: L1 → L2 → services, inactive rows included, sibling
  order, active-variant and active-booking counts, and a fixed number of SQL
  statements whatever the catalog's size;
- category and service create / PATCH: every value rule both ways, the
  structure rules, the name rules, the no-op and empty-body cases, and that a
  PATCH can't touch the slug or ``is_active``;
- deactivate / activate: the not-empty and active-bookings refusals, confirm,
  idempotence, and the active-under-inactive rule;
- audit rows hold only what changed, and a SVC-428 pricing save still records
  only pricing fields;
- the public routes gain ``icon_key`` and otherwise behave as before.

Category names are unique across the whole DB and other files commit rows, so
every name carries a random tag and every tree read is filtered to this test's
own ids.
"""

import re
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models.admin_action import AdminAction
from app.models.booking import Booking, BookingStatus
from app.models.category import Category
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.user import AdminRole
from app.seed_data.catalog_icons import CATALOG_ICON_KEYS
from app.services.admin_catalog_service import slugify
from tests.test_accept_contract import _booking
from tests.test_admin_console_auth import _admin, _session

V1 = "/api/v1"
A = f"{V1}/admin/catalog"
INVALID = "CATALOG_INVALID"
TAKEN = "CATALOG_NAME_TAKEN"


def _tag() -> str:
    return uuid.uuid4().hex[:10]


async def _cat(
    db, *, parent: Category | None = None, is_active: bool = True, sort_order: int = 0, name=None
) -> Category:
    tag = _tag()
    row = Category(
        name=name or f"Cat430 {tag}",
        slug=f"cat430-{tag}",
        is_active=is_active,
        sort_order=sort_order,
        parent_id=parent.id if parent else None,
    )
    db.add(row)
    await db.flush()
    return row


async def _svc(db, category: Category, **fields) -> Service:
    row = Service(
        category_id=category.id,
        name=fields.pop("name", f"Service {_tag()}"),
        base_price=fields.pop("base_price", Decimal("499.00")),
        duration_minutes=fields.pop("duration_minutes", 60),
        is_active=fields.pop("is_active", True),
        **fields,
    )
    db.add(row)
    await db.flush()
    return row


async def _variant(db, service: Service, *, is_active: bool = True, sort_order: int = 0):
    row = ServiceVariant(
        service_id=service.id,
        label=f"Variant {_tag()}",
        base_price=Decimal("599.00"),
        provider_payout=Decimal("479.20"),
        platform_commission=Decimal("119.80"),
        sort_order=sort_order,
        is_active=is_active,
    )
    db.add(row)
    await db.flush()
    return row


async def _audit(db, admin) -> list[AdminAction]:
    return list(
        (await db.scalars(select(AdminAction).where(AdminAction.admin_user_id == admin.id))).all()
    )


def _refused(r, field: str, *, status: int = 422, code: str = INVALID) -> None:
    assert r.status_code == status, r.text
    body = r.json()
    assert body["code"] == code, body
    assert body["details"]["field"] == field, body


def _service_body(category: Category, **overrides) -> dict:
    body = {
        "category_id": str(category.id),
        "name": f"New Service {_tag()}",
        "base_price": 499,
        "price_unit": "PER_VISIT",
    }
    body.update(overrides)
    return body


def _node(tree: dict, category_id) -> dict:
    """A category node anywhere in the tree."""
    stack = list(tree["categories"])
    while stack:
        node = stack.pop()
        if node["id"] == str(category_id):
            return node
        stack.extend(node["children"])
    raise AssertionError(f"{category_id} not in the tree")


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


def test_icon_list_is_pinned():
    assert CATALOG_ICON_KEYS == (
        "sparkles",
        "bug",
        "air-vent",
        "layout-grid",
        "zap",
        "wrench",
        "hammer",
        "paintbrush",
        "scissors",
        "brush",
        "hand-heart",
        "droplets",
        "sofa",
        "car",
        "bath",
        "camera",
        "house",
        "shower-head",
        "plug",
        "lightbulb",
        "fan",
        "refrigerator",
        "washing-machine",
        "microwave",
        "paint-roller",
        "drill",
        "cctv",
        "sprout",
        "truck",
        "shirt",
    )
    assert len(set(CATALOG_ICON_KEYS)) == 30
    assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", key) for key in CATALOG_ICON_KEYS)
    # Each fits the column.
    assert max(len(key) for key in CATALOG_ICON_KEYS) <= 40


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Deep Cleaning", "deep-cleaning"),
        ("Salon & Spa for Women", "salon-spa-for-women"),
        ("  AC -- Repair!! ", "ac-repair"),
        ("RO/Purifier 2.0", "ro-purifier-2-0"),
        ("!!", ""),
    ],
)
def test_slugify(name, slug):
    assert slugify(name) == slug


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


async def test_tree_nests_everything_with_sibling_order_and_counts(client, db_session):
    admin = await _admin(db_session, AdminRole.SUPPORT)
    l1 = await _cat(db_session, sort_order=5)
    tag = _tag()
    # Sorted by sort_order, then name in any case.
    second = await _cat(db_session, parent=l1, sort_order=1, name=f"b second {tag}")
    first = await _cat(db_session, parent=l1, sort_order=1, name=f"A first {tag}")
    third = await _cat(db_session, parent=l1, sort_order=2, is_active=False)

    zeta = await _svc(db_session, first, name="zeta wash")
    alpha = await _svc(
        db_session,
        first,
        name="Alpha Polish",
        is_active=False,
        price_unit="PER_SQFT",
        min_qty=100,
        icon_key="brush",
    )
    await _variant(db_session, zeta)
    await _variant(db_session, zeta)
    await _variant(db_session, zeta, is_active=False)
    for status in (
        BookingStatus.REQUESTED,
        BookingStatus.ACCEPTED,
        BookingStatus.EN_ROUTE,
        BookingStatus.IN_PROGRESS,
        BookingStatus.COMPLETED,
        BookingStatus.CANCELLED,
    ):
        await _booking(db_session, service=zeta, status=status)

    r = await client.get(A, headers=_session(admin))
    assert r.status_code == 200, r.text
    node = _node(r.json(), l1.id)
    assert node["parent_id"] is None
    assert node["services"] == []
    assert [c["id"] for c in node["children"]] == [str(first.id), str(second.id), str(third.id)]
    assert node["children"][2]["is_active"] is False

    [a_row, z_row] = node["children"][0]["services"]
    assert a_row == {
        "id": str(alpha.id),
        "category_id": str(first.id),
        "name": "Alpha Polish",
        "price_unit": "PER_SQFT",
        "min_qty": 100,
        "base_price": 499.0,
        "duration_minutes": 60,
        "icon_key": "brush",
        "is_active": False,
        "active_variant_count": 0,
        "active_bookings": 0,
    }
    assert z_row["id"] == str(zeta.id)
    assert (z_row["price_unit"], z_row["min_qty"]) == ("PER_VISIT", None)
    # Two active variants of three; four live bookings of six.
    assert (z_row["active_variant_count"], z_row["active_bookings"]) == (2, 4)
    assert set(node["children"][0]) == {
        "id",
        "name",
        "slug",
        "description",
        "icon_key",
        "sort_order",
        "is_active",
        "parent_id",
        "children",
        "services",
    }


async def test_an_l1_holding_services_lists_them_directly(client, db_session):
    admin = await _admin(db_session)
    l1 = await _cat(db_session)
    service = await _svc(db_session, l1)
    node = _node((await client.get(A, headers=_session(admin))).json(), l1.id)
    assert node["children"] == []
    assert [s["id"] for s in node["services"]] == [str(service.id)]


async def _add_branch(db) -> None:
    l1 = await _cat(db)
    l2 = await _cat(db, parent=l1)
    service = await _svc(db, l2)
    await _variant(db, service)
    await _booking(db, service=service, status=BookingStatus.ACCEPTED)


async def test_tree_runs_the_same_number_of_statements_whatever_its_size(client, db_session):
    admin = await _admin(db_session)
    await _add_branch(db_session)
    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        assert (await client.get(A, headers=_session(admin))).status_code == 200
        small = len(statements)
        for _ in range(3):
            await _add_branch(db_session)
        statements.clear()
        assert (await client.get(A, headers=_session(admin))).status_code == 200
        large = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert small == large
    # Categories, services, and the two grouped counts.
    catalog = [s for s in statements if "FROM categories" in s or "FROM services" in s]
    catalog += [s for s in statements if "FROM service_variants" in s or "FROM booking_items" in s]
    assert len(catalog) == 4, statements


# ---------------------------------------------------------------------------
# Category create
# ---------------------------------------------------------------------------


async def test_create_l1_makes_the_slug_and_audits_the_catalog_fields(client, db_session):
    admin = await _admin(db_session, AdminRole.OPS)
    tag = _tag()
    r = await client.post(
        f"{A}/categories",
        headers=_session(admin),
        json={"name": f"  Window & Glass {tag} ", "icon_key": "house", "sort_order": 9999},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == f"Window & Glass {tag}"
    assert body["slug"] == f"window-glass-{tag}"
    assert (body["icon_key"], body["sort_order"], body["is_active"], body["parent_id"]) == (
        "house",
        9999,
        True,
        None,
    )
    assert (
        body["subcategory_count"],
        body["active_subcategory_count"],
        body["service_count"],
        body["active_service_count"],
    ) == (0, 0, 0, 0)

    [row] = await _audit(db_session, admin)
    assert (row.action, row.target_type, row.target_id) == (
        "category.create",
        "category",
        body["id"],
    )
    assert row.before_json is None
    assert row.after_json == {
        "name": f"Window & Glass {tag}",
        "slug": f"window-glass-{tag}",
        "icon_key": "house",
        "sort_order": 9999,
        "is_active": True,
        "parent_id": None,
    }


async def test_create_l2_under_an_l1_and_inactive(client, db_session):
    admin = await _admin(db_session)
    l1 = await _cat(db_session)
    r = await client.post(
        f"{A}/categories",
        headers=_session(admin),
        json={"name": f"Sub {_tag()}", "parent_id": str(l1.id), "is_active": False},
    )
    assert r.status_code == 201, r.text
    assert (r.json()["parent_id"], r.json()["is_active"], r.json()["sort_order"]) == (
        str(l1.id),
        False,
        0,
    )
    assert r.json()["icon_key"] is None


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"name": " a "}, "name"),
        ({"name": "x" * 101}, "name"),
        ({"name": "!! ??"}, "name"),
        ({"sort_order": -1}, "sort_order"),
        ({"sort_order": 10000}, "sort_order"),
        ({"icon_key": "rocket"}, "icon_key"),
        ({"icon_key": "Sparkles"}, "icon_key"),
        ({"parent_id": str(uuid.uuid4())}, "parent_id"),
    ],
)
async def test_create_category_refuses_bad_values(client, db_session, overrides, field):
    admin = await _admin(db_session)
    body = {"name": f"Refused {_tag()}", **overrides}
    r = await client.post(f"{A}/categories", headers=_session(admin), json=body)
    _refused(r, field)
    assert await _audit(db_session, admin) == []


async def test_create_category_accepts_the_edges(client, db_session):
    admin = await _admin(db_session)
    for body in (
        {"name": "Q" + _tag()[:1]},  # 2 characters
        {"name": "x" * 90 + _tag(), "sort_order": 0},  # 100 characters
    ):
        r = await client.post(f"{A}/categories", headers=_session(admin), json=body)
        assert r.status_code == 201, r.text


async def test_create_category_refuses_slug_and_unknown_fields(client, db_session):
    admin = await _admin(db_session)
    for extra in ({"slug": "mine"}, {"pricing": 1}):
        r = await client.post(
            f"{A}/categories", headers=_session(admin), json={"name": f"X {_tag()}", **extra}
        )
        assert r.status_code == 422, r.text


async def test_category_name_and_slug_must_be_free(client, db_session):
    admin = await _admin(db_session)
    tag = _tag()
    existing = await _cat(db_session, name=f"Garden Care {tag}")
    existing.slug = f"garden-care-{tag}"
    await db_session.flush()

    # Same name, other case.
    r = await client.post(
        f"{A}/categories", headers=_session(admin), json={"name": f"GARDEN care {tag}"}
    )
    _refused(r, "name", status=409, code=TAKEN)
    # A different name that makes the same slug.
    r = await client.post(
        f"{A}/categories", headers=_session(admin), json={"name": f"Garden & Care {tag}"}
    )
    _refused(r, "name", status=409, code=TAKEN)
    assert await _audit(db_session, admin) == []


async def test_parent_must_be_an_l1_without_services(client, db_session):
    admin = await _admin(db_session)
    l1 = await _cat(db_session)
    l2 = await _cat(db_session, parent=l1)
    holds_services = await _cat(db_session)
    await _svc(db_session, holds_services, is_active=False)  # inactive counts too

    for parent in (l2, holds_services):
        r = await client.post(
            f"{A}/categories",
            headers=_session(admin),
            json={"name": f"Child {_tag()}", "parent_id": str(parent.id)},
        )
        _refused(r, "parent_id")


async def test_an_active_category_cannot_go_under_an_inactive_one(client, db_session):
    admin = await _admin(db_session)
    off = await _cat(db_session, is_active=False)
    body = {"name": f"Child {_tag()}", "parent_id": str(off.id)}
    _refused(await client.post(f"{A}/categories", headers=_session(admin), json=body), "parent_id")
    r = await client.post(
        f"{A}/categories", headers=_session(admin), json={**body, "is_active": False}
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Category PATCH
# ---------------------------------------------------------------------------


async def test_rename_keeps_the_slug_and_audits_only_what_changed(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session, sort_order=4)
    slug, old_name = cat.slug, cat.name
    new_name = f"Renamed {_tag()}"
    r = await client.patch(
        f"{A}/categories/{cat.id}",
        headers=_session(admin),
        json={"name": new_name, "sort_order": 4, "icon_key": "fan"},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["slug"], r.json()["icon_key"]) == (new_name, slug, "fan")

    [row] = await _audit(db_session, admin)
    assert row.action == "category.update"
    assert row.before_json == {"name": old_name, "icon_key": None}
    assert row.after_json == {"name": new_name, "icon_key": "fan"}


async def test_category_patch_no_op_empty_and_nulls(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session, sort_order=2)
    url = f"{A}/categories/{cat.id}"

    r = await client.patch(url, headers=_session(admin), json={"sort_order": 2, "name": cat.name})
    assert r.status_code == 200, r.text
    assert await _audit(db_session, admin) == []

    _refused(await client.patch(url, headers=_session(admin), json={}), "body")
    _refused(await client.patch(url, headers=_session(admin), json={"name": None}), "name")
    _refused(
        await client.patch(url, headers=_session(admin), json={"sort_order": None}), "sort_order"
    )
    for extra in ({"slug": "other"}, {"is_active": False}):
        r = await client.patch(url, headers=_session(admin), json=extra)
        assert r.status_code == 422, r.text
    assert await _audit(db_session, admin) == []

    # Its own name in another case is free; another category's name isn't.
    r = await client.patch(url, headers=_session(admin), json={"name": cat.name.upper()})
    assert r.status_code == 200, r.text
    other = await _cat(db_session)
    r = await client.patch(url, headers=_session(admin), json={"name": other.name.lower()})
    _refused(r, "name", status=409, code=TAKEN)


async def test_icon_key_null_clears_it(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    cat.icon_key = "zap"
    await db_session.flush()
    r = await client.patch(
        f"{A}/categories/{cat.id}", headers=_session(admin), json={"icon_key": None}
    )
    assert r.status_code == 200, r.text
    assert r.json()["icon_key"] is None


async def test_moving_a_category(client, db_session):
    admin = await _admin(db_session)
    home = await _cat(db_session)
    sub = await _cat(db_session, parent=home)
    target = await _cat(db_session)
    holds_services = await _cat(db_session)
    await _svc(db_session, holds_services)
    off = await _cat(db_session, is_active=False)

    def move(category, parent):
        return client.patch(
            f"{A}/categories/{category.id}",
            headers=_session(admin),
            json={"parent_id": str(parent.id) if parent else None},
        )

    r = await move(sub, target)
    assert r.status_code == 200, r.text
    assert r.json()["parent_id"] == str(target.id)

    _refused(await move(sub, holds_services), "parent_id")  # services there
    _refused(await move(sub, off), "parent_id")  # active under inactive
    _refused(await move(target, home), "parent_id")  # target has a child now: 2 levels
    _refused(await move(home, home), "parent_id")  # itself
    _refused(await move(holds_services, sub), "parent_id")  # sub is an L2

    # Back to the top level: it becomes a tile.
    r = await move(sub, None)
    assert r.status_code == 200, r.text
    assert r.json()["parent_id"] is None
    # An inactive category may go under an inactive one.
    sub.is_active = False
    await db_session.flush()
    assert (await move(sub, off)).status_code == 200


# ---------------------------------------------------------------------------
# Category deactivate / activate
# ---------------------------------------------------------------------------


async def test_deactivate_is_refused_while_the_category_holds_active_rows(client, db_session):
    admin = await _admin(db_session)
    with_service = await _cat(db_session)
    service = await _svc(db_session, with_service)
    await _svc(db_session, with_service, is_active=False)
    parent = await _cat(db_session)
    await _cat(db_session, parent=parent)
    await _cat(db_session, parent=parent, is_active=False)

    r = await client.post(f"{A}/categories/{with_service.id}/deactivate", headers=_session(admin))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "CATEGORY_NOT_EMPTY"
    assert r.json()["details"] == {"active_services": 1, "active_subcategories": 0}

    r = await client.post(f"{A}/categories/{parent.id}/deactivate", headers=_session(admin))
    assert r.status_code == 409, r.text
    assert r.json()["details"] == {"active_services": 0, "active_subcategories": 1}
    assert await _audit(db_session, admin) == []

    # With only inactive rows left, it goes.
    service.is_active = False
    await db_session.flush()
    r = await client.post(f"{A}/categories/{with_service.id}/deactivate", headers=_session(admin))
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False
    [row] = await _audit(db_session, admin)
    assert (row.action, row.before_json, row.after_json) == (
        "category.deactivate",
        {"is_active": True},
        {"is_active": False},
    )
    # Again: 200, no new row.
    r = await client.post(f"{A}/categories/{with_service.id}/deactivate", headers=_session(admin))
    assert r.status_code == 200
    assert len(await _audit(db_session, admin)) == 1


async def test_activate_needs_an_active_parent(client, db_session):
    admin = await _admin(db_session)
    parent = await _cat(db_session, is_active=False)
    child = await _cat(db_session, parent=parent, is_active=False)

    r = await client.post(f"{A}/categories/{child.id}/activate", headers=_session(admin))
    _refused(r, "parent_id")

    assert (
        await client.post(f"{A}/categories/{parent.id}/activate", headers=_session(admin))
    ).status_code == 200
    r = await client.post(f"{A}/categories/{child.id}/activate", headers=_session(admin))
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True
    assert [row.action for row in await _audit(db_session, admin)] == [
        "category.activate",
        "category.activate",
    ]
    r = await client.post(f"{A}/categories/{child.id}/activate", headers=_session(admin))
    assert r.status_code == 200
    assert len(await _audit(db_session, admin)) == 2


async def test_deactivated_category_leaves_public_categories(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    cat.icon_key = "sprout"
    await db_session.flush()
    listed = {c["id"]: c for c in (await client.get(f"{V1}/categories")).json()}
    assert listed[str(cat.id)]["icon_key"] == "sprout"

    await client.post(f"{A}/categories/{cat.id}/deactivate", headers=_session(admin))
    assert str(cat.id) not in {c["id"] for c in (await client.get(f"{V1}/categories")).json()}


async def test_unknown_ids_are_404(client, db_session):
    admin = await _admin(db_session)
    nope = uuid.uuid4()
    for kind, code, patch_body in (
        ("categories", "CATEGORY_NOT_FOUND", {"sort_order": 1}),
        ("services", "SERVICE_NOT_FOUND", {"name": "Whatever"}),
    ):
        for method, suffix in (
            ("GET", ""),
            ("PATCH", ""),
            ("POST", "/deactivate"),
            ("POST", "/activate"),
        ):
            kwargs = {"json": patch_body} if method == "PATCH" else {}
            r = await client.request(
                method, f"{A}/{kind}/{nope}{suffix}", headers=_session(admin), **kwargs
            )
            assert (r.status_code, r.json()["code"]) == (404, code), (method, kind, suffix)


# ---------------------------------------------------------------------------
# Service create
# ---------------------------------------------------------------------------


async def test_create_service_stores_everything_and_audits(client, db_session):
    admin = await _admin(db_session, AdminRole.OPS)
    cat = await _cat(db_session)
    markdown = "  ## Included\n\n- Sofa\n- Carpet\n"
    r = await client.post(
        f"{A}/services",
        headers=_session(admin),
        json=_service_body(
            cat,
            name="  Carpet Shampoo  ",
            description=markdown,
            base_price="12.50",
            duration_minutes=480,
            price_unit="per_sqft",
            min_qty=100000,
            icon_key="sofa",
        ),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Carpet Shampoo"
    assert body["description"] == markdown  # as sent
    assert (body["base_price"], body["duration_minutes"]) == (12.5, 480)
    assert (body["price_unit"], body["min_qty"], body["icon_key"], body["is_active"]) == (
        "PER_SQFT",
        100000,
        "sofa",
        True,
    )
    assert (body["variants"], body["active_variant_count"], body["active_bookings"]) == ([], 0, 0)

    [row] = await _audit(db_session, admin)
    assert (row.action, row.target_type, row.target_id) == ("service.create", "service", body["id"])
    assert row.after_json["price_unit"] == "PER_SQFT"
    assert row.after_json["min_qty"] == 100000
    assert row.after_json["icon_key"] == "sofa"
    assert row.after_json["base_price"] == 12.5


async def test_create_service_defaults(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    r = await client.post(
        f"{A}/services",
        headers=_session(admin),
        json=_service_body(cat, description="   ", base_price=15),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["duration_minutes"], body["description"], body["min_qty"], body["icon_key"]) == (
        60,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"name": "ab"}, "name"),
        ({"name": "x" * 201}, "name"),
        ({"description": "x" * 2001}, "description"),
        ({"base_price": 0}, "base_price"),
        ({"base_price": -5}, "base_price"),
        ({"base_price": "1000000.01"}, "base_price"),
        ({"base_price": "10.005"}, "base_price"),
        ({"duration_minutes": 14}, "duration_minutes"),
        ({"duration_minutes": 481}, "duration_minutes"),
        ({"price_unit": "PER_DAY"}, "price_unit"),
        ({"price_unit": "PER_SQFT"}, "min_qty"),
        ({"price_unit": "PER_HOUR", "min_qty": None}, "min_qty"),
        ({"price_unit": "PER_HOUR", "min_qty": 0}, "min_qty"),
        ({"price_unit": "PER_SQFT", "min_qty": 100001}, "min_qty"),
        ({"price_unit": "PER_VISIT", "min_qty": 1}, "min_qty"),
        ({"price_unit": "PER_ITEM", "min_qty": 3}, "min_qty"),
        ({"icon_key": "rocket"}, "icon_key"),
        ({"category_id": str(uuid.uuid4())}, "category_id"),
    ],
)
async def test_create_service_refuses_bad_values(client, db_session, overrides, field):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    r = await client.post(
        f"{A}/services", headers=_session(admin), json=_service_body(cat, **overrides)
    )
    _refused(r, field)
    assert await _audit(db_session, admin) == []


async def test_create_service_accepts_the_edges(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    for overrides in (
        {"name": "abc", "base_price": "0.01", "duration_minutes": 15},
        {"name": "x" * 200, "base_price": 1000000, "description": "x" * 2000},
        {"price_unit": "PER_HOUR", "min_qty": 1},
        {"price_unit": "PER_ITEM"},
        {"price_unit": "PER_VISIT", "min_qty": None},
    ):
        r = await client.post(
            f"{A}/services", headers=_session(admin), json=_service_body(cat, **overrides)
        )
        assert r.status_code == 201, (overrides, r.text)


async def test_create_service_shape_errors(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    body = _service_body(cat)
    del body["price_unit"]
    assert (
        await client.post(f"{A}/services", headers=_session(admin), json=body)
    ).status_code == 422
    r = await client.post(
        f"{A}/services", headers=_session(admin), json=_service_body(cat, slug="no")
    )
    assert r.status_code == 422


async def test_service_names_are_unique_per_category_in_any_case(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    other = await _cat(db_session)
    await _svc(db_session, cat, name="Tap Repair", is_active=False)

    r = await client.post(
        f"{A}/services", headers=_session(admin), json=_service_body(cat, name=" tap REPAIR ")
    )
    _refused(r, "name", status=409, code=TAKEN)
    r = await client.post(
        f"{A}/services", headers=_session(admin), json=_service_body(other, name="Tap Repair")
    )
    assert r.status_code == 201, r.text


async def test_service_category_rules(client, db_session):
    admin = await _admin(db_session)
    parent = await _cat(db_session)
    await _cat(db_session, parent=parent, is_active=False)  # inactive child counts
    off = await _cat(db_session, is_active=False)

    r = await client.post(f"{A}/services", headers=_session(admin), json=_service_body(parent))
    _refused(r, "category_id")
    r = await client.post(f"{A}/services", headers=_session(admin), json=_service_body(off))
    _refused(r, "category_id")
    r = await client.post(
        f"{A}/services", headers=_session(admin), json=_service_body(off, is_active=False)
    )
    assert r.status_code == 201, r.text
    assert r.json()["is_active"] is False


async def test_a_category_with_services_cannot_get_sub_categories_and_vice_versa(
    client, db_session
):
    admin = await _admin(db_session)
    l1 = await _cat(db_session)
    r = await client.post(f"{A}/services", headers=_session(admin), json=_service_body(l1))
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{A}/categories",
        headers=_session(admin),
        json={"name": f"Child {_tag()}", "parent_id": str(l1.id)},
    )
    _refused(r, "parent_id")


# ---------------------------------------------------------------------------
# Service PATCH
# ---------------------------------------------------------------------------


async def test_service_patch_audits_only_what_changed(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    service = await _svc(db_session, cat, name="Fan Repair", duration_minutes=60)
    r = await client.patch(
        f"{A}/services/{service.id}",
        headers=_session(admin),
        json={"name": "Fan Repair", "duration_minutes": 90, "base_price": "499.00"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["duration_minutes"] == 90
    [row] = await _audit(db_session, admin)
    assert (row.action, row.before_json, row.after_json) == (
        "service.update",
        {"duration_minutes": 60},
        {"duration_minutes": 90},
    )


async def test_service_patch_no_op_empty_nulls_and_locked_fields(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    service = await _svc(db_session, cat, description="Old", icon_key="fan")
    url = f"{A}/services/{service.id}"

    r = await client.patch(url, headers=_session(admin), json={"price_unit": "PER_VISIT"})
    assert r.status_code == 200, r.text
    assert await _audit(db_session, admin) == []

    _refused(await client.patch(url, headers=_session(admin), json={}), "body")
    for field in ("name", "base_price", "duration_minutes", "price_unit", "category_id"):
        _refused(await client.patch(url, headers=_session(admin), json={field: None}), field)
    for extra in ({"is_active": False}, {"slug": "x"}):
        assert (await client.patch(url, headers=_session(admin), json=extra)).status_code == 422
    assert await _audit(db_session, admin) == []

    r = await client.patch(
        url, headers=_session(admin), json={"description": None, "icon_key": None}
    )
    assert r.status_code == 200, r.text
    assert (r.json()["description"], r.json()["icon_key"]) == (None, None)


async def test_a_legacy_zero_price_row_can_still_be_edited(client, db_session):
    """The seeded "Emergency/last-minute setup" is at 0.00: a field's own rule runs
    only when that field is sent."""
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    service = await _svc(db_session, cat, base_price=Decimal("0.00"))
    url = f"{A}/services/{service.id}"
    r = await client.patch(url, headers=_session(admin), json={"name": "Emergency setup"})
    assert r.status_code == 200, r.text
    _refused(await client.patch(url, headers=_session(admin), json={"base_price": 0}), "base_price")


@pytest.mark.parametrize(
    ("start", "body", "field"),
    [
        # Moving off a unit that needs min_qty needs min_qty: null too.
        (("PER_SQFT", 50), {"price_unit": "PER_VISIT"}, "min_qty"),
        # Moving onto one needs min_qty.
        (("PER_VISIT", None), {"price_unit": "PER_HOUR"}, "min_qty"),
        # min_qty alone on a unit that doesn't take it.
        (("PER_VISIT", None), {"min_qty": 5}, "min_qty"),
        (("PER_SQFT", 50), {"min_qty": None}, "min_qty"),
        (("PER_SQFT", 50), {"min_qty": 0}, "min_qty"),
    ],
)
async def test_unit_and_min_qty_on_patch_are_checked_together(
    client, db_session, start, body, field
):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    unit, qty = start
    service = await _svc(db_session, cat, price_unit=unit, min_qty=qty)
    _refused(
        await client.patch(f"{A}/services/{service.id}", headers=_session(admin), json=body), field
    )


@pytest.mark.parametrize(
    ("start", "body", "end"),
    [
        (("PER_SQFT", 50), {"price_unit": "PER_VISIT", "min_qty": None}, ("PER_VISIT", None)),
        (("PER_VISIT", None), {"price_unit": "PER_HOUR", "min_qty": 2}, ("PER_HOUR", 2)),
        (("PER_SQFT", 50), {"price_unit": "PER_HOUR"}, ("PER_HOUR", 50)),
        (("PER_SQFT", 50), {"min_qty": 75}, ("PER_SQFT", 75)),
    ],
)
async def test_unit_and_min_qty_changes_that_hold(client, db_session, start, body, end):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    unit, qty = start
    service = await _svc(db_session, cat, price_unit=unit, min_qty=qty)
    r = await client.patch(f"{A}/services/{service.id}", headers=_session(admin), json=body)
    assert r.status_code == 200, r.text
    assert (r.json()["price_unit"], r.json()["min_qty"]) == end


async def test_moving_a_service(client, db_session):
    admin = await _admin(db_session)
    home = await _cat(db_session)
    service = await _svc(db_session, home, name="Geyser Fix")
    target = await _cat(db_session)
    has_kids = await _cat(db_session)
    await _cat(db_session, parent=has_kids)
    off = await _cat(db_session, is_active=False)
    clash = await _cat(db_session)
    await _svc(db_session, clash, name="GEYSER FIX")

    def move(category):
        return client.patch(
            f"{A}/services/{service.id}",
            headers=_session(admin),
            json={"category_id": str(category.id)},
        )

    _refused(await move(has_kids), "category_id")
    _refused(await move(off), "category_id")
    _refused(await move(clash), "name", status=409, code=TAKEN)
    r = await move(target)
    assert r.status_code == 200, r.text
    assert r.json()["category_id"] == str(target.id)
    [row] = await _audit(db_session, admin)
    assert row.before_json == {"category_id": str(home.id)}

    # An inactive service may go in an inactive category.
    service.is_active = False
    await db_session.flush()
    assert (await move(off)).status_code == 200


async def test_rename_clash_within_the_category(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    service = await _svc(db_session, cat, name="Sink Repair")
    await _svc(db_session, cat, name="Tap Repair")
    url = f"{A}/services/{service.id}"
    _refused(
        await client.patch(url, headers=_session(admin), json={"name": "tap repair"}),
        "name",
        status=409,
        code=TAKEN,
    )
    assert (
        await client.patch(url, headers=_session(admin), json={"name": "SINK repair"})
    ).status_code == 200


# ---------------------------------------------------------------------------
# Service detail, deactivate / activate
# ---------------------------------------------------------------------------


async def test_service_detail_lists_every_variant(client, db_session):
    admin = await _admin(db_session, AdminRole.SUPPORT)
    cat = await _cat(db_session)
    service = await _svc(db_session, cat)
    second = await _variant(db_session, service, sort_order=2, is_active=False)
    first = await _variant(db_session, service, sort_order=1)
    r = await client.get(f"{A}/services/{service.id}", headers=_session(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [v["id"] for v in body["variants"]] == [str(first.id), str(second.id)]
    assert [v["is_active"] for v in body["variants"]] == [True, False]
    assert body["variants"][0]["base_price"] == 599.0
    assert body["active_variant_count"] == 1


async def test_deactivate_with_active_bookings_needs_confirm(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    service = await _svc(db_session, cat)
    _, requested = await _booking(db_session, service=service, status=BookingStatus.REQUESTED)
    _, accepted = await _booking(db_session, service=service, status=BookingStatus.ACCEPTED)
    await _booking(db_session, service=service, status=BookingStatus.COMPLETED)
    url = f"{A}/services/{service.id}/deactivate"

    for kwargs in ({}, {"json": {}}, {"json": {"confirm": False}}):
        r = await client.post(url, headers=_session(admin), **kwargs)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "SERVICE_HAS_ACTIVE_BOOKINGS"
        assert r.json()["details"] == {"active_bookings": 2}
    assert await _audit(db_session, admin) == []

    r = await client.post(url, headers=_session(admin), json={"confirm": True})
    assert r.status_code == 200, r.text
    assert (r.json()["is_active"], r.json()["active_bookings"]) == (False, 2)
    [row] = await _audit(db_session, admin)
    assert row.action == "service.deactivate"
    assert row.details == {"active_bookings": 2, "confirm": True}
    assert (row.before_json, row.after_json) == ({"is_active": True}, {"is_active": False})

    # The bookings are left alone.
    statuses = (
        await db_session.scalars(
            select(Booking.status)
            .where(Booking.id.in_([requested.id, accepted.id]))
            .execution_options(populate_existing=True)
        )
    ).all()
    assert sorted(statuses) == sorted([BookingStatus.REQUESTED, BookingStatus.ACCEPTED])

    # Already off: 200, no row, no confirm needed.
    assert (await client.post(url, headers=_session(admin))).status_code == 200
    assert len(await _audit(db_session, admin)) == 1


async def test_deactivate_without_live_bookings_needs_no_confirm(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    service = await _svc(db_session, cat)
    await _booking(db_session, service=service, status=BookingStatus.CANCELLED)
    await _booking(db_session, service=service, status=BookingStatus.COMPLETED)
    r = await client.post(f"{A}/services/{service.id}/deactivate", headers=_session(admin))
    assert r.status_code == 200, r.text
    [row] = await _audit(db_session, admin)
    assert row.details == {"active_bookings": 0, "confirm": False}

    # And it drops out of the public list.
    public = (await client.get(f"{V1}/services", params={"category_id": str(cat.id)})).json()
    assert public == []


async def test_activate_service_needs_an_active_category(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session, is_active=False)
    service = await _svc(db_session, cat, is_active=False)
    url = f"{A}/services/{service.id}/activate"
    _refused(await client.post(url, headers=_session(admin)), "category_id")
    cat.is_active = True
    await db_session.flush()
    r = await client.post(url, headers=_session(admin))
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True
    [row] = await _audit(db_session, admin)
    assert (row.action, row.after_json) == ("service.activate", {"is_active": True})
    assert (await client.post(url, headers=_session(admin))).status_code == 200
    assert len(await _audit(db_session, admin)) == 1


# ---------------------------------------------------------------------------
# Public routes and the pricing audit
# ---------------------------------------------------------------------------


async def test_public_services_carry_icon_key(client, db_session):
    cat = await _cat(db_session)
    service = await _svc(db_session, cat, icon_key="wrench")
    [listed] = (await client.get(f"{V1}/services", params={"category_id": str(cat.id)})).json()
    assert listed["icon_key"] == "wrench"
    assert "price_unit" not in listed and "min_qty" not in listed
    detail = (await client.get(f"{V1}/services/{service.id}")).json()
    assert detail["icon_key"] == "wrench"


async def test_a_pricing_save_still_records_only_pricing_fields(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    r = await client.patch(
        f"{V1}/admin/pricing/categories/{cat.id}",
        headers=_session(admin),
        json={"platform_commission_pct": 25},
    )
    assert r.status_code == 200, r.text
    [row] = await _audit(db_session, admin)
    assert row.action == "pricing.category_update"
    assert row.before_json == {"platform_commission_pct": 20.0}
    assert row.after_json == {"platform_commission_pct": 25.0}
