"""
SVC-481: the admin variant editor (/admin/catalog/services/{id}/variants*).

- create / PATCH: every value rule, GST read-only (copied on create, refused in a
  body), label unique per service in any case, empty and no-op saves;
- the commission percent: amounts rounded half up to paise (a half-paisa case),
  the "current %" rule on a whole-rupee variant (₹240 on ₹799 = 30.0375%), and
  the 14-day-notice 409 on any increase (SVC-486: confirm is ignored; the
  scheduling itself is in tests/test_commission_changes.py);
- switch off / on: the last-active 409, idempotence;
- the service's base_price follows its cheapest active variant after every
  write, in the same audit row;
- SP prices and existing bookings don't move; a switched-off variant can't be
  booked and isn't ranked, and switching it back on brings its SP prices back;
- price-impact: the counted population and a price sitting exactly on a bound.

Other files commit rows, so every read is scoped to this test's own ids.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.address import Address
from app.models.admin_action import ADMIN_ACTION_TARGET_TYPES, ADMIN_ACTIONS
from app.models.booking_item import BookingItem
from app.models.provider_profile import KYCStatus
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.schemas.booking import CreateBookingItemRequest
from app.services.booking_service import BookingService
from tests.factories.user_factory import UserFactory
from tests.test_admin_catalog import A, _audit, _cat, _refused, _svc
from tests.test_admin_console_auth import _admin, _session
from tests.test_service_ranking import URL, _ids, _sheet, _sp
from tests.test_service_ranking import _service as _ranked_service

NOTICE = "COMMISSION_INCREASE_NEEDS_NOTICE"


async def _variant(
    db,
    service: Service,
    price: str,
    commission: str,
    *,
    label: str | None = None,
    sort_order: int = 0,
    is_active: bool = True,
    gst: str = "0",
) -> ServiceVariant:
    price_d, commission_d = Decimal(price), Decimal(commission)
    row = ServiceVariant(
        service_id=service.id,
        label=label or f"V {uuid.uuid4().hex[:6]}",
        base_price=price_d,
        platform_commission=commission_d,
        provider_payout=price_d - commission_d,
        gst_percent=Decimal(gst),
        sort_order=sort_order,
        is_active=is_active,
    )
    db.add(row)
    await db.flush()
    return row


async def _fresh(db, row):
    await db.refresh(row)
    return row


def _money(v: ServiceVariant) -> tuple:
    return (v.base_price, v.platform_commission, v.provider_payout)


def _url(service, variant=None, suffix: str = "") -> str:
    url = f"{A}/services/{service.id}/variants"
    if variant is not None:
        url += f"/{variant.id}"
    return url + suffix


def _row(body: dict, variant_id) -> dict:
    return next(v for v in body["variants"] if v["id"] == str(variant_id))


async def _service_with(db, *prices_and_commissions, base="2499.00"):
    service = await _svc(db, await _cat(db), base_price=Decimal(base))
    variants = [
        await _variant(db, service, p, c, sort_order=i)
        for i, (p, c) in enumerate(prices_and_commissions)
    ]
    return service, variants


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_stores_amounts_copies_gst_and_goes_last(client, db_session):
    admin = await _admin(db_session)
    service, (first, second) = await _service_with(
        db_session, ("2499.00", "749.70"), ("2999.00", "899.70")
    )
    first.gst_percent = second.gst_percent = Decimal("18")
    await db_session.flush()

    r = await client.post(
        _url(service),
        headers=_session(admin),
        json={"label": " 3 BHK ", "base_price": "3499", "commission_pct": "25"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == str(service.id)
    assert body["priced_per_variant"] is True
    new = next(v for v in body["variants"] if v["label"] == "3 BHK")
    assert new == {
        "id": new["id"],
        "label": "3 BHK",
        "subcategory": None,
        "base_price": 3499.0,
        "duration_minutes": None,
        "sort_order": 2,
        "is_active": True,
        "platform_commission": 874.75,
        "provider_payout": 2624.25,
        "commission_pct": 25.0,
        "gst_percent": 18.0,
        "pending_commission_change": None,
    }
    [row] = await _audit(db_session, admin)
    assert (row.action, row.target_type, row.target_id) == (
        "variant.create",
        "service_variant",
        new["id"],
    )
    assert row.before_json is None  # the cheapest active is still 2499
    assert row.after_json["platform_commission"] == 874.75
    assert row.details == {"service_id": str(service.id), "commission_pct": 25.0}


async def test_create_takes_duration_and_sort_order(client, db_session):
    admin = await _admin(db_session)
    service, _ = await _service_with(db_session, ("2499.00", "749.70"))
    r = await client.post(
        _url(service),
        headers=_session(admin),
        json={
            "label": "Big",
            "base_price": "2999",
            "commission_pct": "0",
            "duration_minutes": 90,
            "sort_order": 0,
        },
    )
    assert r.status_code == 201, r.text
    new = next(v for v in r.json()["variants"] if v["label"] == "Big")
    assert (new["duration_minutes"], new["sort_order"]) == (90, 0)
    assert (new["platform_commission"], new["provider_payout"]) == (0.0, 2999.0)


async def test_create_on_a_service_with_no_variant_gets_gst_0(client, db_session):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session), base_price=Decimal("500.00"))
    r = await client.post(
        _url(service),
        headers=_session(admin),
        json={"label": "Only", "base_price": "400", "commission_pct": "99.99"},
    )
    assert r.status_code == 201, r.text
    [v] = r.json()["variants"]
    assert (v["gst_percent"], v["sort_order"], v["commission_pct"]) == (0.0, 0, 99.99)
    # The base price followed it down, in the same row.
    assert r.json()["base_price"] == 400.0
    [row] = await _audit(db_session, admin)
    assert row.before_json == {"service_base_price": 500.0}
    assert row.after_json["service_base_price"] == 400.0


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"base_price": 0}, "base_price"),
        ({"base_price": -5}, "base_price"),
        ({"base_price": "1000000.01"}, "base_price"),
        ({"base_price": "10.005"}, "base_price"),
        ({"commission_pct": -1}, "commission_pct"),
        ({"commission_pct": 100}, "commission_pct"),
        ({"commission_pct": "12.345"}, "commission_pct"),
        ({"label": ""}, "label"),
        ({"label": "   "}, "label"),
        ({"label": "x" * 201}, "label"),
        ({"duration_minutes": 14}, "duration_minutes"),
        ({"duration_minutes": 481}, "duration_minutes"),
        ({"sort_order": -1}, "sort_order"),
        ({"sort_order": 10000}, "sort_order"),
        ({"gst_percent": 18}, "gst_percent"),
    ],
)
async def test_create_value_rules(client, db_session, override, field):
    admin = await _admin(db_session)
    service, _ = await _service_with(db_session, ("2499.00", "749.70"))
    body = {"label": "New", "base_price": "999", "commission_pct": "20", **override}
    _refused(await client.post(_url(service), headers=_session(admin), json=body), field)
    assert await _audit(db_session, admin) == []


async def test_create_bounds_are_inclusive(client, db_session):
    admin = await _admin(db_session)
    service, _ = await _service_with(db_session, ("2499.00", "749.70"))
    for i, body in enumerate(
        [
            {"label": "x" * 200, "base_price": "1000000", "commission_pct": "99.99"},
            {"label": "y", "base_price": "0.01", "commission_pct": "0", "duration_minutes": 15},
            {"label": "z", "base_price": "1", "commission_pct": "1", "sort_order": 9999},
        ]
    ):
        r = await client.post(_url(service), headers=_session(admin), json=body)
        assert r.status_code == 201, (i, r.text)


@pytest.mark.parametrize("label", ["1 BHK", "1 bhk", "  1 Bhk  "])
async def test_create_refuses_a_label_the_service_has_in_any_case(client, db_session, label):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session))
    await _variant(db_session, service, "2499.00", "749.70", label="1 BHK")
    r = await client.post(
        _url(service),
        headers=_session(admin),
        json={"label": label, "base_price": "999", "commission_pct": "20"},
    )
    _refused(r, "label", status=409, code="VARIANT_LABEL_TAKEN")


async def test_the_same_label_on_another_service_is_fine(client, db_session):
    admin = await _admin(db_session)
    cat = await _cat(db_session)
    other = await _svc(db_session, cat)
    await _variant(db_session, other, "2499.00", "749.70", label="1 BHK")
    service = await _svc(db_session, cat)
    r = await client.post(
        _url(service),
        headers=_session(admin),
        json={"label": "1 BHK", "base_price": "999", "commission_pct": "20"},
    )
    assert r.status_code == 201, r.text


async def test_create_never_needs_confirm(client, db_session):
    admin = await _admin(db_session)
    service, _ = await _service_with(db_session, ("1000.00", "100.00"))
    r = await client.post(
        _url(service),
        headers=_session(admin),
        json={"label": "Pricey", "base_price": "1000", "commission_pct": "50"},
    )
    assert r.status_code == 201, r.text


async def test_unknown_service_is_404(client, db_session):
    admin = await _admin(db_session)
    r = await client.post(
        f"{A}/services/{uuid.uuid4()}/variants",
        headers=_session(admin),
        json={"label": "x", "base_price": "1", "commission_pct": "1"},
    )
    assert (r.status_code, r.json()["code"]) == (404, "SERVICE_NOT_FOUND")


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "body"),
        ({"confirm": True}, "body"),
        ({"label": None}, "label"),
        ({"base_price": None}, "base_price"),
        ({"commission_pct": None}, "commission_pct"),
        ({"duration_minutes": None}, "duration_minutes"),
        ({"sort_order": None}, "sort_order"),
        ({"base_price": 0}, "base_price"),
        ({"base_price": "10.005"}, "base_price"),
        ({"commission_pct": 100}, "commission_pct"),
        ({"commission_pct": "-0.01"}, "commission_pct"),
        ({"label": " "}, "label"),
        ({"duration_minutes": 481}, "duration_minutes"),
        ({"sort_order": 10000}, "sort_order"),
        ({"gst_percent": 18}, "gst_percent"),
        ({"gst_percent": None}, "gst_percent"),
    ],
)
async def test_patch_value_rules(client, db_session, body, field):
    admin = await _admin(db_session)
    service, (v,) = await _service_with(db_session, ("2499.00", "749.70"))
    before = _money(v)
    _refused(await client.patch(_url(service, v), headers=_session(admin), json=body), field)
    assert _money(await _fresh(db_session, v)) == before
    assert await _audit(db_session, admin) == []


async def test_patch_unknown_fields_and_is_active_are_refused(client, db_session):
    admin = await _admin(db_session)
    service, (v,) = await _service_with(db_session, ("2499.00", "749.70"))
    for body in ({"is_active": False}, {"subcategory": "x"}, {"platform_commission": 1}):
        r = await client.patch(_url(service, v), headers=_session(admin), json=body)
        assert r.status_code == 422, r.text


async def test_patch_edits_label_duration_and_sort_order(client, db_session):
    admin = await _admin(db_session)
    service, (v,) = await _service_with(db_session, ("2499.00", "749.70"))
    old_label = v.label
    r = await client.patch(
        _url(service, v),
        headers=_session(admin),
        json={"label": "Studio", "duration_minutes": 120, "sort_order": 4},
    )
    assert r.status_code == 200, r.text
    row = _row(r.json(), v.id)
    assert (row["label"], row["duration_minutes"], row["sort_order"]) == ("Studio", 120, 4)
    [audit] = await _audit(db_session, admin)
    assert audit.before_json == {
        "label": old_label,
        "duration_minutes": None,
        "sort_order": 0,
    }
    assert audit.after_json == {"label": "Studio", "duration_minutes": 120, "sort_order": 4}


async def test_patch_label_clash_is_409_but_its_own_label_in_another_case_is_fine(
    client, db_session
):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session))
    one = await _variant(db_session, service, "2499.00", "749.70", label="1 BHK")
    two = await _variant(db_session, service, "2999.00", "899.70", label="2 BHK", sort_order=1)
    r = await client.patch(_url(service, two), headers=_session(admin), json={"label": "1 bhk"})
    _refused(r, "label", status=409, code="VARIANT_LABEL_TAKEN")
    r = await client.patch(_url(service, one), headers=_session(admin), json={"label": "1 bhk"})
    assert r.status_code == 200, r.text
    assert _row(r.json(), one.id)["label"] == "1 bhk"


async def test_a_save_that_changes_nothing_writes_no_row(client, db_session):
    admin = await _admin(db_session)
    service, (v,) = await _service_with(db_session, ("2499.00", "749.70"))
    r = await client.patch(
        _url(service, v),
        headers=_session(admin),
        json={"label": f"  {v.label}", "base_price": "2499.00", "commission_pct": "30"},
    )
    assert r.status_code == 200, r.text
    assert await _audit(db_session, admin) == []


async def test_a_variant_of_another_service_is_404(client, db_session):
    admin = await _admin(db_session)
    service, _ = await _service_with(db_session, ("2499.00", "749.70"))
    _, (foreign,) = await _service_with(db_session, ("999.00", "99.90"))
    headers = _session(admin)
    for r in (
        await client.patch(_url(service, foreign), headers=headers, json={"label": "x"}),
        await client.post(_url(service, foreign, "/deactivate"), headers=headers),
        await client.post(_url(service, foreign, "/activate"), headers=headers),
        await client.get(
            _url(service, foreign, "/price-impact"), headers=headers, params={"base_price": 1}
        ),
    ):
        assert (r.status_code, r.json()["code"]) == (404, "VARIANT_NOT_FOUND"), r.text


# ---------------------------------------------------------------------------
# The commission percent
# ---------------------------------------------------------------------------


async def _ac_installation(db):
    """₹240 on ₹799: 30.0375%, shown 30.04 (the beta catalogue's shape)."""
    return await _service_with(db, ("799.00", "240.00"), base="799.00")


async def test_the_response_shows_the_percent_rounded_half_up(client, db_session):
    admin = await _admin(db_session)
    service, (v,) = await _ac_installation(db_session)
    r = await client.get(f"{A}/services/{service.id}", headers=_session(admin))
    row = _row(r.json(), v.id)
    assert (row["commission_pct"], row["platform_commission"], row["provider_payout"]) == (
        30.04,
        240.0,
        559.0,
    )
    assert r.json()["priced_per_variant"] is False


@pytest.mark.parametrize(
    "body",
    [{"base_price": "999"}, {"base_price": "999", "commission_pct": "30.04"}],
    ids=["price_only", "price_and_the_shown_pct"],
)
async def test_a_price_edit_keeps_the_exact_stored_ratio(client, db_session, body):
    """30.0375% of 999 = 300.0751 → 300.08. At 30.04% it would be 300.10."""
    admin = await _admin(db_session)
    service, (v,) = await _ac_installation(db_session)
    r = await client.patch(_url(service, v), headers=_session(admin), json=body)
    assert r.status_code == 200, r.text
    assert _money(await _fresh(db_session, v)) == (
        Decimal("999.00"),
        Decimal("300.08"),
        Decimal("698.92"),
    )
    [row] = await _audit(db_session, admin)
    assert "old_pct" not in row.details  # no change of percent


async def test_sending_the_shown_pct_alone_is_no_change(client, db_session):
    admin = await _admin(db_session)
    service, (v,) = await _ac_installation(db_session)
    r = await client.patch(
        _url(service, v), headers=_session(admin), json={"commission_pct": "30.04"}
    )
    assert r.status_code == 200, r.text
    assert _money(await _fresh(db_session, v)) == (
        Decimal("799.00"),
        Decimal("240.00"),
        Decimal("559.00"),
    )
    assert await _audit(db_session, admin) == []


async def test_a_decrease_applies_at_once(client, db_session):
    admin = await _admin(db_session)
    service, (v,) = await _ac_installation(db_session)
    r = await client.patch(_url(service, v), headers=_session(admin), json={"commission_pct": 25})
    assert r.status_code == 200, r.text
    assert _money(await _fresh(db_session, v)) == (
        Decimal("799.00"),
        Decimal("199.75"),
        Decimal("599.25"),
    )
    [row] = await _audit(db_session, admin)
    assert row.details == {
        "service_id": str(service.id),
        "old_pct": 30.04,
        "new_pct": 25.0,
        "commission_change_id": row.details["commission_change_id"],
    }
    assert row.before_json == {"platform_commission": 240.0, "provider_payout": 559.0}
    assert row.after_json == {"platform_commission": 199.75, "provider_payout": 599.25}


@pytest.mark.parametrize("new_pct", ["30.05", "31", "99.99"])
async def test_an_increase_needs_notice(client, db_session, new_pct):
    admin = await _admin(db_session)
    service, (v,) = await _ac_installation(db_session)
    r = await client.patch(
        _url(service, v),
        headers=_session(admin),
        json={"commission_pct": new_pct, "base_price": "899"},
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == NOTICE
    assert body["details"] == {
        "old_pct": 30.04,
        "new_pct": float(new_pct),
        "notice_days": 14,
        "earliest_effective_from": body["details"]["earliest_effective_from"],
        "schedule_path": "commission-changes",
    }
    assert "Schedule a commission increase at least 14 days ahead" in body.values()
    assert _money(await _fresh(db_session, v))[0] == Decimal("799.00")
    assert await _audit(db_session, admin) == []


async def test_confirm_no_longer_lets_an_increase_through(client, db_session):
    """SVC-486: ``confirm`` is still accepted (no 422) but never read."""
    admin = await _admin(db_session)
    service, (v,) = await _ac_installation(db_session)
    r = await client.patch(
        _url(service, v),
        headers=_session(admin),
        json={"commission_pct": "35", "base_price": "800", "confirm": True},
    )
    assert r.status_code == 409, r.text
    assert r.json()["code"] == NOTICE
    assert _money(await _fresh(db_session, v))[0] == Decimal("799.00")
    assert await _audit(db_session, admin) == []

    ok = await client.patch(
        _url(service, v), headers=_session(admin), json={"label": "AC", "confirm": True}
    )
    assert ok.status_code == 200, ok.text


async def test_a_label_clash_is_reported_before_the_notice(client, db_session):
    admin = await _admin(db_session)
    service = await _svc(db_session, await _cat(db_session))
    await _variant(db_session, service, "2499.00", "749.70", label="Taken")
    v = await _variant(db_session, service, "799.00", "240.00", sort_order=1)
    r = await client.patch(
        _url(service, v), headers=_session(admin), json={"label": "taken", "commission_pct": 50}
    )
    _refused(r, "label", status=409, code="VARIANT_LABEL_TAKEN")


async def test_the_commission_rounds_half_up_at_a_half_paisa(client, db_session):
    """12.5% of ₹101 is 12.625: half up gives 12.63 (banker's rounding 12.62)."""
    admin = await _admin(db_session)
    service, (v,) = await _service_with(db_session, ("101.00", "20.20"), base="101.00")
    r = await client.patch(
        _url(service, v), headers=_session(admin), json={"commission_pct": "12.5"}
    )
    assert r.status_code == 200, r.text
    assert _money(await _fresh(db_session, v)) == (
        Decimal("101.00"),
        Decimal("12.63"),
        Decimal("88.37"),
    )


async def test_commission_plus_payout_is_always_the_price(client, db_session):
    admin = await _admin(db_session)
    service, (v,) = await _service_with(db_session, ("1.00", "0.99"), base="1.00")
    for body in (
        {"base_price": "333.33", "commission_pct": "33.33"},
        {"base_price": "0.03"},
        {"base_price": "999999.99", "commission_pct": "0.01"},
    ):
        r = await client.patch(_url(service, v), headers=_session(admin), json=body)
        assert r.status_code == 200, r.text
        price, commission, payout = _money(await _fresh(db_session, v))
        assert commission + payout == price


# ---------------------------------------------------------------------------
# Switch off / on, and the service's base price
# ---------------------------------------------------------------------------


async def test_the_last_active_variant_cant_be_switched_off(client, db_session):
    admin = await _admin(db_session)
    service, (on, off) = await _service_with(
        db_session, ("2499.00", "749.70"), ("2999.00", "899.70")
    )
    off.is_active = False
    await db_session.flush()
    r = await client.post(_url(service, on, "/deactivate"), headers=_session(admin))
    assert (r.status_code, r.json()["code"]) == (409, "LAST_ACTIVE_VARIANT"), r.text
    assert (await _fresh(db_session, on)).is_active is True
    assert await _audit(db_session, admin) == []


async def test_switching_is_idempotent(client, db_session):
    admin = await _admin(db_session)
    service, (on, off) = await _service_with(
        db_session, ("2499.00", "749.70"), ("2999.00", "899.70")
    )
    off.is_active = False
    await db_session.flush()
    headers = _session(admin)
    assert (await client.post(_url(service, on, "/activate"), headers=headers)).status_code == 200
    r = await client.post(_url(service, off, "/deactivate"), headers=headers)
    assert r.status_code == 200
    assert await _audit(db_session, admin) == []


async def test_base_price_follows_the_cheapest_active_variant(client, db_session):
    admin = await _admin(db_session)
    headers = _session(admin)
    service, (one, two) = await _service_with(
        db_session, ("2499.00", "749.70"), ("2999.00", "899.70"), base="2799.00"
    )
    seen: set = set()

    async def step(response, action, before, after):
        """The response, the stored price, and the one new audit row agree."""
        assert response.status_code in (200, 201), response.text
        assert response.json()["base_price"] == after
        assert (await _fresh(db_session, service)).base_price == Decimal(str(after))
        [row] = [a for a in await _audit(db_session, admin) if a.id not in seen]
        seen.add(row.id)
        assert row.action == action
        assert (row.before_json["service_base_price"], row.after_json["service_base_price"]) == (
            before,
            after,
        )

    # Any write corrects a stale base price (the ticket's 2,799 example).
    r = await client.patch(_url(service, two), headers=headers, json={"sort_order": 5})
    await step(r, "variant.update", 2799.0, 2499.0)

    r = await client.post(
        _url(service),
        headers=headers,
        json={"label": "Studio", "base_price": "1999", "commission_pct": "30"},
    )
    await step(r, "variant.create", 2499.0, 1999.0)
    studio = next(v["id"] for v in r.json()["variants"] if v["label"] == "Studio")

    r = await client.patch(
        f"{A}/services/{service.id}/variants/{studio}", headers=headers, json={"base_price": 3999}
    )
    await step(r, "variant.update", 1999.0, 2499.0)

    r = await client.post(_url(service, one, "/deactivate"), headers=headers)
    await step(r, "variant.deactivate", 2499.0, 2999.0)

    r = await client.post(_url(service, one, "/activate"), headers=headers)
    await step(r, "variant.activate", 2999.0, 2499.0)


async def test_editing_a_switched_off_variant_doesnt_move_the_base_price(client, db_session):
    admin = await _admin(db_session)
    service, (on, off) = await _service_with(
        db_session, ("2499.00", "749.70"), ("2999.00", "899.70")
    )
    off.is_active = False
    await db_session.flush()
    r = await client.patch(_url(service, off), headers=_session(admin), json={"base_price": 99})
    assert r.status_code == 200, r.text
    assert r.json()["base_price"] == 2499.0
    [row] = await _audit(db_session, admin)
    assert "service_base_price" not in row.after_json


# ---------------------------------------------------------------------------
# SP prices, bookings, dispatch
# ---------------------------------------------------------------------------


async def _consumer_headers(db, auth_headers):
    consumer = await UserFactory.create(db, role="consumer")
    return auth_headers(str(consumer.id), "consumer")


async def _book(db, service, variant):
    consumer = await UserFactory.create(db, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="1 Test Rd")
    db.add(address)
    await db.flush()
    return await BookingService(db).create_booking(
        consumer_id=consumer.id,
        items=[
            CreateBookingItemRequest(
                service_id=service.id, service_variant_id=variant.id, quantity=1
            )
        ],
        address_id=address.id,
    )


async def test_a_price_edit_leaves_sp_prices_and_bookings_alone(client, db_session):
    admin = await _admin(db_session)
    service = await _ranked_service(db_session, base_price=1000)
    variant = await db_session.scalar(
        select(ServiceVariant).where(ServiceVariant.service_id == service.id)
    )
    sp = await _sp(db_session, service, price=1100)
    booking = await _book(db_session, service, variant)
    item_before = await db_session.scalar(
        select(BookingItem.unit_price).where(BookingItem.booking_id == booking.id)
    )

    r = await client.patch(
        _url(service, variant),
        headers=_session(admin),
        json={"base_price": "1500", "commission_pct": "10"},
    )
    assert r.status_code == 200, r.text

    price = await db_session.scalar(
        select(SpServicePricing.price_amount)
        .where(SpServicePricing.sp_id == sp.id)
        .execution_options(populate_existing=True)
    )
    assert price == Decimal("1100.00")
    item = await db_session.scalar(
        select(BookingItem)
        .where(BookingItem.booking_id == booking.id)
        .execution_options(populate_existing=True)
    )
    assert item.unit_price == item_before == Decimal("1000.00")


async def test_a_switched_off_variant_is_unbookable_and_unranked_until_back_on(
    client, db_session, auth_headers
):
    admin = await _admin(db_session)
    service = await _ranked_service(db_session, base_price=1000)
    standard = await db_session.scalar(
        select(ServiceVariant).where(ServiceVariant.service_id == service.id)
    )
    big = await _variant(db_session, service, "2000.00", "400.00", label="Big", sort_order=1)
    on_standard = await _sp(db_session, service)
    on_big = await _sp(db_session, service, variant=big, price=2000)
    consumer = await _consumer_headers(db_session, auth_headers)
    assert _ids(await _sheet(client, consumer, service, variant_id=str(big.id))) == [str(on_big.id)]

    r = await client.post(_url(service, big, "/deactivate"), headers=_session(admin))
    assert r.status_code == 200, r.text

    with pytest.raises(ValueError, match="inactive"):
        await _book(db_session, service, big)
    resp = await client.get(
        URL.format(service.id),
        params={"lat": 12.9784, "lng": 77.6408, "variant_id": str(big.id)},
        headers=consumer,
    )
    assert (resp.status_code, resp.json()["code"]) == (404, "VARIANT_NOT_FOUND")
    assert str(on_big.id) not in _ids(await _sheet(client, consumer, service))
    assert str(on_standard.id) in _ids(await _sheet(client, consumer, service))

    # Its SP price was kept, so switching it back on brings the SP back.
    r = await client.post(_url(service, big, "/activate"), headers=_session(admin))
    assert r.status_code == 200, r.text
    assert _ids(await _sheet(client, consumer, service, variant_id=str(big.id))) == [str(on_big.id)]
    assert standard.is_active


# ---------------------------------------------------------------------------
# price-impact
# ---------------------------------------------------------------------------


async def _impact(client, admin, service, variant, price):
    r = await client.get(
        _url(service, variant, "/price-impact"),
        headers=_session(admin),
        params={"base_price": price},
    )
    return r


async def _impact_setup(db, *, sensitive=True):
    """Reference 1000, ±10%: 900 and 1100 sit exactly on the bounds."""
    service = await _ranked_service(db, base_price=1000, sensitive=sensitive, bounds_pct="10")
    variant = await db.scalar(select(ServiceVariant).where(ServiceVariant.service_id == service.id))
    for price in (900, 1100, 1200):
        await _sp(db, service, price=price)
    # Not counted: KYC not verified, account off, price switched off.
    await _sp(db, service, price=1200, kyc=KYCStatus.SUBMITTED)
    await _sp(db, service, price=1200, account_active=False)
    await _sp(db, service, price=1200, price_active=False)
    return service, variant


async def test_price_impact_counts_moves_at_the_range_edges(client, db_session):
    admin = await _admin(db_session)
    service, variant = await _impact_setup(db_session)

    # 1100 → range 990–1210: 900 moves out, 1100 stays in, 1200 moves in.
    r = await _impact(client, admin, service, variant, "1100")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "sp_prices": 3,
        "outside_now": 1,
        "outside_after": 1,
        "moving_out": 1,
        "moving_in": 1,
    }
    # The same price: nothing moves.
    r = await _impact(client, admin, service, variant, "1000")
    assert r.json() == {
        "sp_prices": 3,
        "outside_now": 1,
        "outside_after": 1,
        "moving_out": 0,
        "moving_in": 0,
    }
    # 999.99 → 899.991–1099.989: 900 still in, 1100 (on today's bound) moves out.
    r = await _impact(client, admin, service, variant, "999.99")
    assert (r.json()["moving_out"], r.json()["outside_after"]) == (1, 2)
    assert await _audit(db_session, admin) == []
    assert (await _fresh(db_session, variant)).base_price == Decimal("1000.00")


async def test_price_impact_without_a_range_moves_nothing(client, db_session):
    admin = await _admin(db_session)
    service, variant = await _impact_setup(db_session, sensitive=False)
    r = await _impact(client, admin, service, variant, "5000")
    assert r.json() == {
        "sp_prices": 3,
        "outside_now": 0,
        "outside_after": 0,
        "moving_out": 0,
        "moving_in": 0,
    }


@pytest.mark.parametrize("price", ["abc", "0", "-1", "10.005", "1000000.01", "NaN"])
async def test_price_impact_validates_the_price(client, db_session, price):
    admin = await _admin(db_session)
    service, (v,) = await _service_with(db_session, ("2499.00", "749.70"))
    _refused(await _impact(client, admin, service, v, price), "base_price")


def test_the_variant_actions_are_registered():
    assert "service_variant" in ADMIN_ACTION_TARGET_TYPES
    assert {"variant.create", "variant.update", "variant.deactivate", "variant.activate"} <= set(
        ADMIN_ACTIONS
    )
