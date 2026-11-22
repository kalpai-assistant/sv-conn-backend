"""
SVC-415: ``GET /services/from-prices``, the service tiles' "from ₹X" and whether
the customer's area is served at all (Jira 14911 / 14939).

- from_price: the lowest price among SPs the ranking sheet could list right now
  (every dispatch stage, subscribed, an active price on an active variant of an
  active service). In-range prices first, per variant's own range; the lowest
  out-of-range one only when the service has no in-range price on any variant.
- served: an SP covers the point with an active price, online or not.
- One entry per active service; one ranking statement whatever the SP or
  service count; the whole response cached per point for 10 minutes; Redis
  down never fails it.
- variants (SVC-496): every active variant in sort_order, with the same rule
  applied to it alone; NULL when nobody listable prices it.
- Parity: each variant's from_price is what the sheet (``rank_providers``)
  shows for that variant (cheapest in-range SP, else its cheapest), and the
  service's is the lowest in-range one across them, else the lowest of all.

Every test makes its own services, so other tests' committed rows (none of
which price these services) can't change the asserted entries.
"""

import json
import uuid

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, event, select
from sqlalchemy.dialects import postgresql

from app.models.professional import Professional
from app.models.provider_profile import KYCStatus
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service_variant import ServiceVariant
from app.services import provider_matching
from app.services.from_price_service import CACHE_PREFIX, CACHE_TTL_SECONDS, cache_key
from app.services.provider_matching import (
    SERVED_STAGES,
    eligibility_stage_names,
    eligible_provider_conditions,
    listable_only_stages,
    serves_area_conditions,
    stage_conditions,
)
from tests.factories.user_factory import UserFactory
from tests.test_service_ranking import (
    FAR,
    HERE,
    LAT,
    LNG,
    _service,
    _sp,
    _variant,
)

URL = "/api/v1/services/from-prices"
SHEET_URL = "/api/v1/services/{}/providers"


@pytest.fixture(autouse=True)
async def _clean_cache(redis_client):
    """Every test asks at the same point, so start (and leave) with no key."""
    keys = [k async for k in redis_client.scan_iter(f"{CACHE_PREFIX}:*")]
    if keys:
        await redis_client.delete(*keys)
    yield
    keys = [k async for k in redis_client.scan_iter(f"{CACHE_PREFIX}:*")]
    if keys:
        await redis_client.delete(*keys)


@pytest.fixture
async def consumer_headers(db_session, auth_headers):
    consumer = await UserFactory.create(db_session, role="consumer")
    return auth_headers(str(consumer.id), "consumer")


