"""
SVC-429: the admin coupon surface.

- ``POST /admin/coupons``: full validation, code stored lowercase, shown
  uppercase, unique in any case (409), one ``coupon.create`` audit row;
- the ticket's AC: a coupon created here, used on a real ``POST /bookings``,
  shows that use in ``GET /admin/coupons/{code}``;
- ``GET /admin/coupons``: the five statuses, ``q``, ``personal``, pagination;
- ``PATCH``: partial, validated against the merged row, ``usage_limit`` not below
  usage, only changed fields audited, a no-op writes no row;
- deactivate / activate: idempotent, audited only on a change;
- admin and ops write, support reads;
- the consumer ``/coupons/available`` and ``/coupons/validate`` still behave.

Every test tags its codes with its own random hex and lists with ``q=<tag>``,
since other files commit coupons into the shared DB.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.exceptions import CouponInvalid
from app.models.admin_action import ADMIN_ACTION_TARGET_TYPES, ADMIN_ACTIONS, AdminAction
from app.models.coupon import Coupon, DiscountType
from app.models.user import AdminRole
from app.services.coupon_service import CouponService
from tests.factories.user_factory import UserFactory
from tests.test_admin_console_auth import _admin, _session
from tests.test_coupons import _consumer_with_address, _service, _stub_booking

A = "/api/v1/admin/coupons"
ERROR_CODE = "COUPON_INVALID"
ROW_FIELDS = {
    "id",
    "code",
    "discount_type",
    "discount_value",
    "max_discount",
    "min_order_amount",
    "valid_from",
    "valid_until",
    "usage_limit",
    "usage_limit_per_user",
    "is_active",
    "user_id",
    "owner_name",
    "owner_phone",
    "usage_count",
    "status",
    "created_at",
}


def _tag() -> str:
    return uuid.uuid4().hex[:8].upper()


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def _body(code: str, **overrides) -> dict:
    body = {
        "code": code,
        "discount_type": "percent",
        "discount_value": 10,
        "valid_until": _iso(timedelta(days=30)),
    }
    body.update(overrides)
    return body


async def _rows(db, admin) -> list[AdminAction]:
    return list(
        (
            await db.execute(
                select(AdminAction)
                .where(AdminAction.admin_user_id == admin.id)
                .order_by(AdminAction.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _coupon(db, code: str, **fields) -> Coupon:
    now = datetime.now(UTC)
    row = Coupon(
        code=code.lower(),
        discount_type=fields.pop("discount_type", DiscountType.PERCENT),
        discount_value=fields.pop("discount_value", 10),
        valid_from=fields.pop("valid_from", now - timedelta(days=1)),
        valid_until=fields.pop("valid_until", now + timedelta(days=30)),
        usage_limit_per_user=fields.pop("usage_limit_per_user", 1),
        is_active=fields.pop("is_active", True),
        **fields,
    )
    db.add(row)
    await db.flush()
    return row


async def _redeem(db, coupon: Coupon, discount: float = 50) -> None:
    from app.models.coupon import CouponRedemption

    consumer, addr = await _consumer_with_address(db)
    booking = await _stub_booking(db, consumer.id, addr.id)
    db.add(
        CouponRedemption(
            coupon_id=coupon.id,
            user_id=consumer.id,
            booking_id=booking.id,
            discount_applied=discount,
        )
    )
    await db.flush()


# ---------------------------------------------------------------------------
# The ticket's AC: create → book with it → stats
# ---------------------------------------------------------------------------


async def test_created_coupon_used_on_a_real_booking_shows_in_its_stats(
    client, db_session, auth_headers
):
    admin = await _admin(db_session, AdminRole.OPS)
    code = f"T{_tag()}"
    r = await client.post(
        A,
        headers=_session(admin),
        json=_body(code, discount_value=20, max_discount=150, usage_limit=5),
    )
    assert r.status_code == 201, r.text
    assert r.json()["usage_count"] == 0

    consumer, addr = await _consumer_with_address(db_session)
    consumer.name = "Coupon Customer"
    svc = await _service(db_session, base_price=1000.0)
    with patch("app.routers.bookings.send_booking_notification") as task:
        task.delay = lambda **kwargs: None
        booked = await client.post(
            "/api/v1/bookings/",
            headers=auth_headers(str(consumer.id), "consumer"),
            json={
                "service_id": str(svc.id),
                "address_id": str(addr.id),
                "payment_method": "online",
                # The consumer types it in any case.
                "coupon_code": code.lower(),
            },
        )
    assert booked.status_code == 201, booked.text
    booking_id = booked.json()["id"]

    detail = await client.get(f"{A}/{code}", headers=_session(admin))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["usage_count"] == 1
    assert body["usage_limit"] == 5
    # 20% of 1000 is 200, capped at 150.
    assert body["total_discount"] == 150.0
    [redemption] = body["recent_redemptions"]
    assert redemption["booking_id"] == booking_id
    assert redemption["booking_status"] == "requested"
    assert redemption["discount_applied"] == 150.0
    assert redemption["customer_id"] == str(consumer.id)
    assert redemption["customer_name"] == "Coupon Customer"


async def test_detail_lists_the_latest_20_redemptions_newest_first(client, db_session):
    admin = await _admin(db_session, AdminRole.SUPPORT)
    coupon = await _coupon(db_session, f"M{_tag()}", usage_limit_per_user=50)
    for _ in range(22):
        await _redeem(db_session, coupon, discount=10)

    body = (await client.get(f"{A}/{coupon.code}", headers=_session(admin))).json()
    assert body["usage_count"] == 22
    assert body["total_discount"] == 220.0
    rows = body["recent_redemptions"]
    assert len(rows) == 20
    assert [r["redeemed_at"] for r in rows] == sorted(
        (r["redeemed_at"] for r in rows), reverse=True
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_stores_lowercase_shows_uppercase_and_audits(client, db_session):
    admin = await _admin(db_session)
    tag = _tag()
    r = await client.post(
        A,
        headers=_session(admin),
        json=_body(f"  x{tag.lower()} ", discount_type="FLAT", discount_value=99.5),
    )
    assert r.status_code == 201, r.text
    row = r.json()
    assert set(row) == ROW_FIELDS
    assert row["code"] == f"X{tag}"
    assert row["discount_type"] == "flat"
    assert row["discount_value"] == 99.5
    assert row["status"] == "active"
    # Defaults: no cap, no minimum, unlimited, one per user, on, global.
    assert row["max_discount"] is None
    assert row["min_order_amount"] is None
    assert row["usage_limit"] is None
    assert row["usage_limit_per_user"] == 1
    assert row["is_active"] is True
    assert row["user_id"] is None
    valid_from = datetime.fromisoformat(row["valid_from"])
    assert abs(valid_from - datetime.now(UTC)) < timedelta(minutes=1)

    stored = await db_session.scalar(select(Coupon).where(Coupon.id == uuid.UUID(row["id"])))
    assert stored.code == f"x{tag.lower()}"

    [audit] = await _rows(db_session, admin)
    assert (audit.action, audit.target_type, audit.target_id) == (
        "coupon.create",
        "coupon",
        row["id"],
    )
    assert audit.before_json is None
    assert audit.after_json["code"] == f"x{tag.lower()}"
    assert audit.after_json["discount_value"] == 99.5
    assert audit.after_json["is_active"] is True
    assert audit.action in ADMIN_ACTIONS
    assert audit.target_type in ADMIN_ACTION_TARGET_TYPES


async def test_create_can_start_switched_off(client, db_session):
    admin = await _admin(db_session)
    r = await client.post(A, headers=_session(admin), json=_body(f"O{_tag()}", is_active=False))
    assert r.status_code == 201, r.text
    assert (r.json()["is_active"], r.json()["status"]) == (False, "inactive")


async def test_create_with_a_future_start_is_scheduled(client, db_session):
    admin = await _admin(db_session)
    r = await client.post(
        A,
        headers=_session(admin),
        json=_body(f"S{_tag()}", valid_from=_iso(timedelta(days=2))),
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "scheduled"


async def test_a_code_taken_in_another_case_is_a_409(client, db_session):
    admin = await _admin(db_session)
    tag = _tag()
    await _coupon(db_session, f"d{tag.lower()}")
    r = await client.post(A, headers=_session(admin), json=_body(f"D{tag}"))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "COUPON_CODE_TAKEN"
    assert await _rows(db_session, admin) == []


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"code": "AB"}, "code"),
        ({"code": "ABC-12"}, "code"),
        ({"code": "A" * 21}, "code"),
        ({"code": "ÄBCD"}, "code"),
        ({"discount_value": 0}, "discount_value"),
        ({"discount_value": -5}, "discount_value"),
        ({"discount_value": 100.01}, "discount_value"),
        ({"discount_value": 10.005}, "discount_value"),
        ({"discount_type": "flat", "max_discount": 50}, "max_discount"),
        ({"max_discount": 0}, "max_discount"),
        ({"min_order_amount": -1}, "min_order_amount"),
        ({"min_order_amount": 1e11}, "min_order_amount"),
        (
            {"valid_from": _iso(timedelta(days=10)), "valid_until": _iso(timedelta(days=5))},
            "valid_until",
        ),
        ({"valid_until": _iso(timedelta(minutes=-1))}, "valid_until"),
        ({"usage_limit": 0}, "usage_limit"),
        ({"usage_limit_per_user": 0}, "usage_limit_per_user"),
        ({"usage_limit_per_user": None}, "usage_limit_per_user"),
        ({"valid_from": None}, None),
    ],
)
async def test_create_refusals(client, db_session, overrides, field):
    admin = await _admin(db_session)
    code = overrides.get("code", f"V{_tag()}")
    r = await client.post(A, headers=_session(admin), json=_body(**{**overrides, "code": code}))
    if field is None:
        # valid_from: null is "not sent", so it defaults to now.
        assert r.status_code == 201, r.text
        return
    assert r.status_code == 422, r.text
    if field == "usage_limit_per_user":
        # A null on a non-nullable int is a schema error, before the service.
        return
    assert (r.json()["code"], r.json()["details"]["field"]) == (ERROR_CODE, field), r.json()
    assert await _rows(db_session, admin) == []
    if field != "code":
        assert await db_session.scalar(select(Coupon).where(Coupon.code == code.lower())) is None


async def test_a_percent_coupon_needs_no_cap(client, db_session):
    admin = await _admin(db_session)
    r = await client.post(A, headers=_session(admin), json=_body(f"P{_tag()}", discount_value=100))
    assert r.status_code == 201, r.text
    assert (r.json()["discount_value"], r.json()["max_discount"]) == (100.0, None)


async def test_create_refuses_fields_the_console_cant_set(client, db_session):
    admin = await _admin(db_session)
    other = await UserFactory.create(db_session, role="consumer")
    r = await client.post(
        A, headers=_session(admin), json={**_body(f"U{_tag()}"), "user_id": str(other.id)}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def test_the_url_code_is_case_insensitive_and_unknown_is_404(client, db_session):
    admin = await _admin(db_session, AdminRole.SUPPORT)
    tag = _tag()
    await _coupon(db_session, f"c{tag.lower()}")
    for code in (f"C{tag}", f"c{tag.lower()}", f"c{tag}"):
        r = await client.get(f"{A}/{code}", headers=_session(admin))
        assert r.status_code == 200, r.text
        assert r.json()["code"] == f"C{tag}"
        assert (r.json()["usage_count"], r.json()["total_discount"]) == (0, 0.0)
        assert r.json()["recent_redemptions"] == []

    missing = await client.get(f"{A}/NOPE{tag}", headers=_session(admin))
    assert missing.status_code == 404
    assert missing.json()["code"] == "COUPON_NOT_FOUND"


async def test_each_coupon_has_exactly_one_status_and_filters_match(client, db_session):
    admin = await _admin(db_session, AdminRole.SUPPORT)
    tag = _tag()
    now = datetime.now(UTC)
    await _coupon(db_session, f"ACT{tag}")
    await _coupon(db_session, f"INA{tag}", is_active=False)
    # Inactive wins over expired.
    await _coupon(db_session, f"INX{tag}", is_active=False, valid_until=now - timedelta(days=1))
    await _coupon(
        db_session,
        f"EXP{tag}",
        valid_from=now - timedelta(days=10),
        valid_until=now - timedelta(days=1),
    )
    exhausted = await _coupon(db_session, f"EXH{tag}", usage_limit=1)
    await _redeem(db_session, exhausted)
    # One use of two is still active.
    half = await _coupon(db_session, f"HAF{tag}", usage_limit=2)
    await _redeem(db_session, half)
    await _coupon(
        db_session,
        f"SCH{tag}",
        valid_from=now + timedelta(days=1),
        valid_until=now + timedelta(days=5),
    )

    r = await client.get(A, params={"q": tag.lower()}, headers=_session(admin))
    assert r.status_code == 200, r.text
    statuses = {row["code"]: row["status"] for row in r.json()["items"]}
    assert statuses == {
        f"ACT{tag}": "active",
        f"INA{tag}": "inactive",
        f"INX{tag}": "inactive",
        f"EXP{tag}": "expired",
        f"EXH{tag}": "exhausted",
        f"HAF{tag}": "active",
        f"SCH{tag}": "scheduled",
    }
    assert r.json()["total"] == 7

    for wanted in ("active", "inactive", "expired", "exhausted", "scheduled"):
        r = await client.get(A, params={"q": tag, "status": wanted}, headers=_session(admin))
        assert r.status_code == 200, r.text
        expected = {code for code, s in statuses.items() if s == wanted}
        assert {row["code"] for row in r.json()["items"]} == expected, wanted
        assert r.json()["total"] == len(expected)

    bad = await client.get(A, params={"status": "deleted"}, headers=_session(admin))
    assert bad.status_code == 422


async def test_list_is_newest_first_and_pages(client, db_session):
    admin = await _admin(db_session)
    tag = _tag()
    now = datetime.now(UTC)
    for i in range(3):
        row = await _coupon(db_session, f"N{i}{tag}")
        row.created_at = now - timedelta(minutes=10 - i)
    await db_session.flush()

    first = await client.get(
        A, params={"q": tag, "per_page": 2, "page": 1}, headers=_session(admin)
    )
    second = await client.get(
        A, params={"q": tag, "per_page": 2, "page": 2}, headers=_session(admin)
    )
    assert [r["code"] for r in first.json()["items"]] == [f"N2{tag}", f"N1{tag}"]
    assert [r["code"] for r in second.json()["items"]] == [f"N0{tag}"]
    assert (first.json()["total"], first.json()["page"], first.json()["per_page"]) == (3, 1, 2)


async def test_q_matches_literally(client, db_session):
    admin = await _admin(db_session)
    tag = _tag()
    await _coupon(db_session, f"Q{tag}")
    # A LIKE wildcard in q is a literal, not "match anything".
    r = await client.get(A, params={"q": "%"}, headers=_session(admin))
    assert r.json()["items"] == []
    r = await client.get(A, params={"q": tag[2:6]}, headers=_session(admin))
    assert f"Q{tag}" in {row["code"] for row in r.json()["items"]}


async def test_personal_coupons_are_listed_with_their_owner(client, db_session):
    admin = await _admin(db_session, AdminRole.SUPPORT)
    tag = _tag()
    owner = await UserFactory.create(db_session, role="consumer", name="Referral Owner")
    await _coupon(db_session, f"REF{tag}", user_id=owner.id)
    await _coupon(db_session, f"GLO{tag}")

    every = await client.get(A, params={"q": tag}, headers=_session(admin))
    by_code = {r["code"]: r for r in every.json()["items"]}
    assert by_code[f"REF{tag}"]["user_id"] == str(owner.id)
    assert by_code[f"REF{tag}"]["owner_name"] == "Referral Owner"
    assert by_code[f"REF{tag}"]["owner_phone"] == owner.phone
    assert by_code[f"GLO{tag}"]["owner_name"] is None

    mine = await client.get(A, params={"q": tag, "personal": "true"}, headers=_session(admin))
    assert [r["code"] for r in mine.json()["items"]] == [f"REF{tag}"]
    glob = await client.get(A, params={"q": tag, "personal": "false"}, headers=_session(admin))
    assert [r["code"] for r in glob.json()["items"]] == [f"GLO{tag}"]


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


async def test_patch_saves_and_audits_only_the_changed_fields(client, db_session):
    admin = await _admin(db_session, AdminRole.OPS)
    coupon = await _coupon(db_session, f"E{_tag()}", min_order_amount=200)
    r = await client.patch(
        f"{A}/{coupon.code.upper()}",
        headers=_session(admin),
        # min_order_amount is resent unchanged, so it isn't audited.
        json={"discount_value": 15, "usage_limit": 100, "min_order_amount": 200},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["discount_value"], r.json()["usage_limit"]) == (15.0, 100)

    [audit] = await _rows(db_session, admin)
    assert (audit.action, audit.target_id) == ("coupon.update", str(coupon.id))
    assert audit.before_json == {"discount_value": 10.0, "usage_limit": None}
    assert audit.after_json == {"discount_value": 15.0, "usage_limit": 100}


async def test_a_patch_that_changes_nothing_writes_no_audit_row(client, db_session):
    admin = await _admin(db_session)
    coupon = await _coupon(db_session, f"Z{_tag()}")
    r = await client.patch(
        f"{A}/{coupon.code}", headers=_session(admin), json={"discount_value": 10.00}
    )
    assert r.status_code == 200, r.text
    assert await _rows(db_session, admin) == []


async def test_switching_to_flat_clears_the_cap(client, db_session):
    admin = await _admin(db_session)
    coupon = await _coupon(db_session, f"F{_tag()}", max_discount=150)
    r = await client.patch(
        f"{A}/{coupon.code}",
        headers=_session(admin),
        json={"discount_type": "flat", "discount_value": 300},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["discount_type"], r.json()["max_discount"]) == ("flat", None)
    [audit] = await _rows(db_session, admin)
    assert audit.after_json == {
        "discount_type": "flat",
        "discount_value": 300.0,
        "max_discount": None,
    }

    sent_cap = await client.patch(
        f"{A}/{coupon.code}", headers=_session(admin), json={"max_discount": 50}
    )
    assert sent_cap.status_code == 422
    assert sent_cap.json()["details"]["field"] == "max_discount"


async def test_usage_limit_cant_go_below_usage(client, db_session):
    admin = await _admin(db_session)
    coupon = await _coupon(db_session, f"L{_tag()}", usage_limit=5, usage_limit_per_user=5)
    await _redeem(db_session, coupon)
    await _redeem(db_session, coupon)

    low = await client.patch(f"{A}/{coupon.code}", headers=_session(admin), json={"usage_limit": 1})
    assert low.status_code == 422, low.text
    assert (low.json()["code"], low.json()["details"]["field"]) == (ERROR_CODE, "usage_limit")

    # Equal to usage is allowed: the coupon is then exhausted.
    equal = await client.patch(
        f"{A}/{coupon.code}", headers=_session(admin), json={"usage_limit": 2}
    )
    assert equal.status_code == 200, equal.text
    assert (equal.json()["usage_count"], equal.json()["status"]) == (2, "exhausted")

    unlimited = await client.patch(
        f"{A}/{coupon.code}", headers=_session(admin), json={"usage_limit": None}
    )
    assert (unlimited.json()["usage_limit"], unlimited.json()["status"]) == (None, "active")


async def test_a_coupon_already_over_its_limit_can_still_be_edited(client, db_session):
    """The usage check runs only when the body sends usage_limit."""
    admin = await _admin(db_session)
    coupon = await _coupon(db_session, f"W{_tag()}", usage_limit=1, usage_limit_per_user=5)
    await _redeem(db_session, coupon)
    await _redeem(db_session, coupon)
    r = await client.patch(
        f"{A}/{coupon.code}", headers=_session(admin), json={"min_order_amount": 300}
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "body"),
        ({"discount_value": None}, "discount_value"),
        ({"valid_until": None}, "valid_until"),
        ({"discount_type": None}, "discount_type"),
        ({"usage_limit_per_user": None}, "usage_limit_per_user"),
        ({"discount_value": 150}, "discount_value"),
        # Checked against the merged row: the existing valid_until is 30 days out.
        ({"valid_from": _iso(timedelta(days=40))}, "valid_until"),
        ({"usage_limit_per_user": 0}, "usage_limit_per_user"),
        ({"max_discount": -1}, "max_discount"),
    ],
)
async def test_patch_refusals(client, db_session, body, field):
    admin = await _admin(db_session)
    coupon = await _coupon(db_session, f"R{_tag()}")
    r = await client.patch(f"{A}/{coupon.code}", headers=_session(admin), json=body)
    assert r.status_code == 422, r.text
    assert (r.json()["code"], r.json()["details"]["field"]) == (ERROR_CODE, field)
    assert await _rows(db_session, admin) == []


async def test_patch_refuses_code_user_id_and_is_active(client, db_session):
    admin = await _admin(db_session)
    coupon = await _coupon(db_session, f"K{_tag()}")
    for body in ({"code": "NEWCODE"}, {"user_id": str(uuid.uuid4())}, {"is_active": False}):
        r = await client.patch(f"{A}/{coupon.code}", headers=_session(admin), json=body)
        assert r.status_code == 422, body


async def test_patch_unknown_code_is_404_before_the_body_check(client, db_session):
    admin = await _admin(db_session)
    r = await client.patch(f"{A}/NOPE{_tag()}", headers=_session(admin), json={})
    assert r.status_code == 404
    assert r.json()["code"] == "COUPON_NOT_FOUND"


async def test_ending_a_coupon_early_by_patch_is_allowed(client, db_session):
    admin = await _admin(db_session)
    coupon = await _coupon(db_session, f"Y{_tag()}")
    r = await client.patch(
        f"{A}/{coupon.code}",
        headers=_session(admin),
        json={"valid_until": _iso(timedelta(minutes=-1))},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "expired"


async def test_a_personal_coupon_is_editable(client, db_session):
    admin = await _admin(db_session)
    owner = await UserFactory.create(db_session, role="consumer")
    coupon = await _coupon(db_session, f"G{_tag()}", user_id=owner.id)
    r = await client.patch(
        f"{A}/{coupon.code}", headers=_session(admin), json={"discount_value": 25}
    )
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == str(owner.id)


# ---------------------------------------------------------------------------
# Deactivate / activate
# ---------------------------------------------------------------------------


async def test_deactivate_and_activate_are_idempotent_and_audited_once(client, db_session):
    admin = await _admin(db_session, AdminRole.OPS)
    owner = await UserFactory.create(db_session, role="consumer")
    coupon = await _coupon(db_session, f"H{_tag()}", user_id=owner.id)

    off = await client.post(f"{A}/{coupon.code}/deactivate", headers=_session(admin))
    assert off.status_code == 200, off.text
    assert (off.json()["is_active"], off.json()["status"]) == (False, "inactive")
    again = await client.post(f"{A}/{coupon.code.upper()}/deactivate", headers=_session(admin))
    assert again.status_code == 200
    [row] = await _rows(db_session, admin)
    assert (row.action, row.target_id) == ("coupon.deactivate", str(coupon.id))
    assert (row.before_json, row.after_json) == ({"is_active": True}, {"is_active": False})

    on = await client.post(f"{A}/{coupon.code}/activate", headers=_session(admin))
    assert (on.json()["is_active"], on.json()["status"]) == (True, "active")
    await client.post(f"{A}/{coupon.code}/activate", headers=_session(admin))
    rows = await _rows(db_session, admin)
    # One transaction, so both rows share created_at: compare unordered.
    assert sorted(r.action for r in rows) == ["coupon.activate", "coupon.deactivate"]

    missing = await client.post(f"{A}/NOPE{_tag()}/deactivate", headers=_session(admin))
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


async def test_support_reads_but_cant_write(client, db_session):
    support = await _admin(db_session, AdminRole.SUPPORT)
    coupon = await _coupon(db_session, f"J{_tag()}")
    h = _session(support)

    assert (await client.get(A, headers=h)).status_code == 200
    assert (await client.get(f"{A}/{coupon.code}", headers=h)).status_code == 200
    writes = [
        await client.post(A, headers=h, json=_body(f"J{_tag()}")),
        await client.patch(f"{A}/{coupon.code}", headers=h, json={"discount_value": 20}),
        await client.post(f"{A}/{coupon.code}/deactivate", headers=h),
        await client.post(f"{A}/{coupon.code}/activate", headers=h),
    ]
    assert [w.status_code for w in writes] == [403, 403, 403, 403]
    assert await _rows(db_session, support) == []


async def test_a_consumer_token_is_refused(client, db_session, auth_headers):
    consumer = await UserFactory.create(db_session, role="consumer")
    r = await client.get(A, headers=auth_headers(str(consumer.id), "consumer"))
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# The consumer side is unchanged
# ---------------------------------------------------------------------------


async def test_consumer_endpoints_see_admin_created_and_deactivated_coupons(
    client, db_session, auth_headers
):
    admin = await _admin(db_session)
    consumer, _ = await _consumer_with_address(db_session)
    headers = auth_headers(str(consumer.id), "consumer")
    code = f"B{_tag()}"
    created = await client.post(
        A, headers=_session(admin), json=_body(code, discount_type="flat", discount_value=75)
    )
    assert created.status_code == 201, created.text

    available = await client.get("/api/v1/coupons/available", headers=headers)
    assert code.lower() in {c["code"] for c in available.json()}
    for typed in (code, code.lower()):
        ok = await client.post(
            "/api/v1/coupons/validate", headers=headers, json={"code": typed, "cart_amount": 500}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["discount_amount"] == 75.0

    await client.post(f"{A}/{code}/deactivate", headers=_session(admin))
    available = await client.get("/api/v1/coupons/available", headers=headers)
    assert code.lower() not in {c["code"] for c in available.json()}
    refused = await client.post(
        "/api/v1/coupons/validate", headers=headers, json={"code": code, "cart_amount": 500}
    )
    assert refused.status_code == 400

    with pytest.raises(CouponInvalid):
        await CouponService(db_session).validate(code, consumer.id, 500)
