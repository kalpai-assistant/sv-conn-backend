"""
SVC-413: the SP's market insights per variant,
``GET /providers/me/market-insights?service_variant_id=``, and the folded-in fix
that SPs who leave stop keeping active prices.

- Who counts: KYC VERIFIED, an active account, a service-area centre and an
  active price for the variant; online or not. Areas overlap when the centres
  are within the two radii added together. The asking SP counts only while its
  own price is on.
- Below 3 SPs, or without a centre: ``insufficient_competition``, no stats.
- Maths (14926 #2): median to paise, percentile half up, thirds at 33 / 67.
- Cache (14933): only the competitors' prices, an hour, no ids, never cleared;
  the SP's own price is read fresh. A Redis error never fails the request.
- Account deletion and admin deactivation switch the SP's prices off.
"""

import json
import logging
import uuid
from decimal import Decimal

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select, update

from app.models.professional import Professional
from app.models.provider_profile import KYCStatus
from app.models.sp_service_pricing import SpServicePricing
from app.services.account_service import AccountService
from app.services.market_insights_service import (
    MarketInsightsService,
    area_stats,
    bucket,
    cache_key,
    median,
    percentile,
)
from app.services.team_service import TeamService
from tests.factories.user_factory import UserFactory
from tests.test_service_ranking import FAR, HERE, _first_variant, _service, _sp, _variant

URL = "/api/v1/providers/me/market-insights"
PRICING = "/api/v1/providers/me/pricing"

# 1 degree of latitude is ~111.19 km.
KM_PER_DEG_LAT = 111.19


def _north(km: float) -> str:
    """A point ``km`` due north of ``HERE``."""
    return f"SRID=4326;POINT(77.6408 {12.9784 + km / KM_PER_DEG_LAT})"


def _d(values) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


@pytest.fixture(autouse=True)
async def _clean_cache(redis_client):
    yield
    keys = [k async for k in redis_client.scan_iter("market_insights:*")]
    if keys:
        await redis_client.delete(*keys)


async def _me(db, service, price=450, **knobs):
    """The asking SP, at ``HERE`` with a 5 km radius unless a knob says otherwise."""
    knobs.setdefault("point", HERE)
    knobs.setdefault("radius_km", 5)
    return await _sp(db, service, price=price, **knobs)


async def _get(client, auth_headers, sp, variant_id, role="provider"):
    return await client.get(
        URL,
        params={"service_variant_id": str(variant_id)},
        headers=auth_headers(str(sp.id), role),
    )


async def _insights(client, auth_headers, sp, variant) -> dict:
    resp = await _get(client, auth_headers, sp, variant.id)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _set_price(db, sp, variant, price) -> None:
    await db.execute(
        update(SpServicePricing)
        .where(SpServicePricing.sp_id == sp.id, SpServicePricing.service_variant_id == variant.id)
        .values(price_amount=Decimal(price))
    )
    await db.flush()


# --- the maths ----------------------------------------------------------------


def test_percentile_of_the_cheapest_is_0_and_the_dearest_100():
    assert percentile(_d([200, 300, 400]), Decimal("100")) == 0
    assert percentile(_d([100, 200, 300]), Decimal("400")) == 100


def test_percentile_of_the_exact_median_of_an_odd_count_is_50():
    assert percentile(_d([100, 300]), Decimal("200")) == 50
    assert percentile(_d([100, 200, 400, 500]), Decimal("300")) == 50


def test_percentile_with_all_prices_equal_is_50():
    assert percentile(_d([450, 450, 450, 450]), Decimal("450")) == 50


def test_percentile_counts_ties_as_half():
    # 1 below + 2 at my price / 2 = 2, of 4 others.
    assert percentile(_d([100, 450, 450, 900]), Decimal("450")) == 50
    # 1 below + 1 tie / 2 = 1.5, of 3 others: 50.
    assert percentile(_d([100, 450, 900]), Decimal("450")) == 50


def test_percentile_rounds_half_up_not_to_even():
    # 1 of 8 below: 12.5 -> 13 (banker's round() gives 12).
    assert percentile(_d([100, *[900] * 7]), Decimal("500")) == 13
    # 5 of 8 below: 62.5 -> 63 (banker's round() gives 62).
    assert percentile(_d([*[100] * 5, *[900] * 3]), Decimal("500")) == 63


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "lower"),
        (33, "lower"),
        (34, "middle"),
        (50, "middle"),
        (66, "middle"),
        (67, "upper"),
        (100, "upper"),
    ],
)
def test_bucket_edges(value, expected):
    assert bucket(value) == expected