async def _tiles(client, headers, lat=LAT, lng=LNG) -> dict[str, dict]:
    resp = await client.get(URL, params={"lat": lat, "lng": lng}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len({t["service_id"] for t in body}) == len(body)  # one entry per service
    return {t["service_id"]: t for t in body}


async def _tile(client, headers, service) -> dict:
    tile = (await _tiles(client, headers))[str(service.id)]
    return {"from_price": tile["from_price"], "served": tile["served"]}


def _rows(tile) -> list[tuple[str, float | None, bool]]:
    """A tile's ``variants`` as (variant_id, from_price, outside_recommended_range)."""
    return [
        (v["variant_id"], v["from_price"], v["outside_recommended_range"]) for v in tile["variants"]
    ]


async def _variant_prices(client, headers, service) -> list[tuple[str, float | None, bool]]:
    return _rows((await _tiles(client, headers))[str(service.id)])


async def _standard(db, service) -> ServiceVariant:
    return await db.scalar(
        select(ServiceVariant).where(
            ServiceVariant.service_id == service.id, ServiceVariant.label == "Standard"
        )
    )


async def _ac(db):
    """AC gas charging: base ₹2,499, bounds 25% → ₹1,874.25 – ₹3,123.75."""
    return await _service(db, base_price=2499, sensitive=True, bounds_pct=25)


# --- the stage helpers ----------------------------------------------------------


def test_served_and_listable_only_stages_partition_every_stage():
    names = eligibility_stage_names()
    assert set(SERVED_STAGES) <= set(names)
    assert set(SERVED_STAGES).isdisjoint(listable_only_stages())
    assert set(SERVED_STAGES) | set(listable_only_stages()) == set(names)
    assert len(names) == len(set(names))


def test_listable_only_stages_follow_a_new_stage(monkeypatch):
    """A stage added later lands in "listable only" with no edit, and served
    stays the same subset."""
    original = provider_matching._eligibility_stages

    def with_extra(address_location):
        return [*original(address_location), ("brand_new", ProviderServiceModel.id.is_not(None))]

    monkeypatch.setattr(provider_matching, "_eligibility_stages", with_extra)
    assert "brand_new" in listable_only_stages()
    assert "brand_new" not in SERVED_STAGES


def test_stage_conditions_refuses_an_unknown_name():
    with pytest.raises(KeyError):
        stage_conditions(HERE, ["kyc_verified", "no_such_stage"])


def _sql(conditions) -> list[str]:
    return [str(c.compile(dialect=postgresql.dialect())) for c in conditions]


def test_serves_area_is_the_subscription_and_the_served_stages():
    service_id = uuid.uuid4()
    every = dict(
        zip(
            eligibility_stage_names(),
            _sql(stage_conditions(HERE, eligibility_stage_names())),
            strict=True,
        )
    )
    served = _sql(serves_area_conditions(service_id, HERE))
    assert served[0] == _sql(eligible_provider_conditions(service_id, HERE))[0]  # subscription
    assert served[1:] == [every[name] for name in SERVED_STAGES]


def test_cache_key_rounds_to_four_decimals_and_folds_negative_zero():
    assert cache_key(12.97841, 77.64079) == f"{CACHE_PREFIX}:12.9784:77.6408"
    assert cache_key(12.97844, 77.64081) == cache_key(12.97841, 77.64079)
    assert cache_key(-0.00001, 0.00001) == f"{CACHE_PREFIX}:0.0000:0.0000"


# --- the route --------------------------------------------------------------------


async def test_login_required(client):
    resp = await client.get(URL, params={"lat": LAT, "lng": LNG})
    assert resp.status_code == 401


async def test_route_resolves_before_the_service_id_route(client, consumer_headers):
    """Not swallowed by ``/services/{service_id}`` (which would 422 on the UUID)."""
    resp = await client.get(URL, params={"lat": LAT, "lng": LNG}, headers=consumer_headers)
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)
    missing = await client.get(URL, params={"lat": LAT}, headers=consumer_headers)
    assert missing.status_code == 422
    assert "lng" in missing.text and "uuid" not in missing.text.lower()


async def test_coordinates_are_bounded(client, consumer_headers):
    resp = await client.get(URL, params={"lat": 91, "lng": LNG}, headers=consumer_headers)
    assert resp.status_code == 422


async def test_every_active_service_is_listed_and_an_inactive_one_is_not(
    client, db_session, consumer_headers
):
    nobody = await _service(db_session)
    inactive = await _service(db_session, active=False)
    await _sp(db_session, inactive, price=500)
    tiles = await _tiles(client, consumer_headers)
    standard = await _standard(db_session, nobody)
    assert tiles[str(nobody.id)] == {
        "service_id": str(nobody.id),
        "from_price": None,
        "served": False,
        "variants": [
            {"variant_id": str(standard.id), "from_price": None, "outside_recommended_range": False}
        ],
    }
    assert str(inactive.id) not in tiles


async def test_a_service_with_no_active_variant_lists_none(client, db_session, consumer_headers):
    service = await _service(db_session, standard=False)
    await _variant(db_session, service, "Old", 1000, active=False)
    tiles = await _tiles(client, consumer_headers)
    assert tiles[str(service.id)] == {
        "service_id": str(service.id),
        "from_price": None,
        "served": False,
        "variants": [],
    }


# --- from_price -------------------------------------------------------------------


async def test_the_ac_example_takes_the_lowest_in_range_price(client, db_session, consumer_headers):
    service = await _ac(db_session)
    for price in (2200, 2400, 1500):
        await _sp(db_session, service, price=price)
    assert await _tile(client, consumer_headers, service) == {"from_price": 2200.0, "served": True}


