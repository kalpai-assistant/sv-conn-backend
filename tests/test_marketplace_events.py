"""
SVC-419: marketplace analytics events in ``marketplace_events``.

- ``POST /analytics/events``: the five client events, one per request, login
  required, ``user_id`` from the token. Only those names; extra fields, bad
  bounds and bad enums are a 422. A repeated ``event_id`` is a 204 that stores
  nothing. An id naming no row is a 422 that stores nothing and leaves the
  session usable. Money is stored as a 2-decimal string, rounded half up.
- ``sp.price_set``: written by ``set_price`` when a price is sent and differs
  from the stored one (or there was none). On/off alone writes nothing.
- ``booking.marketplace_created``: one row per item of a chosen-SP booking, in
  the booking's transaction. None without ``sp_id``, none on an idempotent retry.
- Account deletion nulls ``user_id`` and keeps the rows.
"""

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.booking import Booking
from app.models.marketplace_event import CLIENT_EVENTS, SERVER_EVENTS, MarketplaceEvent
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.schemas.analytics import ClientEvent
from app.schemas.booking import CreateBookingItemRequest
from app.services.booking_service import BookingService
from app.services.marketplace_events import ANALYTICS_UNKNOWN_REFERENCE
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _eligible_provider
from tests.test_price_lock import _consumer, _item, _post, _price
from tests.test_price_lock import _service as _lock_service
from tests.test_sp_pricing_api import _patch
from tests.test_sp_pricing_api import _service as _pricing_service
from tests.test_sp_pricing_api import _sp as _pricing_sp

EVENTS = "/api/v1/analytics/events"
ME = "/api/v1/users/me"


async def _events(db, **where) -> list[MarketplaceEvent]:
    await db.flush()
    # populate_existing: a row the session holds is read again, so a change
    # made by an UPDATE (account deletion) shows.
    stmt = (
        select(MarketplaceEvent)
        .order_by(MarketplaceEvent.created_at, MarketplaceEvent.id)
        .execution_options(populate_existing=True)
    )
    for column, value in where.items():
        stmt = stmt.where(getattr(MarketplaceEvent, column) == value)
    return list((await db.scalars(stmt)).all())


# --- Ingest ---------------------------------------------------------------------------


@pytest.fixture
async def world(db_session):
    """A customer, a service with one variant, and an SP that prices it."""
    service, variant = await _lock_service(db_session)
    sp = await _eligible_provider(db_session, service)
    consumer = await UserFactory.create(db_session, role="consumer")
    return {"consumer": consumer, "service": service, "variant": variant, "sp": sp}


def _props(name: str, w: dict) -> dict:
    s, v, sp = str(w["service"].id), str(w["variant"].id), str(w["sp"].id)
    return {
        "marketplace.sheet_opened": {
            "service_id": s,
            "service_variant_id": v,
            "sp_count_in_area": 7,
            "has_boosted": True,
            "user_pincode": "411038",
        },
        "marketplace.sheet_abandoned": {
            "service_id": s,
            "service_variant_id": v,
            "time_spent_ms": 12_500,
            "scroll_depth_pct": 60,
        },
        "marketplace.sp_selected": {
            "service_id": s,
            "service_variant_id": v,
            "sp_id": sp,
            "sp_rank_in_sheet": 0,
            "sp_score": 0.82,
            "price_amount": 2400,
            "is_boosted_sp": False,
            "is_outside_range": False,
        },
        "marketplace.price_change_at_checkout": {
            "service_id": s,
            "service_variant_id": v,
            "sp_id": sp,
            "old_price": 2400,
            "new_price": 2399.5,
            "user_action": "confirmed",
        },
        "marketplace.tile_coming_soon_tapped": {
            "service_id": s,
            "state": "no_pros_online",
            "user_pincode": None,
        },
    }[name]


def _body(name: str, w: dict, *, event_id=None, **props) -> dict:
    return {
        "event_id": str(event_id or uuid.uuid4()),
        "name": name,
        "properties": {**_props(name, w), **props},
    }


async def _send(client, auth_headers, user, body):
    return await client.post(EVENTS, json=body, headers=auth_headers(str(user.id), "consumer"))