def test_median_of_an_odd_count_is_the_middle_price():
    assert median(_d([100, 250.5, 900])) == Decimal("250.50")


def test_median_of_an_even_count_is_the_average_of_the_two_middles_to_paise():
    # (200.01 + 300.02) / 2 = 250.015 -> 250.02, half up.
    assert median(_d([100, 200.01, 300.02, 400])) == Decimal("250.02")
    assert median(_d([100, 200, 300, 400])) == Decimal("250.00")


def test_area_stats_include_my_price_when_on():
    stats = area_stats(_d([350, 475, 500, 650]), Decimal("450"))

    assert stats.model_dump() == {
        "sp_count": 5,
        "median_price": 475.0,
        "min_price": 350.0,
        "max_price": 650.0,
        "my_percentile": 25,
        "my_bucket": "lower",
    }


def test_area_stats_without_my_price_has_no_position():
    stats = area_stats(_d([350, 475, 500]), None)

    assert (stats.sp_count, stats.median_price) == (3, 475.0)
    assert (stats.my_percentile, stats.my_bucket) == (None, None)


def test_area_stats_below_3_sps_is_none():
    assert area_stats(_d([500]), Decimal("450")) is None
    assert area_stats(_d([500, 600]), None) is None


# --- the endpoint ---------------------------------------------------------------


async def test_the_response_shape(client, db_session, auth_headers):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    me = await _me(db_session, service, price="450.00")
    for price in ("350.00", "475.00", "500.00", "650.00"):
        await _sp(db_session, service, price=price)

    body = await _insights(client, auth_headers, me, variant)

    assert body == {
        "service_variant_id": str(variant.id),
        "service_id": str(service.id),
        "service_name": "Tap repair",
        "my_price": 450.0,
        "reference_price": 1000.0,
        "insufficient_competition": False,
        "area_stats": {
            "sp_count": 5,
            "median_price": 475.0,
            "min_price": 350.0,
            "max_price": 650.0,
            "my_percentile": 25,
            "my_bucket": "lower",
        },
    }


async def test_who_counts(client, db_session, auth_headers):
    """Only the overlapping, offline and in-area SPs count; each excluded one has
    an extreme price, so min / max would show it."""
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    other_variant = await _variant(db_session, service, "Large", 2000, sort_order=1)
    me = await _me(db_session, service, price=500)
    # 10 km away: 5 + 6 = 11 overlaps (neither radius alone reaches the other centre).
    await _sp(db_session, service, price=400, point=_north(10), radius_km=6)
    await _sp(db_session, service, price=600, online=False)
    # 10 km away with 5 + 4 = 9: the areas don't touch.
    await _sp(db_session, service, price=1, point=_north(10), radius_km=4)
    await _sp(db_session, service, price=2, point=FAR)
    await _sp(db_session, service, price=3, account_active=False)
    for kyc in (KYCStatus.PENDING, KYCStatus.SUBMITTED, KYCStatus.REJECTED):
        await _sp(db_session, service, price=4, kyc=kyc)
    await _sp(db_session, service, price=5, price_active=False)
    await _sp(db_session, service, price=6, variant=other_variant)
    await _sp(db_session, service, price=7, point=None)

    stats = (await _insights(client, auth_headers, me, variant))["area_stats"]

    assert (stats["sp_count"], stats["min_price"], stats["max_price"]) == (3, 400.0, 600.0)
    assert (stats["my_percentile"], stats["my_bucket"]) == (50, "middle")


async def test_two_sps_is_insufficient_competition(client, db_session, auth_headers):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    me = await _me(db_session, service)
    await _sp(db_session, service, price=600)

    body = await _insights(client, auth_headers, me, variant)

    assert body["insufficient_competition"] is True
    assert body["area_stats"] is None
    assert body["my_price"] == 450.0