async def test_bounds_are_inclusive(client, db_session, consumer_headers):
    service = await _ac(db_session)
    await _sp(db_session, service, price="1874.25")  # exactly the lower bound
    await _sp(db_session, service, price="1874.24")  # a paisa under it
    tile = await _tile(client, consumer_headers, service)
    assert tile == {"from_price": 1874.25, "served": True}


async def test_only_out_of_range_prices_fall_back_to_the_lowest_of_them(
    client, db_session, consumer_headers
):
    service = await _ac(db_session)
    await _sp(db_session, service, price=1500)
    await _sp(db_session, service, price=4000)
    assert await _tile(client, consumer_headers, service) == {"from_price": 1500.0, "served": True}


async def test_an_in_range_price_on_any_variant_beats_a_cheaper_out_of_range_one(
    client, db_session, consumer_headers
):
    """Each variant is judged by its own range; the fallback is for the service,
    not per variant."""
    service = await _ac(db_session)
    small = await _variant(db_session, service, "Split 1T", 1000, sort_order=1)  # 750–1250
    await _sp(db_session, service, price=1000)  # Standard: out of range
    await _sp(db_session, service, price=1200, variant=small)  # in range
    assert await _tile(client, consumer_headers, service) == {"from_price": 1200.0, "served": True}


async def test_lowest_in_range_across_variants(client, db_session, consumer_headers):
    service = await _ac(db_session)
    small = await _variant(db_session, service, "Split 1T", 1000, sort_order=1)
    await _sp(db_session, service, price=2000)
    await _sp(db_session, service, price=900, variant=small)
    assert await _tile(client, consumer_headers, service) == {"from_price": 900.0, "served": True}


async def test_a_non_sensitive_category_counts_everyone(client, db_session, consumer_headers):
    service = await _service(db_session, base_price=1000)
    await _sp(db_session, service, price=100)
    await _sp(db_session, service, price=5000)
    assert await _tile(client, consumer_headers, service) == {"from_price": 100.0, "served": True}


async def test_an_inactive_variant_is_left_out(client, db_session, consumer_headers):
    service = await _service(db_session, base_price=1000)
    off = await _variant(db_session, service, "Old", 1000, sort_order=1, active=False)
    await _sp(db_session, service, price=900)
    await _sp(db_session, service, price=100, variant=off)
    assert await _tile(client, consumer_headers, service) == {"from_price": 900.0, "served": True}


async def test_only_an_inactive_variant_priced_is_not_served(client, db_session, consumer_headers):
    service = await _service(db_session, base_price=1000)
    off = await _variant(db_session, service, "Old", 1000, sort_order=1, active=False)
    await _sp(db_session, service, price=100, variant=off)
    assert await _tile(client, consumer_headers, service) == {"from_price": None, "served": False}