def test_the_schema_accepts_exactly_the_client_names():
    names = {
        m.model_fields["name"].annotation.__args__[0] for m in ClientEvent.__args__[0].__args__
    }
    assert names == set(CLIENT_EVENTS)
    assert not names & set(SERVER_EVENTS)


@pytest.mark.asyncio
async def test_each_client_event_is_stored(client, db_session, auth_headers, world):
    expected = {
        "marketplace.sheet_opened": (
            {"sp_count_in_area": 7, "has_boosted": True, "user_pincode": "411038"},
            False,
            True,
        ),
        "marketplace.sheet_abandoned": (
            {"time_spent_ms": 12_500, "scroll_depth_pct": 60},
            False,
            True,
        ),
        "marketplace.sp_selected": (
            {
                "sp_rank_in_sheet": 0,
                "sp_score": 0.82,
                "price_amount": "2400.00",
                "is_boosted_sp": False,
                "is_outside_range": False,
            },
            True,
            True,
        ),
        "marketplace.price_change_at_checkout": (
            {"old_price": "2400.00", "new_price": "2399.50", "user_action": "confirmed"},
            True,
            True,
        ),
        "marketplace.tile_coming_soon_tapped": (
            {"state": "no_pros_online", "user_pincode": None},
            False,
            False,
        ),
    }
    assert set(expected) == set(CLIENT_EVENTS)
    consumer = world["consumer"]
    for name, (payload, has_sp, has_variant) in expected.items():
        body = _body(name, world)
        resp = await _send(client, auth_headers, consumer, body)
        assert resp.status_code == 204, (name, resp.text)
        assert resp.content == b""

        [row] = await _events(db_session, event_id=uuid.UUID(body["event_id"]))
        assert row.name == name
        assert row.user_id == consumer.id
        assert row.service_id == world["service"].id
        assert row.service_variant_id == (world["variant"].id if has_variant else None)
        assert row.sp_id == (world["sp"].id if has_sp else None)
        assert row.booking_id is None
        assert row.occurred_at is None
        # Typed ids are in their columns only.
        assert row.payload == payload, name


@pytest.mark.asyncio
async def test_occurred_at_is_stored(client, db_session, auth_headers, world):
    body = _body("marketplace.sheet_abandoned", world)
    body["occurred_at"] = "2026-11-18T10:15:00+05:30"
    resp = await _send(client, auth_headers, world["consumer"], body)
    assert resp.status_code == 204, resp.text
    [row] = await _events(db_session, event_id=uuid.UUID(body["event_id"]))
    assert row.occurred_at.isoformat() == "2026-11-18T04:45:00+00:00"


@pytest.mark.asyncio
async def test_a_naive_occurred_at_is_422(client, db_session, auth_headers, world):
    body = _body("marketplace.sheet_abandoned", world)
    body["occurred_at"] = "2026-11-18T10:15:00"
    resp = await _send(client, auth_headers, world["consumer"], body)
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        (2400, "2400.00"),
        (2399.5, "2399.50"),
        # A JS float lands on the cent it meant, half up.
        (2199.9999999998, "2200.00"),
        (2399.999, "2400.00"),
        (2399.994, "2399.99"),
        (0.005, "0.01"),
        (0, "0.00"),
        (9999999999.99, "9999999999.99"),
    ],
)
async def test_money_is_stored_as_a_two_decimal_string(
    client, db_session, auth_headers, world, sent, stored
):
    body = _body("marketplace.sp_selected", world, price_amount=sent)
    resp = await _send(client, auth_headers, world["consumer"], body)
    assert resp.status_code == 204, resp.text
    [row] = await _events(db_session, event_id=uuid.UUID(body["event_id"]))
    assert row.payload["price_amount"] == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SERVER_EVENTS + ("marketplace.unknown", "sheet_opened", ""))
async def test_a_name_the_app_may_not_send_is_422(client, db_session, auth_headers, world, name):
    body = _body("marketplace.sheet_opened", world)
    body["name"] = name
    resp = await _send(client, auth_headers, world["consumer"], body)
    assert resp.status_code == 422
    assert await _events(db_session, user_id=world["consumer"].id) == []