@pytest.mark.parametrize("own", ["off", "unpriced"])
async def test_own_price_off_or_unset_still_shows_the_area(client, db_session, auth_headers, own):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    if own == "off":
        me = await _me(db_session, service, price_active=False)
    else:
        me = await _me(db_session, service, priced=False)
    for price in (400, 500, 600):
        await _sp(db_session, service, price=price)

    body = await _insights(client, auth_headers, me, variant)

    assert body["my_price"] is None
    assert body["insufficient_competition"] is False
    assert body["area_stats"] == {
        "sp_count": 3,
        "median_price": 500.0,
        "min_price": 400.0,
        "max_price": 600.0,
        "my_percentile": None,
        "my_bucket": None,
    }


async def test_own_price_off_with_two_others_is_insufficient(client, db_session, auth_headers):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    me = await _me(db_session, service, price_active=False)
    for price in (400, 600):
        await _sp(db_session, service, price=price)

    body = await _insights(client, auth_headers, me, variant)

    assert (body["insufficient_competition"], body["area_stats"]) == (True, None)


async def test_no_centre_is_insufficient_and_skips_the_query(
    client, db_session, auth_headers, redis_client, monkeypatch
):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    me = await _me(db_session, service, point=None)
    for price in (400, 500, 600):
        await _sp(db_session, service, price=price)

    async def _no_query(*args, **kwargs):
        raise AssertionError("the competitor query ran")

    monkeypatch.setattr(MarketInsightsService, "_competitors", _no_query)

    body = await _insights(client, auth_headers, me, variant)

    assert (body["insufficient_competition"], body["area_stats"]) == (True, None)
    assert body["my_price"] == 450.0
    assert await redis_client.exists(cache_key(me.id, variant.id)) == 0


# --- errors ---------------------------------------------------------------------


async def test_unknown_variant_is_404(client, db_session, auth_headers):
    service = await _service(db_session)
    me = await _me(db_session, service)

    resp = await _get(client, auth_headers, me, uuid.uuid4())

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "VARIANT_NOT_FOUND"


async def test_inactive_variant_or_service_is_404(client, db_session, auth_headers):
    service = await _service(db_session)
    off = await _variant(db_session, service, "Off", 900, sort_order=1, active=False)
    me = await _me(db_session, service)
    off_service = await _service(db_session, active=False)
    off_service_variant = await _first_variant(db_session, off_service)

    for variant_id in (off.id, off_service_variant.id):
        resp = await _get(client, auth_headers, me, variant_id)
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "VARIANT_NOT_FOUND"


async def test_an_unsubscribed_service_is_404(client, db_session, auth_headers):
    service = await _service(db_session)
    me = await _me(db_session, service)
    other = await _service(db_session)
    other_variant = await _first_variant(db_session, other)

    resp = await _get(client, auth_headers, me, other_variant.id)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "SUBSCRIPTION_NOT_FOUND"


async def test_a_team_pro_or_consumer_is_403(client, db_session, auth_headers):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    sp = await _me(db_session, service)
    member = await UserFactory.create(db_session, role="consumer")
    db_session.add(
        Professional(service_provider_id=sp.id, user_id=member.id, display_name="Team Pro")
    )
    consumer = await UserFactory.create(db_session, role="consumer")
    await db_session.flush()

    for user in (member, consumer):
        resp = await _get(client, auth_headers, user, variant.id, role="consumer")
        assert resp.status_code == 403, resp.text


async def test_without_a_token_is_401(client, db_session):
    resp = await client.get(URL, params={"service_variant_id": str(uuid.uuid4())})

    assert resp.status_code == 401


# --- the cache ------------------------------------------------------------------


async def test_competitors_are_cached_for_an_hour_as_prices_only(
    client, db_session, auth_headers, redis_client
):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    me = await _me(db_session, service)
    rivals = [await _sp(db_session, service, price=p) for p in ("400.00", "500.50", "600.00")]

    first = await _insights(client, auth_headers, me, variant)
    raw = await redis_client.get(cache_key(me.id, variant.id))
    ttl = await redis_client.ttl(cache_key(me.id, variant.id))
    await _set_price(db_session, rivals[0], variant, "100.00")
    second = await _insights(client, auth_headers, me, variant)

    # Prices only, in order, exact: no SP ids.
    assert json.loads(raw) == ["400.00", "500.50", "600.00"]
    for user in (me, *rivals):
        assert str(user.id) not in raw
    assert 3590 <= ttl <= 3600
    # The competitor's change doesn't show while the key is there ...
    assert second == first
    assert first["area_stats"]["min_price"] == 400.0

    # ... and does once it's gone.
    await redis_client.delete(cache_key(me.id, variant.id))
    third = await _insights(client, auth_headers, me, variant)
    assert third["area_stats"]["min_price"] == 100.0


