"""
SVC-412: the SP's own pricing screen, ``GET /providers/me/pricing`` and
``PATCH /providers/me/pricing/{variant_id}``, plus ``commission.take_home``.

- Per variant: platform price, the sensitive category's range (else null), the
  SP's price and on/off (null until set), the out-of-range flag and the
  take-home breakdown (commission, GST on it, what the SP receives).
- The PATCH is one insert-or-update on (SP, variant). An out-of-range price is
  saved and flagged. ``is_active`` alone needs a price (422 ``PRICE_REQUIRED``).
  A variant that's off, or of a service the SP doesn't subscribe to, is a 404.
- A team Pro signs in as a consumer, so ``require_role("provider")`` refuses it.
- Switching a variant off takes the SP off the customer's sheet for it; on
  brings it back at the same price.
- Unsubscribing switches the service's prices off and keeps them; subscribing
  again leaves them off.

The AC gas charging example throughout: ₹2,499, ₹750 of it the platform's, in
a sensitive category at ±25% (₹1,874.25 – ₹3,123.75).
"""

import asyncio
import logging
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.exceptions import SubscriptionNotFound
from app.models.category import Category
from app.models.professional import Professional
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.services.commission import commission_pct, split_at_price, take_home
from app.services.provider_service import ProviderService
from app.services.sp_pricing_service import SpPricingService
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_service_ranking import LAT, LNG
from tests.test_service_ranking import _sp as _ranked_sp

PRICING = "/api/v1/providers/me/pricing"
SHEET = "/api/v1/services/{}/providers"

BASE = Decimal("2499.00")
COMMISSION = Decimal("750.00")
LOW, HIGH = Decimal("1874.25"), Decimal("3123.75")


async def _service(
    db,
    *,
    sensitive=True,
    bounds_pct="25",
    gst_pct="18",
    name="AC gas charging",
    active=True,
):
    """A service in its own category with one variant, ``Standard``, at
    ``BASE`` / ``COMMISSION``. Returns ``(service, variant)``."""
    slug = f"spp-{uuid.uuid4().hex[:8]}"
    category = Category(
        name=slug,
        slug=slug,
        is_active=True,
        sort_order=1,
        is_sensitive=sensitive,
        bounds_pct=Decimal(bounds_pct) if sensitive else None,
        commission_gst_pct=Decimal(gst_pct),
    )
    db.add(category)
    await db.flush()
    service = Service(
        id=uuid.uuid4(),
        category_id=category.id,
        name=name,
        base_price=BASE,
        duration_minutes=60,
        is_active=active,
    )
    db.add(service)
    await db.flush()
    variant = await _variant(db, service, "Standard", BASE, COMMISSION)
    return service, variant


async def _variant(db, service, label, price, commission, *, sort_order=0, active=True):
    variant = ServiceVariant(
        service_id=service.id,
        label=label,
        base_price=Decimal(price),
        provider_payout=Decimal(price) - Decimal(commission),
        platform_commission=Decimal(commission),
        sort_order=sort_order,
        is_active=active,
    )
    db.add(variant)
    await db.flush()
    return variant


async def _sp(db, service, **knobs):
    """An SP that subscribes to ``service`` and would be listed on its sheet
    once priced (``test_service_ranking._sp``), with no price yet."""
    return await _ranked_sp(db, service, priced=False, **knobs)


def _headers(auth_headers, user, role="provider"):
    return auth_headers(str(user.id), role)


async def _patch(client, auth_headers, sp, variant, **body):
    return await client.patch(
        f"{PRICING}/{variant.id}", json=body, headers=_headers(auth_headers, sp)
    )