@pytest.mark.asyncio
async def test_a_server_event_shape_is_refused_too(client, db_session, auth_headers, world):
    resp = await _send(
        client,
        auth_headers,
        world["consumer"],
        {
            "event_id": str(uuid.uuid4()),
            "name": "sp.price_set",
            "properties": {"sp_id": str(world["sp"].id), "new_price": 100},
        },
    )
    assert resp.status_code == 422
    assert await _events(db_session, user_id=world["consumer"].id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("where", "field", "value"),
    [
        ("properties", "phone", "+919876543210"),
        ("properties", "email", "a@b.in"),
        ("properties", "lat", 18.52),
        ("properties", "user_id", None),
        ("envelope", "user_id", None),
        ("envelope", "ip", "1.2.3.4"),
    ],
)
async def test_an_undeclared_field_is_422(
    client, db_session, auth_headers, world, where, field, value
):
    body = _body("marketplace.sheet_opened", world)
    if value is None:
        value = str(uuid.uuid4())
    (body if where == "envelope" else body["properties"])[field] = value
    resp = await _send(client, auth_headers, world["consumer"], body)
    assert resp.status_code == 422
    assert await _events(db_session, user_id=world["consumer"].id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "field", "value"),
    [
        ("marketplace.sp_selected", "sp_rank_in_sheet", -1),
        ("marketplace.sp_selected", "sp_score", -0.1),
        ("marketplace.sp_selected", "price_amount", -1),
        ("marketplace.sp_selected", "price_amount", 10_000_000_000),
        ("marketplace.sp_selected", "price_amount", None),
        ("marketplace.sheet_abandoned", "scroll_depth_pct", 101),
        ("marketplace.sheet_abandoned", "scroll_depth_pct", -1),
        ("marketplace.sheet_abandoned", "scroll_depth_pct", 42.5),
        ("marketplace.sheet_abandoned", "time_spent_ms", -1),
        ("marketplace.sheet_abandoned", "time_spent_ms", 86_400_001),
        ("marketplace.sheet_opened", "sp_count_in_area", -1),
        ("marketplace.sheet_opened", "user_pincode", "41103"),
        ("marketplace.sheet_opened", "user_pincode", "4110388"),
        ("marketplace.sheet_opened", "user_pincode", "abcdef"),
        ("marketplace.sheet_opened", "user_pincode", "٤١١٠٣٨"),
        ("marketplace.sheet_opened", "user_pincode", 411038),
        ("marketplace.price_change_at_checkout", "user_action", "cancelled"),
        ("marketplace.tile_coming_soon_tapped", "state", "closed"),
        ("marketplace.sheet_opened", "service_variant_id", "not-a-uuid"),
    ],
)
async def test_bounds_and_enums(client, db_session, auth_headers, world, name, field, value):
    resp = await _send(
        client, auth_headers, world["consumer"], _body(name, world, **{field: value})
    )
    assert resp.status_code == 422, resp.text
    assert await _events(db_session, user_id=world["consumer"].id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("marketplace.sp_selected", "price_amount"),
        ("marketplace.sp_selected", "sp_score"),
        ("marketplace.sheet_abandoned", "scroll_depth_pct"),
        # Undeclared, so refused anyway; the point is a 422, not a 500.
        ("marketplace.sheet_abandoned", "lat"),
    ],
)
async def test_non_finite_numbers_are_422_not_500(
    client, db_session, auth_headers, world, raw, name, field
):
    """Python's JSON parser takes NaN / Infinity, and FastAPI's 422 echoes the
    input, which JSON can't encode. The schema turns them into text first."""
    consumer = world["consumer"]
    body = _body(name, world, **{field: "__RAW__"})
    resp = await client.post(
        EVENTS,
        content=json.dumps(body).replace('"__RAW__"', raw),
        headers={**auth_headers(str(consumer.id), "consumer"), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    assert await _events(db_session, user_id=consumer.id) == []


@pytest.mark.asyncio
async def test_a_missing_property_is_422(client, db_session, auth_headers, world):
    body = _body("marketplace.sheet_opened", world)
    del body["properties"]["user_pincode"]
    resp = await _send(client, auth_headers, world["consumer"], body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_without_a_token_is_401(client, db_session, world):
    resp = await client.post(EVENTS, json=_body("marketplace.sheet_opened", world))
    assert resp.status_code == 401
    assert await _events(db_session, service_id=world["service"].id) == []


@pytest.mark.asyncio
async def test_a_repeated_event_id_is_204_and_stores_one_row(
    client, db_session, auth_headers, world
):
    event_id = uuid.uuid4()
    first = _body("marketplace.sheet_opened", world, event_id=event_id)
    assert (await _send(client, auth_headers, world["consumer"], first)).status_code == 204
    # The retry differs, and another user sends it too: the first row stands.
    other = await UserFactory.create(db_session, role="consumer")
    retry = _body("marketplace.sheet_opened", world, event_id=event_id, sp_count_in_area=99)
    assert (await _send(client, auth_headers, world["consumer"], retry)).status_code == 204
    assert (await _send(client, auth_headers, other, retry)).status_code == 204

    [row] = await _events(db_session, event_id=event_id)
    assert row.user_id == world["consumer"].id
    assert row.payload["sp_count_in_area"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["service_id", "service_variant_id", "sp_id"])
async def test_an_unknown_id_is_422_and_stores_nothing(
    client, db_session, auth_headers, world, field
):
    consumer = world["consumer"]
    bad = _body("marketplace.sp_selected", world, **{field: str(uuid.uuid4())})
    resp = await _send(client, auth_headers, consumer, bad)
    assert resp.status_code == 422
    assert resp.json()["code"] == ANALYTICS_UNKNOWN_REFERENCE
    assert await _events(db_session, event_id=uuid.UUID(bad["event_id"])) == []

    # Only the savepoint rolled back: the same session keeps working.
    good = _body("marketplace.sp_selected", world)
    assert (await _send(client, auth_headers, consumer, good)).status_code == 204
    assert len(await _events(db_session, user_id=consumer.id)) == 1


@pytest.mark.asyncio
async def test_user_id_is_the_callers(client, db_session, auth_headers, world):
    other = await UserFactory.create(db_session, role="consumer")
    body = _body("marketplace.tile_coming_soon_tapped", world)
    assert (await _send(client, auth_headers, other, body)).status_code == 204
    [row] = await _events(db_session, event_id=uuid.UUID(body["event_id"]))
    assert row.user_id == other.id


@pytest.mark.asyncio
async def test_the_rate_limit_is_600_an_hour(client, db_session, auth_headers, redis_client, world):
    consumer = world["consumer"]
    key = f"rl:analytics_events:{consumer.id}"
    await redis_client.set(key, 599, ex=3600)
    try:
        ok = await _send(client, auth_headers, consumer, _body("marketplace.sheet_opened", world))
        assert ok.status_code == 204
        assert ok.headers["X-RateLimit-Limit"] == "600"
        over = await _send(client, auth_headers, consumer, _body("marketplace.sheet_opened", world))
        assert over.status_code == 429
        assert len(await _events(db_session, user_id=consumer.id)) == 1
    finally:
        await redis_client.delete(key)


# --- sp.price_set -------------------------------------------------------------------


@pytest.fixture
async def priced(db_session):
    service, variant = await _pricing_service(db_session)
    sp = await _pricing_sp(db_session, service)
    return sp, service, variant


async def _price_events(db, sp):
    return await _events(db, sp_id=sp.id, name="sp.price_set")


@pytest.mark.asyncio
async def test_a_first_price_has_no_old_price(client, db_session, auth_headers, priced):
    sp, service, variant = priced
    resp = await _patch(client, auth_headers, sp, variant, price_amount=2400)
    assert resp.status_code == 200, resp.text

    [row] = await _price_events(db_session, sp)
    assert (row.user_id, row.sp_id, row.service_id, row.service_variant_id) == (
        sp.id,
        sp.id,
        service.id,
        variant.id,
    )
    assert row.booking_id is None and row.occurred_at is None
    assert row.payload == {"old_price": None, "new_price": "2400.00", "out_of_bounds": False}


@pytest.mark.asyncio
async def test_a_changed_price_has_old_and_new(client, db_session, auth_headers, priced):
    sp, _, variant = priced
    await _patch(client, auth_headers, sp, variant, price_amount=2400)
    resp = await _patch(client, auth_headers, sp, variant, price_amount=2299.5, is_active=False)
    assert resp.status_code == 200, resp.text

    first, second = await _price_events(db_session, sp)
    assert first.payload["old_price"] is None
    assert second.payload == {
        "old_price": "2400.00",
        "new_price": "2299.50",
        "out_of_bounds": False,
    }


@pytest.mark.asyncio
async def test_the_same_price_writes_nothing(client, db_session, auth_headers, priced):
    sp, _, variant = priced
    await _patch(client, auth_headers, sp, variant, price_amount=2400)
    # 2400 and "2400.00" are the same price, with or without a toggle.
    assert (
        await _patch(client, auth_headers, sp, variant, price_amount="2400.00")
    ).status_code == 200
    resp = await _patch(client, auth_headers, sp, variant, price_amount=2400, is_active=False)
    assert resp.status_code == 200
    assert len(await _price_events(db_session, sp)) == 1


@pytest.mark.asyncio
async def test_on_off_alone_writes_nothing(client, db_session, auth_headers, priced):
    sp, _, variant = priced
    await _patch(client, auth_headers, sp, variant, price_amount=2400)
    for active in (False, True):
        resp = await _patch(client, auth_headers, sp, variant, is_active=active)
        assert resp.status_code == 200, resp.text
    assert len(await _price_events(db_session, sp)) == 1


@pytest.mark.asyncio
async def test_a_refused_toggle_writes_nothing(client, db_session, auth_headers, priced):
    sp, _, variant = priced
    resp = await _patch(client, auth_headers, sp, variant, is_active=True)
    assert resp.status_code == 422
    assert await _price_events(db_session, sp) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("price", "outside"), [(1874.24, True), (1874.25, False), (3200, True)])
async def test_out_of_bounds_is_the_recommended_range_flag(
    client, db_session, auth_headers, priced, price, outside
):
    sp, _, variant = priced
    resp = await _patch(client, auth_headers, sp, variant, price_amount=price)
    assert resp.status_code == 200, resp.text
    assert resp.json()["outside_recommended_range"] is outside
    [row] = await _price_events(db_session, sp)
    assert row.payload["out_of_bounds"] is outside


# --- booking.marketplace_created ----------------------------------------------------


async def _two_item_cart(db):
    """A chosen SP pricing two services; returns what ``_post`` needs."""
    service_a, variant_a = await _lock_service(db)
    service_b, variant_b = await _lock_service(
        db, base_price=Decimal("1000.00"), commission=Decimal("300.00")
    )
    sp = await _eligible_provider(db, service_a)
    db.add(ProviderServiceModel(provider_id=sp.id, service_id=service_b.id))
    await _price(db, sp, service_a, variant_a, "2000.00")
    await _price(db, sp, service_b, variant_b, "799.50")
    consumer, address = await _consumer(db)
    items = [_item(service_a, variant_a, 2000), _item(service_b, variant_b, 799.5, quantity=3)]
    return sp, consumer, address, items, (service_a, variant_a), (service_b, variant_b)


@pytest.mark.asyncio
async def test_a_chosen_sp_booking_writes_one_row_per_item(client, db_session, auth_headers):
    sp, consumer, address, items, (sa, va), (sb, vb) = await _two_item_cart(db_session)

    resp = await _post(client, auth_headers, consumer, address, items, sp=sp)
    assert resp.status_code == 201, resp.text
    booking_id = uuid.UUID(resp.json()["id"])

    rows = await _events(db_session, booking_id=booking_id)
    assert {r.name for r in rows} == {"booking.marketplace_created"}
    by_variant = {r.service_variant_id: r for r in rows}
    assert set(by_variant) == {va.id, vb.id}
    for row in rows:
        assert (row.user_id, row.sp_id, row.occurred_at) == (consumer.id, sp.id, None)
    assert by_variant[va.id].service_id == sa.id
    assert by_variant[va.id].payload == {
        "locked_price": "2000.00",
        "quantity": 1,
        "reference_price": "2499.00",
    }
    assert by_variant[vb.id].service_id == sb.id
    assert by_variant[vb.id].payload == {
        "locked_price": "799.50",
        "quantity": 3,
        "reference_price": "1000.00",
    }


@pytest.mark.asyncio
async def test_a_booking_without_sp_id_writes_nothing(client, db_session, auth_headers):
    _, consumer, address, items, _, _ = await _two_item_cart(db_session)
    resp = await _post(client, auth_headers, consumer, address, items)
    assert resp.status_code == 201, resp.text
    assert await _events(db_session, booking_id=uuid.UUID(resp.json()["id"])) == []
    assert await _events(db_session, user_id=consumer.id) == []


@pytest.mark.asyncio
async def test_a_refused_booking_writes_nothing(client, db_session, auth_headers):
    sp, consumer, address, items, _, _ = await _two_item_cart(db_session)
    items[0]["quoted_price"] = 1999  # the SP charges 2000: 409 PRICE_CHANGED
    resp = await _post(client, auth_headers, consumer, address, items, sp=sp)
    assert resp.status_code == 409, resp.text
    assert await _events(db_session, user_id=consumer.id) == []


@pytest.mark.asyncio
async def test_a_failure_after_the_items_rolls_the_events_back(db_session, monkeypatch):
    sp, consumer, address, items, _, _ = await _two_item_cart(db_session)

    async def boom(self, **kwargs):
        raise RuntimeError("after the items flush")

    monkeypatch.setattr(BookingService, "_log_transition", boom)
    savepoint = await db_session.begin_nested()
    with pytest.raises(RuntimeError):
        await BookingService(db_session).create_booking(
            consumer_id=consumer.id,
            items=[CreateBookingItemRequest(**i) for i in items],
            address_id=address.id,
            payment_method="cash",
            sp_id=sp.id,
        )
    # The rows were written, in the booking's transaction...
    assert len(await _events(db_session, user_id=consumer.id)) == 2
    await savepoint.rollback()

    # ...and go with it.
    assert await _events(db_session, user_id=consumer.id) == []
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Booking).where(Booking.consumer_id == consumer.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_an_idempotent_retry_adds_no_rows(client, db_session, auth_headers):
    sp, consumer, address, items, _, _ = await _two_item_cart(db_session)
    body = {
        "items": items,
        "address_id": str(address.id),
        "payment_method": "cash",
        "sp_id": str(sp.id),
    }
    headers = {
        **auth_headers(str(consumer.id), "consumer"),
        "Idempotency-Key": f"svc419-{uuid.uuid4()}",
    }
    first = await client.post("/api/v1/bookings/", json=body, headers=headers)
    retry = await client.post("/api/v1/bookings/", json=body, headers=headers)
    assert (first.status_code, retry.status_code) == (201, 201)
    assert first.json()["id"] == retry.json()["id"]
    assert len(await _events(db_session, user_id=consumer.id)) == 2


# --- Account deletion -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_deletion_nulls_user_id_and_keeps_the_rows(
    client, db_session, auth_headers, world, monkeypatch
):
    from app.utils import storage

    monkeypatch.setattr(storage, "delete_file", lambda key: None)
    monkeypatch.setattr(storage, "delete_prefix", lambda prefix: 0)
    consumer = world["consumer"]
    other = await UserFactory.create(db_session, role="consumer")
    mine = [_body(n, world) for n in ("marketplace.sheet_opened", "marketplace.sp_selected")]
    theirs = _body("marketplace.sheet_opened", world)
    for body in mine:
        assert (await _send(client, auth_headers, consumer, body)).status_code == 204
    assert (await _send(client, auth_headers, other, theirs)).status_code == 204

    resp = await client.request("DELETE", ME, headers=auth_headers(str(consumer.id), "consumer"))
    assert resp.status_code == 204, resp.text

    for body in mine:
        [row] = await _events(db_session, event_id=uuid.UUID(body["event_id"]))
        assert row.user_id is None
        assert row.service_id == world["service"].id
    [kept] = await _events(db_session, event_id=uuid.UUID(theirs["event_id"]))
    assert kept.user_id == other.id


@pytest.mark.asyncio
async def test_a_deleted_sp_keeps_sp_id_and_loses_user_id(
    client, db_session, auth_headers, priced, monkeypatch
):
    from app.utils import storage

    monkeypatch.setattr(storage, "delete_file", lambda key: None)
    monkeypatch.setattr(storage, "delete_prefix", lambda prefix: 0)
    sp, _, variant = priced
    assert (await _patch(client, auth_headers, sp, variant, price_amount=2400)).status_code == 200

    resp = await client.request("DELETE", ME, headers=auth_headers(str(sp.id), "provider"))
    assert resp.status_code == 204, resp.text

    [row] = await _price_events(db_session, sp)
    assert (row.user_id, row.sp_id) == (None, sp.id)