async def test_my_own_price_change_shows_with_the_cache_warm(
    client, db_session, auth_headers, redis_client
):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    me = await _me(db_session, service, price=450)
    for price in (400, 500, 600):
        await _sp(db_session, service, price=price)

    before = await _insights(client, auth_headers, me, variant)
    cached = await redis_client.get(cache_key(me.id, variant.id))
    patch = await client.patch(
        f"{PRICING}/{variant.id}",
        json={"price_amount": 650},
        headers=auth_headers(str(me.id), "provider"),
    )
    assert patch.status_code == 200, patch.text
    after = await _insights(client, auth_headers, me, variant)

    # 1 of 3 below: 33, the top of the lower third.
    assert (before["my_price"], before["area_stats"]["my_percentile"]) == (450.0, 33)
    assert before["area_stats"]["my_bucket"] == "lower"
    assert after["my_price"] == 650.0
    assert after["area_stats"]["max_price"] == 650.0
    assert (after["area_stats"]["my_percentile"], after["area_stats"]["my_bucket"]) == (
        100,
        "upper",
    )
    # Nothing cleared the key; it's the same competitor list.
    assert await redis_client.get(cache_key(me.id, variant.id)) == cached

    off = await client.patch(
        f"{PRICING}/{variant.id}",
        json={"is_active": False},
        headers=auth_headers(str(me.id), "provider"),
    )
    assert off.status_code == 200, off.text
    switched_off = await _insights(client, auth_headers, me, variant)
    assert switched_off["my_price"] is None
    assert switched_off["area_stats"]["sp_count"] == 3


async def test_redis_down_still_answers_uncached(
    client, db_session, auth_headers, redis_client, monkeypatch, caplog
):
    service = await _service(db_session)
    variant = await _first_variant(db_session, service)
    me = await _me(db_session, service)
    for price in (400, 500, 600):
        await _sp(db_session, service, price=price)

    async def _down(*args, **kwargs):
        raise RedisConnectionError("redis is down")

    monkeypatch.setattr(redis_client, "get", _down)
    monkeypatch.setattr(redis_client, "set", _down)

    with caplog.at_level(logging.WARNING, logger="app.services.market_insights_service"):
        body = await _insights(client, auth_headers, me, variant)

    assert body["area_stats"]["sp_count"] == 4
    assert body["area_stats"]["median_price"] == 475.0
    messages = [r.getMessage() for r in caplog.records]
    assert any("market_insights.cache_read_failed" in m for m in messages)
    assert any("market_insights.cache_write_failed" in m for m in messages)


# --- SPs who leave stop keeping active prices -------------------------------------


async def _active_rows(db, sp) -> list[bool]:
    return list(
        (
            await db.scalars(
                select(SpServicePricing.is_active)
                .where(SpServicePricing.sp_id == sp.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _sp_with_two_prices(db, service, second_variant):
    sp = await _sp(db, service)
    db.add(
        SpServicePricing(
            sp_id=sp.id,
            service_id=service.id,
            service_variant_id=second_variant.id,
            price_amount=Decimal("2100"),
            price_unit="PER_VISIT",
            is_active=True,
        )
    )
    await db.flush()
    return sp


@pytest.mark.parametrize("leave", ["delete_account", "deactivate_user"])
async def test_an_sp_who_leaves_has_every_price_switched_off(db_session, leave):
    service = await _service(db_session)
    large = await _variant(db_session, service, "Large", 2000, sort_order=1)
    leaver = await _sp_with_two_prices(db_session, service, large)
    stayer = await _sp_with_two_prices(db_session, service, large)

    if leave == "delete_account":
        await AccountService(db_session).delete_account(leaver.id)
    else:
        await TeamService(db_session).deactivate_user(leaver.id)

    assert await _active_rows(db_session, leaver) == [False, False]
    assert await _active_rows(db_session, stayer) == [True, True]
    # The amounts stay, as on an unsubscribe.
    amounts = await db_session.scalars(
        select(SpServicePricing.price_amount).where(SpServicePricing.sp_id == leaver.id)
    )
    assert sorted(amounts.all()) == [Decimal("1000.00"), Decimal("2100.00")]