async def test_an_inactive_price_row_is_left_out(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _sp(db_session, service, price=900)
    await _sp(db_session, service, price=100, price_active=False)
    alone = await _service(db_session)
    await _sp(db_session, alone, price=100, price_active=False)
    tiles = await _tiles(client, consumer_headers)  # one call: the second would be cached
    assert (tiles[str(service.id)]["from_price"], tiles[str(service.id)]["served"]) == (900.0, True)
    assert (tiles[str(alone.id)]["from_price"], tiles[str(alone.id)]["served"]) == (None, False)


async def test_an_unsubscribed_sp_is_left_out(client, db_session, consumer_headers):
    service = await _service(db_session)
    sp = await _sp(db_session, service, price=100)
    await db_session.execute(
        delete(ProviderServiceModel).where(ProviderServiceModel.provider_id == sp.id)
    )
    await db_session.flush()
    assert await _tile(client, consumer_headers, service) == {"from_price": None, "served": False}


async def test_an_unpriced_sp_is_left_out(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _sp(db_session, service, priced=False)
    assert await _tile(client, consumer_headers, service) == {"from_price": None, "served": False}


# --- served without a listable SP ---------------------------------------------------


@pytest.mark.parametrize(
    "knobs", [{"online": False}, {"has_token": False}], ids=["offline", "no_push_token"]
)
async def test_unlistable_now_but_served(client, db_session, consumer_headers, knobs):
    service = await _service(db_session)
    await _sp(db_session, service, price=900, **knobs)
    assert await _tile(client, consumer_headers, service) == {"from_price": None, "served": True}


async def test_nobody_to_do_it_is_served_but_not_listable(client, db_session, consumer_headers):
    service = await _service(db_session)
    sp = await _sp(db_session, service, price=900)
    self_pro = await db_session.scalar(
        select(Professional).where(
            Professional.service_provider_id == sp.id, Professional.user_id == sp.id
        )
    )
    self_pro.accepts_direct_jobs = False  # switched off, no team Pro
    await db_session.flush()
    assert await _tile(client, consumer_headers, service) == {"from_price": None, "served": True}


async def test_an_offline_cheaper_sp_doesnt_set_the_price(client, db_session, consumer_headers):
    service = await _service(db_session)
    await _sp(db_session, service, price=500, online=False)
    await _sp(db_session, service, price=900)
    assert await _tile(client, consumer_headers, service) == {"from_price": 900.0, "served": True}


@pytest.mark.parametrize(
    "knobs",
    [
        {"point": FAR},
        {"kyc": KYCStatus.SUBMITTED},
        {"kyc": KYCStatus.PENDING},
        {"account_active": False},
    ],
    ids=["outside_area", "kyc_submitted", "kyc_pending", "inactive_account"],
)
async def test_not_served(client, db_session, consumer_headers, knobs):
    service = await _service(db_session)
    await _sp(db_session, service, price=900, **knobs)
    assert await _tile(client, consumer_headers, service) == {"from_price": None, "served": False}


async def test_several_sps_and_services_in_one_call(client, db_session, consumer_headers):
    a = await _service(db_session)
    b = await _service(db_session)
    c = await _service(db_session)
    d = await _ac(db_session)
    await _sp(db_session, a, price=1200)
    await _sp(db_session, a, price=900)
    await _sp(db_session, b, price=700, online=False)
    await _sp(db_session, b, price=800)
    await _sp(db_session, d, price=1500)
    await _sp(db_session, d, price=3000)
    tiles = await _tiles(client, consumer_headers)
    got = {s: (tiles[str(s.id)]["from_price"], tiles[str(s.id)]["served"]) for s in (a, b, c, d)}
    assert got == {
        a: (900.0, True),
        b: (800.0, True),
        c: (None, False),
        d: (3000.0, True),
    }


# --- per-variant from_price (SVC-496) ------------------------------------------------


async def test_each_variant_takes_its_own_lowest_in_range_price(
    client, db_session, consumer_headers
):
    service = await _ac(db_session)  # Standard 2,499: 1,874.25 - 3,123.75
    standard = await _standard(db_session, service)
    small = await _variant(db_session, service, "Split 1T", 1000, sort_order=1)  # 750-1250
    for price in (2200, 2400, 1500):
        await _sp(db_session, service, price=price)
    for price in (1100, 900, 600):
        await _sp(db_session, service, price=price, variant=small)
    assert await _variant_prices(client, consumer_headers, service) == [
        (str(standard.id), 2200.0, False),
        (str(small.id), 900.0, False),
    ]


async def test_a_variant_with_nothing_in_range_shows_its_lowest_while_the_service_doesnt(
    client, db_session, consumer_headers
):
    """The service takes the in-range price on another variant; the variant with
    only out-of-range SPs still shows its own lowest, even though it's cheaper."""
    service = await _ac(db_session)
    standard = await _standard(db_session, service)
    small = await _variant(db_session, service, "Split 1T", 1000, sort_order=1)  # 750-1250
    await _sp(db_session, service, price=1500)  # Standard: out of range
    await _sp(db_session, service, price=1000)  # Standard: out of range, lower
    await _sp(db_session, service, price=1200, variant=small)  # in range
    tile = (await _tiles(client, consumer_headers))[str(service.id)]
    assert tile["from_price"] == 1200.0
    assert _rows(tile) == [
        (str(standard.id), 1000.0, True),
        (str(small.id), 1200.0, False),
    ]


async def test_an_active_variant_nobody_prices_is_listed_with_null(
    client, db_session, consumer_headers
):
    service = await _service(db_session, base_price=1000)
    standard = await _standard(db_session, service)
    big = await _variant(db_session, service, "Large", 2000, sort_order=1)
    await _sp(db_session, service, price=900)
    tile = (await _tiles(client, consumer_headers))[str(service.id)]
    assert (tile["from_price"], tile["served"]) == (900.0, True)
    assert _rows(tile) == [
        (str(standard.id), 900.0, False),
        (str(big.id), None, False),
    ]


async def test_an_inactive_variant_is_not_listed(client, db_session, consumer_headers):
    service = await _service(db_session, base_price=1000)
    standard = await _standard(db_session, service)
    off = await _variant(db_session, service, "Old", 1000, sort_order=1, active=False)
    await _sp(db_session, service, price=900)
    await _sp(db_session, service, price=100, variant=off)
    assert await _variant_prices(client, consumer_headers, service) == [
        (str(standard.id), 900.0, False)
    ]


async def test_variants_come_in_sort_order(client, db_session, consumer_headers):
    service = await _service(db_session, base_price=1000, standard=False)
    third = await _variant(db_session, service, "C", 1000, sort_order=3)
    first = await _variant(db_session, service, "A", 1000, sort_order=1)
    second = await _variant(db_session, service, "B", 1000, sort_order=2)
    await _sp(db_session, service, price=500, variant=second)
    assert await _variant_prices(client, consumer_headers, service) == [
        (str(first.id), None, False),
        (str(second.id), 500.0, False),
        (str(third.id), None, False),
    ]


@pytest.mark.parametrize(
    "knobs", [{"online": False}, {"has_token": False}], ids=["offline", "no_push_token"]
)
async def test_an_unlistable_sp_sets_no_variant_price_but_serves(
    client, db_session, consumer_headers, knobs
):
    service = await _service(db_session, base_price=1000)
    standard = await _standard(db_session, service)
    small = await _variant(db_session, service, "Small", 600, sort_order=1)
    await _sp(db_session, service, price=400, variant=small, **knobs)  # cheaper, unlistable
    await _sp(db_session, service, price=700, variant=small)
    await _sp(db_session, service, price=800, **knobs)  # Standard's only SP
    tile = (await _tiles(client, consumer_headers))[str(service.id)]
    assert (tile["from_price"], tile["served"]) == (700.0, True)
    assert _rows(tile) == [
        (str(standard.id), None, False),
        (str(small.id), 700.0, False),
    ]


async def test_only_unlistable_sps_leave_every_variant_null_and_served(
    client, db_session, consumer_headers
):
    service = await _service(db_session, base_price=1000)
    standard = await _standard(db_session, service)
    await _sp(db_session, service, price=800, online=False)
    tile = (await _tiles(client, consumer_headers))[str(service.id)]
    assert tile == {
        "service_id": str(service.id),
        "from_price": None,
        "served": True,
        "variants": [
            {"variant_id": str(standard.id), "from_price": None, "outside_recommended_range": False}
        ],
    }


# --- parity with the sheet ----------------------------------------------------------


async def _active_variants(db, service) -> list[ServiceVariant]:
    return list(
        (
            await db.scalars(
                select(ServiceVariant).where(
                    ServiceVariant.service_id == service.id, ServiceVariant.is_active.is_(True)
                )
            )
        ).all()
    )


async def _sheet(client, db, headers, service) -> dict[str, tuple]:
    """Per active variant, read off ``rank_providers`` at the same point:
    (lowest in-range price, lowest price), each None when the sheet has none."""
    out = {}
    for variant in await _active_variants(db, service):
        resp = await client.get(
            SHEET_URL.format(service.id),
            params={"lat": LAT, "lng": LNG, "variant_id": str(variant.id), "limit": 50},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        listed = resp.json()["providers"]
        inside = [p["price_amount"] for p in listed if not p["outside_recommended_range"]]
        out[str(variant.id)] = (
            min(inside, default=None),
            min((p["price_amount"] for p in listed), default=None),
        )
    return out


def _rule(in_range, lowest) -> tuple[float | None, bool]:
    """(the row's from_price, whether it is the out-of-range fallback)."""
    if in_range is not None:
        return in_range, False
    return lowest, lowest is not None


async def test_parity_with_the_ranking_sheet(client, db_session, consumer_headers):
    ac = await _ac(db_session)
    big = await _variant(db_session, ac, "Tower", 4000, sort_order=1)  # 3000-5000
    await _sp(db_session, ac, price=2200)
    await _sp(db_session, ac, price=1500)  # out of range
    await _sp(db_session, ac, price=1900, online=False)  # not listable
    await _sp(db_session, ac, price=3100, variant=big)
    await _sp(db_session, ac, price=2900, variant=big)  # out of the Tower range

    only_out = await _service(db_session, base_price=1000, sensitive=True, bounds_pct=10)
    await _sp(db_session, only_out, price=500)
    await _sp(db_session, only_out, price=2000)

    # One variant in range, a cheaper one only out of range, one nobody lists.
    mixed = await _service(db_session, base_price=1000, sensitive=True, bounds_pct=10)  # 900-1100
    mini = await _variant(db_session, mixed, "Mini", 600, sort_order=1)  # 540-660
    unlisted = await _variant(db_session, mixed, "Max", 3000, sort_order=2)
    await _sp(db_session, mixed, price=1050)
    await _sp(db_session, mixed, price=400, variant=mini)  # out of the Mini range
    await _sp(db_session, mixed, price=700, variant=mini)  # out of the Mini range
    await _sp(db_session, mixed, price=100, variant=unlisted, online=False)

    plain = await _service(db_session, base_price=800)
    off = await _variant(db_session, plain, "Old", 800, sort_order=1, active=False)
    await _sp(db_session, plain, price=750)
    await _sp(db_session, plain, price=600, point=HERE)
    await _sp(db_session, plain, price=100, variant=off)
    await _sp(db_session, plain, price=50, has_token=False)

    empty = await _service(db_session)
    served_only = await _service(db_session)
    await _sp(db_session, served_only, price=400, online=False)

    tiles = await _tiles(client, consumer_headers)
    expected = {
        ac: 2200.0,
        only_out: 500.0,
        mixed: 1050.0,
        plain: 600.0,
        empty: None,
        served_only: None,
    }
    for service, price in expected.items():
        sheet = await _sheet(client, db_session, consumer_headers, service)
        tile = tiles[str(service.id)]
        # Each row = the sheet's lowest in-range price for that variant, else its lowest.
        rows = {vid: (price, outside) for vid, price, outside in _rows(tile)}
        assert rows == {vid: _rule(*pair) for vid, pair in sheet.items()}, service.name
        # The service = the lowest in-range across variants, else the lowest of all.
        in_range = [i for i, _ in sheet.values() if i is not None]
        lowest = [low for _, low in sheet.values() if low is not None]
        want = min(in_range) if in_range else min(lowest, default=None)
        assert tile["from_price"] == want == price, service.name

    # The mixed service shows the rule, not min(rows).
    assert [(price, outside) for _, price, outside in _rows(tiles[str(mixed.id)])] == [
        (1050.0, False),
        (400.0, True),
        (None, False),
    ]
    assert tiles[str(mixed.id)]["from_price"] == 1050.0


# --- the cache -------------------------------------------------------------------------


def _count_pricing_statements(engine):
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", record)


async def test_a_cache_hit_runs_no_pricing_sql(client, db_session, consumer_headers, redis_client):
    service = await _service(db_session)
    await _sp(db_session, service, price=900)
    statements, stop = _count_pricing_statements(db_session.bind.sync_engine)
    try:
        first = await _tiles(client, consumer_headers)
        computed = sum("sp_service_pricing" in s for s in statements)
        statements.clear()
        # A cheaper SP after the first answer: the cached one still stands.
        await _sp(db_session, service, price=100)
        statements.clear()
        second = await _tiles(client, consumer_headers)
        cached = sum("sp_service_pricing" in s for s in statements)
    finally:
        stop()
    assert (computed, cached) == (1, 0)
    assert second == first
    assert second[str(service.id)]["from_price"] == 900.0

    key = cache_key(LAT, LNG)
    assert 0 < await redis_client.ttl(key) <= CACHE_TTL_SECONDS


async def test_a_nearby_point_shares_the_cache_and_a_far_one_doesnt(
    client, db_session, consumer_headers, redis_client
):
    service = await _service(db_session)
    await _sp(db_session, service, price=900)
    await _tiles(client, consumer_headers)
    await _tiles(client, consumer_headers, lat=LAT + 0.00001)  # same 4-decimal key
    await _tiles(client, consumer_headers, lat=LAT + 0.01)
    keys = {k async for k in redis_client.scan_iter(f"{CACHE_PREFIX}:*")}
    assert keys == {cache_key(LAT, LNG), cache_key(LAT + 0.01, LNG)}


async def test_redis_down_still_answers_uncached(
    client, db_session, consumer_headers, redis_client, monkeypatch, caplog
):
    service = await _service(db_session)
    await _sp(db_session, service, price=900)

    async def _down(*args, **kwargs):
        raise RedisConnectionError("redis is down")

    monkeypatch.setattr(redis_client, "get", _down)
    monkeypatch.setattr(redis_client, "set", _down)
    with caplog.at_level("WARNING", logger="app.services.from_price_service"):
        tile = await _tile(client, consumer_headers, service)
    assert tile == {"from_price": 900.0, "served": True}
    messages = [r.getMessage() for r in caplog.records]
    assert any("from_prices.cache_read_failed" in m for m in messages)
    assert any("from_prices.cache_write_failed" in m for m in messages)


async def test_a_corrupt_cache_entry_is_recomputed(
    client, db_session, consumer_headers, redis_client
):
    service = await _service(db_session)
    await _sp(db_session, service, price=900)
    await redis_client.set(cache_key(LAT, LNG), "not json", ex=60)
    assert await _tile(client, consumer_headers, service) == {"from_price": 900.0, "served": True}


async def test_a_cache_entry_from_before_variants_is_recomputed(
    client, db_session, consumer_headers, redis_client
):
    """An entry written before SVC-496 has no ``variants``. It fails validation,
    is recomputed and overwritten, never returned as is or turned into a 500."""
    service = await _service(db_session)
    standard = await _standard(db_session, service)
    await _sp(db_session, service, price=900)
    stale = [{"service_id": str(service.id), "from_price": 1.0, "served": True}]
    await redis_client.set(cache_key(LAT, LNG), json.dumps(stale), ex=60)
    tile = (await _tiles(client, consumer_headers))[str(service.id)]
    assert tile["from_price"] == 900.0
    assert tile["variants"] == [
        {"variant_id": str(standard.id), "from_price": 900.0, "outside_recommended_range": False}
    ]
    rewritten = json.loads(await redis_client.get(cache_key(LAT, LNG)))
    assert all("variants" in item for item in rewritten)


# --- performance -----------------------------------------------------------------------


async def test_statement_count_is_the_same_for_1_and_100_sps_and_many_services(
    client, db_session, consumer_headers, redis_client
):
    first = await _service(db_session)
    await _sp(db_session, first, price=900)

    statements, stop = _count_pricing_statements(db_session.bind.sync_engine)

    async def run() -> dict[str, int]:
        await redis_client.delete(cache_key(LAT, LNG))
        statements.clear()
        await _tiles(client, consumer_headers)
        return {
            "total": len(statements),
            "pricing": sum("sp_service_pricing" in s for s in statements),
        }

    try:
        one = await run()
        for i in range(99):
            await _sp(db_session, first, price=900 + i)
        hundred_sps = await run()
        for i in range(20):
            sensitive = i % 2 == 0
            service = await _service(
                db_session, sensitive=sensitive, bounds_pct=20 if sensitive else None
            )
            small = await _variant(db_session, service, "Small", 600, sort_order=1)
            await _sp(db_session, service, price=950 + i)
            await _sp(db_session, service, price=650, variant=small, online=i % 3 != 0)
        many_services = await run()
    finally:
        stop()

    assert one["pricing"] == 1
    assert one == hundred_sps == many_services