async def _set(client, auth_headers, sp, variant, **body) -> dict:
    resp = await _patch(client, auth_headers, sp, variant, **body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows(db, sp) -> list[SpServicePricing]:
    return list(
        (
            await db.scalars(
                select(SpServicePricing)
                .where(SpServicePricing.sp_id == sp.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _listed(client, auth_headers, sp, service) -> list[str]:
    resp = await client.get(
        SHEET.format(service.id),
        params={"lat": LAT, "lng": LNG},
        headers=_headers(auth_headers, sp),
    )
    assert resp.status_code == 200, resp.text
    return [p["sp_id"] for p in resp.json()["providers"]]


async def _pricing(client, auth_headers, sp) -> dict:
    resp = await client.get(PRICING, headers=_headers(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _group(pricing, service) -> dict:
    [group] = [g for g in pricing["services"] if g["service_id"] == str(service.id)]
    return group


# --- take_home ---------------------------------------------------------------


def _ac_variant(base=BASE, commission=COMMISSION) -> ServiceVariant:
    return ServiceVariant(base_price=base, platform_commission=commission)


def test_take_home_is_the_ticket_example():
    # 2000 x 750/2499 = 600.24; 18% of 600.24 = 108.0432 -> 108.04.
    assert take_home(_ac_variant(), Decimal("18"), Decimal("2000")) == (
        Decimal("600.24"),
        Decimal("108.04"),
        Decimal("1291.72"),
    )


@pytest.mark.parametrize("price", ["1", "999.99", "1666.00", "2000", "2499", "3123.76", "99999"])
def test_take_home_commission_is_split_at_price(price):
    variant = _ac_variant()
    commission, gst, receives = take_home(variant, Decimal("18"), price)
    assert commission == split_at_price(variant, price)[0]
    assert commission + gst + receives == Decimal(price)


def test_take_home_gst_is_on_the_rounded_commission_half_up():
    # Commission 0.25 exactly (base 100, commission 25, price 1): 18% = 0.045 -> 0.05.
    assert take_home(_ac_variant(Decimal("100"), Decimal("25")), Decimal("18"), "1") == (
        Decimal("0.25"),
        Decimal("0.05"),
        Decimal("0.70"),
    )


def test_take_home_of_a_variant_priced_zero_uses_the_legacy_rate():
    variant = _ac_variant(Decimal("0"), Decimal("0"))
    assert take_home(variant, Decimal("18"), "1000") == (
        Decimal("200.00"),
        Decimal("36.00"),
        Decimal("764.00"),
    )
    assert commission_pct(variant) == Decimal("20.00")


def test_commission_pct_is_unrounded():
    pct = commission_pct(_ac_variant())
    assert Decimal("30.0120") < pct < Decimal("30.0121")
    # Applied client-side, it gives the server's commission, not 2000 x 30.01%.
    assert round(2000 * float(pct) / 100, 2) == 600.24


# --- GET ---------------------------------------------------------------------


async def test_get_lists_each_active_variant_unpriced(client, db_session, auth_headers):
    service, standard = await _service(db_session)
    sp = await _sp(db_session, service)
    big = await _variant(db_session, service, "Split 2 ton", "3000", "900", sort_order=2)
    await _variant(db_session, service, "Retired", "500", "100", sort_order=1, active=False)
    # Not subscribed, so never listed.
    other, _ = await _service(db_session, name="Other")

    pricing = await _pricing(client, auth_headers, sp)

    assert [g["service_id"] for g in pricing["services"]] == [str(service.id)]
    group = _group(pricing, service)
    assert group["service_name"] == "AC gas charging"
    assert group["price_unit"] == "PER_VISIT"
    assert [v["variant_id"] for v in group["variants"]] == [str(standard.id), str(big.id)]
    row = group["variants"][0]
    assert row == {
        "variant_id": str(standard.id),
        "label": "Standard",
        "platform_price": 2499.0,
        "price_unit": "PER_VISIT",
        "recommended_range": {"low": 1874.25, "high": 3123.75},
        "price_amount": None,
        "is_active": None,
        "outside_recommended_range": False,
        "take_home": None,
        "commission_pct": pytest.approx(30.012004801920768),
        "gst_pct": 18.0,
        "upcoming_commission": None,
    }
    assert other  # silence: made only to prove it isn't listed


async def test_get_shows_the_sps_price_and_breakdown(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    await _set(client, auth_headers, sp, variant, price_amount=2000)

    [row] = _group(await _pricing(client, auth_headers, sp), service)["variants"]

    assert row["price_amount"] == 2000.0
    assert row["is_active"] is True
    assert row["outside_recommended_range"] is False
    assert row["take_home"] == {
        "commission": 600.24,
        "commission_gst": 108.04,
        "sp_receives": 1291.72,
    }


async def test_get_skips_an_inactive_service_and_lists_one_with_no_active_variant(
    client, db_session, auth_headers
):
    service, _ = await _service(db_session)
    sp = await _sp(db_session, service)
    off_service, off_variant = await _service(db_session, name="Off")
    await ProviderService(db_session).subscribe_to_service(sp.id, off_service.id)
    off_service.is_active = False
    empty, empty_variant = await _service(db_session, name="Empty")
    await ProviderService(db_session).subscribe_to_service(sp.id, empty.id)
    empty_variant.is_active = False
    await db_session.flush()

    pricing = await _pricing(client, auth_headers, sp)

    ids = [g["service_id"] for g in pricing["services"]]
    assert str(off_service.id) not in ids
    # Ordered by name: "AC gas charging" < "Empty".
    assert ids == [str(service.id), str(empty.id)]
    assert _group(pricing, empty)["variants"] == []


async def test_get_has_no_range_in_a_category_that_isnt_sensitive(client, db_session, auth_headers):
    service, variant = await _service(db_session, sensitive=False)
    sp = await _sp(db_session, service)
    # Far outside what any range would allow: never flagged.
    body = await _set(client, auth_headers, sp, variant, price_amount=99999)

    assert body["recommended_range"] is None
    assert body["outside_recommended_range"] is False
    [row] = _group(await _pricing(client, auth_headers, sp), service)["variants"]
    assert row["recommended_range"] is None
    assert row["outside_recommended_range"] is False


async def test_get_uses_the_categorys_gst_rate(client, db_session, auth_headers):
    service, variant = await _service(db_session, gst_pct="5")
    sp = await _sp(db_session, service)

    body = await _set(client, auth_headers, sp, variant, price_amount=2000)

    assert body["gst_pct"] == 5.0
    # 5% of 600.24 = 30.012 -> 30.01.
    assert body["take_home"] == {
        "commission": 600.24,
        "commission_gst": 30.01,
        "sp_receives": 1369.75,
    }


# --- PATCH: writes -----------------------------------------------------------


async def test_patch_creates_the_row_on_and_returns_it(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    body = await _set(client, auth_headers, sp, variant, price_amount=2000)

    assert body["service_id"] == str(service.id)
    assert body["variant_id"] == str(variant.id)
    assert body["price_amount"] == 2000.0
    assert body["is_active"] is True
    assert body["take_home"]["sp_receives"] == 1291.72
    [row] = await _rows(db_session, sp)
    assert (row.service_id, row.service_variant_id) == (service.id, variant.id)
    assert row.price_amount == Decimal("2000.00")
    assert row.price_unit == "PER_VISIT"
    assert row.is_active is True


async def test_patch_can_create_a_row_switched_off(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    body = await _set(client, auth_headers, sp, variant, price_amount=2000, is_active=False)

    assert body["is_active"] is False
    [row] = await _rows(db_session, sp)
    assert row.is_active is False


async def test_a_second_save_updates_the_same_row(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    await _set(client, auth_headers, sp, variant, price_amount=2000)
    body = await _set(client, auth_headers, sp, variant, price_amount="2100.50")

    assert body["price_amount"] == 2100.5
    [row] = await _rows(db_session, sp)
    assert row.price_amount == Decimal("2100.50")


async def test_a_price_update_keeps_the_row_off(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    await _set(client, auth_headers, sp, variant, price_amount=2000)
    await _set(client, auth_headers, sp, variant, is_active=False)

    body = await _set(client, auth_headers, sp, variant, price_amount=2200)

    assert body["is_active"] is False
    [row] = await _rows(db_session, sp)
    assert (row.price_amount, row.is_active) == (Decimal("2200.00"), False)


async def test_a_price_update_keeps_the_row_on(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    await _set(client, auth_headers, sp, variant, price_amount=2000)

    body = await _set(client, auth_headers, sp, variant, price_amount=2200)

    assert body["is_active"] is True


async def test_on_off_alone_keeps_the_price(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    await _set(client, auth_headers, sp, variant, price_amount=2000)

    off = await _set(client, auth_headers, sp, variant, is_active=False)
    on = await _set(client, auth_headers, sp, variant, is_active=True)

    assert (off["price_amount"], off["is_active"]) == (2000.0, False)
    assert (on["price_amount"], on["is_active"]) == (2000.0, True)
    assert on["take_home"]["sp_receives"] == 1291.72


async def test_use_platform_price_is_a_save_at_base_price(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    body = await _set(client, auth_headers, sp, variant, price_amount=2499)

    assert body["price_amount"] == body["platform_price"] == 2499.0
    assert body["outside_recommended_range"] is False
    # At the variant's own price, the commission is its own ₹750.
    assert body["take_home"]["commission"] == 750.0


async def test_save_logs_sp_price_set(client, db_session, auth_headers, caplog):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    with caplog.at_level(logging.INFO, logger="app.services.sp_pricing_service"):
        await _set(client, auth_headers, sp, variant, price_amount=5000)

    [line] = [r.getMessage() for r in caplog.records if "sp.price_set" in r.getMessage()]
    assert f"sp_id={sp.id}" in line
    assert f"variant_id={variant.id}" in line
    assert "price_amount=5000.00" in line
    assert "is_active=True" in line
    assert "out_of_bounds=True" in line


# --- PATCH: the range --------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "outside"),
    [
        ("1874.24", True),
        (str(LOW), False),
        ("2499", False),
        (str(HIGH), False),
        ("3123.76", True),
    ],
)
async def test_range_edges_are_inclusive(client, db_session, auth_headers, price, outside):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    body = await _set(client, auth_headers, sp, variant, price_amount=price)

    # Saved either way: a guideline, not a hard stop.
    assert body["price_amount"] == float(price)
    assert body["outside_recommended_range"] is outside
    [row] = _group(await _pricing(client, auth_headers, sp), service)["variants"]
    assert row["outside_recommended_range"] is outside


# --- PATCH: refusals ---------------------------------------------------------


@pytest.mark.parametrize("is_active", [True, False])
async def test_on_off_without_a_price_is_price_required(
    client, db_session, auth_headers, is_active
):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    resp = await _patch(client, auth_headers, sp, variant, is_active=is_active)

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "PRICE_REQUIRED"
    assert await _rows(db_session, sp) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"price_amount": None, "is_active": None},
        {"price_amount": 0},
        {"price_amount": -1},
        {"price_amount": "12.345"},
        {"price_amount": 12.345},
        {"price_amount": "10000000000.00"},  # 13 digits: more than Numeric(12, 2)
    ],
)
async def test_bad_bodies_are_422(client, db_session, auth_headers, body):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    resp = await client.patch(
        f"{PRICING}/{variant.id}", json=body, headers=_headers(auth_headers, sp)
    )

    assert resp.status_code == 422, resp.text
    assert await _rows(db_session, sp) == []


async def test_the_largest_price_fits(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    body = await _set(client, auth_headers, sp, variant, price_amount="9999999999.99")

    assert body["price_amount"] == 9999999999.99


async def test_an_unknown_variant_is_404(client, db_session, auth_headers):
    service, _ = await _service(db_session)
    sp = await _sp(db_session, service)

    resp = await client.patch(
        f"{PRICING}/{uuid.uuid4()}", json={"price_amount": 2000}, headers=_headers(auth_headers, sp)
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "VARIANT_NOT_FOUND"


async def test_a_switched_off_variant_is_404(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    variant.is_active = False
    await db_session.flush()

    resp = await _patch(client, auth_headers, sp, variant, price_amount=2000)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "VARIANT_NOT_FOUND"


async def test_a_variant_of_an_inactive_service_is_404(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    service.is_active = False
    await db_session.flush()

    resp = await _patch(client, auth_headers, sp, variant, price_amount=2000)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "VARIANT_NOT_FOUND"


async def test_a_service_the_sp_doesnt_subscribe_to_is_404(client, db_session, auth_headers):
    service, _ = await _service(db_session)
    sp = await _sp(db_session, service)
    _, other_variant = await _service(db_session, name="Not mine")

    resp = await _patch(client, auth_headers, sp, other_variant, price_amount=2000)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "SUBSCRIPTION_NOT_FOUND"
    assert await _rows(db_session, sp) == []


async def test_a_team_pro_is_refused(client, db_session, auth_headers):
    """A team Pro is a consumer account with a Pro row under the SP (SVC-320):
    it can't read or set the SP's prices."""
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    member = await UserFactory.create(db_session, role="consumer")
    db_session.add(
        Professional(service_provider_id=sp.id, user_id=member.id, display_name="Team Pro")
    )
    await db_session.flush()
    headers = _headers(auth_headers, member, role="consumer")

    get = await client.get(PRICING, headers=headers)
    patch = await client.patch(
        f"{PRICING}/{variant.id}", json={"price_amount": 2000}, headers=headers
    )

    assert get.status_code == 403, get.text
    assert patch.status_code == 403, patch.text
    assert await _rows(db_session, sp) == []


async def test_without_a_token_is_401(client, db_session):
    service, variant = await _service(db_session)

    assert (await client.get(PRICING)).status_code == 401
    assert (
        await client.patch(f"{PRICING}/{variant.id}", json={"price_amount": 2000})
    ).status_code == 401
    assert service


# --- the customer's sheet ----------------------------------------------------


async def test_an_unpriced_sp_isnt_listed_until_it_saves_a_price(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    assert str(sp.id) not in await _listed(client, auth_headers, sp, service)

    await _set(client, auth_headers, sp, variant, price_amount=2000)

    assert str(sp.id) in await _listed(client, auth_headers, sp, service)


async def test_switching_off_unlists_and_on_restores_the_same_price(
    client, db_session, auth_headers
):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    await _set(client, auth_headers, sp, variant, price_amount="2150.50")

    await _set(client, auth_headers, sp, variant, is_active=False)
    assert str(sp.id) not in await _listed(client, auth_headers, sp, service)

    await _set(client, auth_headers, sp, variant, is_active=True)
    sheet = await client.get(
        SHEET.format(service.id),
        params={"lat": LAT, "lng": LNG},
        headers=_headers(auth_headers, sp),
    )
    [listed] = [p for p in sheet.json()["providers"] if p["sp_id"] == str(sp.id)]
    assert listed["price_amount"] == 2150.5


async def test_the_sheet_and_the_screen_agree_on_out_of_range(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)

    body = await _set(client, auth_headers, sp, variant, price_amount="3123.76")

    sheet = await client.get(
        SHEET.format(service.id),
        params={"lat": LAT, "lng": LNG},
        headers=_headers(auth_headers, sp),
    )
    [listed] = [p for p in sheet.json()["providers"] if p["sp_id"] == str(sp.id)]
    assert listed["outside_recommended_range"] is body["outside_recommended_range"] is True


# --- unsubscribe -------------------------------------------------------------


async def test_unsubscribe_switches_the_prices_off_and_keeps_them(client, db_session, auth_headers):
    service, standard = await _service(db_session)
    big = await _variant(db_session, service, "Split 2 ton", "3000", "900", sort_order=1)
    sp = await _sp(db_session, service)
    await _set(client, auth_headers, sp, standard, price_amount=2000)
    await _set(client, auth_headers, sp, big, price_amount=3100)
    # Another service's price is left alone.
    other, other_variant = await _service(db_session, name="Other")
    await ProviderService(db_session).subscribe_to_service(sp.id, other.id)
    await _set(client, auth_headers, sp, other_variant, price_amount=2000)

    resp = await client.delete(
        f"/api/v1/providers/me/services/{service.id}", headers=_headers(auth_headers, sp)
    )

    assert resp.status_code == 204, resp.text
    rows = {r.service_variant_id: r for r in await _rows(db_session, sp)}
    assert (rows[standard.id].price_amount, rows[standard.id].is_active) == (
        Decimal("2000.00"),
        False,
    )
    assert (rows[big.id].price_amount, rows[big.id].is_active) == (Decimal("3100.00"), False)
    assert rows[other_variant.id].is_active is True


async def test_resubscribing_leaves_the_prices_off(client, db_session, auth_headers):
    service, variant = await _service(db_session)
    sp = await _sp(db_session, service)
    await _set(client, auth_headers, sp, variant, price_amount=2000)
    headers = _headers(auth_headers, sp)
    await client.delete(f"/api/v1/providers/me/services/{service.id}", headers=headers)

    resp = await client.post(
        "/api/v1/providers/me/services", json={"service_id": str(service.id)}, headers=headers
    )

    assert resp.status_code == 201, resp.text
    [row] = await _rows(db_session, sp)
    assert (row.price_amount, row.is_active) == (Decimal("2000.00"), False)
    assert str(sp.id) not in await _listed(client, auth_headers, sp, service)
    # The screen shows the old price, switched off, ready to switch back on.
    [shown] = _group(await _pricing(client, auth_headers, sp), service)["variants"]
    assert (shown["price_amount"], shown["is_active"]) == (2000.0, False)
    # A price-only save keeps it off; the SP switches it on itself.
    assert (await _set(client, auth_headers, sp, variant, price_amount=2100))["is_active"] is False
    await _set(client, auth_headers, sp, variant, is_active=True)
    assert str(sp.id) in await _listed(client, auth_headers, sp, service)


async def test_unsubscribe_without_prices_still_works(client, db_session, auth_headers):
    service, _ = await _service(db_session)
    sp = await _sp(db_session, service)

    resp = await client.delete(
        f"/api/v1/providers/me/services/{service.id}", headers=_headers(auth_headers, sp)
    )

    assert resp.status_code == 204, resp.text
    count = await db_session.scalar(
        select(func.count()).select_from(SpServicePricing).where(SpServicePricing.sp_id == sp.id)
    )
    assert count == 0


# --- concurrency (committed rows on two connections) -------------------------


def test_a_save_waiting_on_an_unsubscribe_is_404():
    """The subscription is read FOR SHARE: a save that arrives while an
    unsubscribe is in flight waits for it, then finds no subscription. Without
    the lock it would switch the row back on after the unsubscribe."""

    async def main():
        engine = create_async_engine(TEST_DATABASE_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        sp = service = variant = None
        try:
            async with factory() as db:
                service, variant = await _service(db)
                sp = await _sp(db, service)
                await SpPricingService(db).set_price(
                    sp.id, variant.id, price_amount=Decimal("2000"), is_active=None
                )
                await db.commit()

            async with factory() as leaver, factory() as saver:
                await ProviderService(leaver).unsubscribe_from_service(sp.id, service.id)
                save = asyncio.create_task(
                    SpPricingService(saver).set_price(
                        sp.id, variant.id, price_amount=Decimal("2100"), is_active=True
                    )
                )
                await asyncio.sleep(1.0)
                assert not save.done()
                await leaver.commit()
                with pytest.raises(SubscriptionNotFound):
                    await asyncio.wait_for(save, timeout=30)
                await saver.rollback()

            async with factory() as db:
                row = await db.scalar(
                    select(SpServicePricing).where(SpServicePricing.sp_id == sp.id)
                )
                return row.price_amount, row.is_active
        finally:
            if sp is not None:
                async with factory() as db:
                    params = {"u": str(sp.id)}
                    for sql in (
                        # SVC-419: the price save's event points at the SP.
                        "DELETE FROM marketplace_events WHERE sp_id::text = :u",
                        "DELETE FROM sp_service_pricing WHERE sp_id::text = :u",
                        "DELETE FROM provider_services WHERE provider_id::text = :u",
                        "DELETE FROM professionals WHERE service_provider_id::text = :u",
                        "DELETE FROM provider_profiles WHERE user_id::text = :u",
                        "DELETE FROM users WHERE id::text = :u",
                    ):
                        await db.execute(text(sql), params)
                    await db.execute(
                        text("DELETE FROM service_variants WHERE service_id = :s"),
                        {"s": service.id},
                    )
                    await db.execute(text("DELETE FROM services WHERE id = :s"), {"s": service.id})
                    await db.execute(
                        text("DELETE FROM categories WHERE id = :c"),
                        {"c": service.category_id},
                    )
                    await db.commit()
            await engine.dispose()

    assert asyncio.run(main()) == (Decimal("2000.00"), False)
