"""Tests pinning the multi-service booking behaviour introduced with the cart.

These pin invariants the design rests on:

* a cart of N services becomes one booking with N ``booking_items`` rows —
  not N separate bookings under a shared identifier;
* the aggregate money on the booking equals the sum of the item lines,
  computed at booking time and never re-read from the live catalogue after;
* matching-policy (i) — a provider must be subscribed to *every* service in
  the cart to be eligible, expressed by ``eligible_providers_multi_stmt``;
* the legacy single-service request shape (``service_id`` only) still works,
  normalised by the schema into a 1-item cart. This is the compat door we
  need for PR 2c to stay reviewable in isolation.
"""

import uuid

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import selectinload

from app.models.address import Address
from app.models.booking import Booking
from app.models.booking_item import BookingItem
from app.models.category import Category
from app.models.provider_service import ProviderService
from app.models.service import Service
from app.schemas.booking import (
    BookingResponse,
    CreateBookingItemRequest,
    CreateBookingRequest,
)
from app.services.booking_service import BookingService
from app.services.provider_matching import (
    eligible_providers_multi_stmt,
    is_eligible_provider_multi_stmt,
)
from app.utils.geo import build_point_wkt
from tests.factories.user_factory import UserFactory

# Pune-ish anchor coordinate. Origin does not matter for these tests as
# long as the provider's radius is generous enough to cover it.
LAT, LNG = 18.5204, 73.8567


async def _consumer_with_address(db_session):
    consumer = await UserFactory.create(db_session, role="consumer")
    addr = Address(
        user_id=consumer.id,
        label="home",
        full_address="Test address, Pune",
        location=build_point_wkt(LAT, LNG),
    )
    db_session.add(addr)
    await db_session.flush()
    return consumer, addr


async def _service(
    db_session, *, name: str = "Test Service", base_price: float = 100.0, is_active: bool = True
):
    cat = Category(
        name=f"Cat {uuid.uuid4()}",
        slug=f"cat-{uuid.uuid4()}",
        is_active=True,
        sort_order=1,
    )
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name=name,
        base_price=base_price,
        duration_minutes=60,
        is_active=is_active,
    )
    db_session.add(svc)
    await db_session.flush()
    return svc


# ---------------------------------------------------------------------------
# CreateBookingRequest normalisation
# ---------------------------------------------------------------------------


def test_legacy_service_id_shape_normalises_to_a_one_item_cart():
    """PR 2c hasn't shipped yet — the mobile app still sends ``service_id``.

    The schema normalises that into the one-shape the service layer sees.
    Without this door PR 2a would break the current mobile app the moment
    it merges.
    """
    from uuid import uuid4

    svc_id = uuid4()
    addr_id = uuid4()
    req = CreateBookingRequest(service_id=svc_id, address_id=addr_id)
    assert req.items is not None
    assert len(req.items) == 1
    assert req.items[0].service_id == svc_id
    assert req.items[0].quantity == 1


def test_creating_with_both_shapes_at_once_is_rejected():
    """One or the other, not both — silent precedence would be a footgun."""
    from uuid import uuid4

    with pytest.raises(ValueError, match="either"):
        CreateBookingRequest(
            service_id=uuid4(),
            items=[CreateBookingItemRequest(service_id=uuid4())],
            address_id=uuid4(),
        )


def test_creating_with_neither_shape_is_rejected():
    """A booking must have at least one service — 400, not a silent empty cart."""
    from uuid import uuid4

    with pytest.raises(ValueError, match="at least one"):
        CreateBookingRequest(address_id=uuid4())


# ---------------------------------------------------------------------------
# Service layer: cart creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cart_becomes_one_booking_with_one_item_per_service(db_session):
    consumer, addr = await _consumer_with_address(db_session)
    svc_a = await _service(db_session, name="Deep Clean", base_price=1000.0)
    svc_b = await _service(db_session, name="Sofa Clean", base_price=500.0)

    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[
            CreateBookingItemRequest(service_id=svc_a.id, quantity=1),
            CreateBookingItemRequest(service_id=svc_b.id, quantity=1),
        ],
        address_id=addr.id,
    )

    # Reload from a fresh query so we're testing what actually landed, not
    # the in-memory object the service returned.
    fresh = (
        await db_session.execute(
            select(Booking).options(selectinload(Booking.items)).where(Booking.id == booking.id)
        )
    ).scalar_one()
    assert len(fresh.items) == 2
    assert {item.service_id for item in fresh.items} == {svc_a.id, svc_b.id}


@pytest.mark.asyncio
async def test_create_leaves_item_service_loaded_for_the_response_builder(db_session):
    """``create_booking`` must hand back items with ``service`` already loaded.

    The 201 body is built from the object this method returns, with no reload in
    between, and ``BookingItemResponse.from_item`` reads ``item.service.name``
    for every item. ``BookingItem.service`` is ``lazy="raise_on_sql"``, so a
    create path that leaves it unloaded raises ``InvalidRequestError`` inside the
    response builder. That escapes as a bare 500 which, having bypassed
    CORSMiddleware, reaches a browser as an opaque network error rather than a
    status code (SVC-230).

    Asserting on ``unloaded`` rather than on "serialising raises" is deliberate.
    ``raise_on_sql`` fires only when the load would emit SQL, and a many-to-one
    is served from the identity map first — here the ``Service`` rows were
    created in this very session, so serialisation succeeds even with the bug
    present. An exception-based test would therefore pin session bookkeeping
    rather than this method's contract, which is: the relationship is loaded
    before the caller gets the object.
    """
    consumer, addr = await _consumer_with_address(db_session)
    svc_a = await _service(db_session, name="Deep Clean", base_price=1000.0)
    svc_b = await _service(db_session, name="Sofa Clean", base_price=500.0)

    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[
            CreateBookingItemRequest(service_id=svc_a.id, quantity=1),
            CreateBookingItemRequest(service_id=svc_b.id, quantity=2),
        ],
        address_id=addr.id,
    )

    for item in booking.items:
        assert "service" not in inspect(item).unloaded, (
            "BookingItem.service must be pre-loaded by create_booking — "
            "from_item reads it and the relationship is lazy='raise_on_sql'"
        )

    # And the thing the route itself does, end to end.
    response = BookingResponse.for_viewer(booking, consumer.id)
    assert {item.service_name for item in response.items} == {"Deep Clean", "Sofa Clean"}


@pytest.mark.asyncio
async def test_booking_aggregates_equal_sum_of_item_totals(db_session):
    """The three denormalised aggregates on Booking are sums over the items.

    Payments and payouts read those aggregates; if this ever drifts, the
    invoice and the sum of the line items disagree.
    """
    consumer, addr = await _consumer_with_address(db_session)
    svc_a = await _service(db_session, name="A", base_price=200.0)
    svc_b = await _service(db_session, name="B", base_price=300.0)

    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[
            CreateBookingItemRequest(service_id=svc_a.id, quantity=2),  # 400
            CreateBookingItemRequest(service_id=svc_b.id, quantity=1),  # 300
        ],
        address_id=addr.id,
    )

    assert booking.service_amount == 700.0
    # 20% commission
    assert booking.platform_commission == 140.0
    assert booking.provider_payout == 560.0


@pytest.mark.asyncio
async def test_item_prices_snapshot_at_booking_time(db_session):
    """Unit price on the item comes from ``services.base_price`` at create
    time, not from a live join. A later price update on the service must
    not retroactively change what the customer was billed.
    """
    consumer, addr = await _consumer_with_address(db_session)
    svc = await _service(db_session, name="C", base_price=1234.0)

    booking = await BookingService(db_session).create_booking(
        consumer_id=consumer.id,
        items=[CreateBookingItemRequest(service_id=svc.id)],
        address_id=addr.id,
    )

    # Mutate the catalogue after the fact.
    svc.base_price = 9999.0
    await db_session.flush()

    fresh = (
        await db_session.execute(select(BookingItem).where(BookingItem.booking_id == booking.id))
    ).scalar_one()
    assert fresh.unit_price == 1234.0  # NOT 9999


@pytest.mark.asyncio
async def test_inactive_service_refuses_the_whole_cart(db_session):
    """A single inactive item takes the whole cart down — see
    ``create_booking``'s docstring. A booking whose second item can never
    be delivered would strand the customer at completion or force a
    mid-lifecycle auto-cancel, both worse than a clean 400 upfront.
    """
    consumer, addr = await _consumer_with_address(db_session)
    svc_ok = await _service(db_session, name="OK", base_price=100.0)
    svc_off = await _service(db_session, name="Off", base_price=100.0, is_active=False)

    with pytest.raises(ValueError, match="not found or inactive"):
        await BookingService(db_session).create_booking(
            consumer_id=consumer.id,
            items=[
                CreateBookingItemRequest(service_id=svc_ok.id),
                CreateBookingItemRequest(service_id=svc_off.id),
            ],
            address_id=addr.id,
        )


# ---------------------------------------------------------------------------
# Matching-policy (i)
# ---------------------------------------------------------------------------


async def _online_verified_provider(db_session, service_ids: list, *, has_token: bool = True):
    """Provider profile subscribed to every service_id passed.

    ``has_token=False`` models SVC-276: online but with no FCM token, so the
    broadcast excludes them by construction.
    """
    from app.models.provider_profile import KYCStatus, ProviderProfile

    user = await UserFactory.create(
        db_session,
        role="provider",
        fcm_token=(f"fcm-{uuid.uuid4().hex[:16]}" if has_token else None),
    )
    profile = ProviderProfile(
        user_id=user.id,
        kyc_status=KYCStatus.VERIFIED,
        is_online=True,
        service_area_center=build_point_wkt(LAT, LNG),
        service_area_radius_km=50,
    )
    db_session.add(profile)
    for svc_id in service_ids:
        db_session.add(ProviderService(provider_id=user.id, service_id=svc_id))
    await db_session.flush()
    return user


@pytest.mark.asyncio
async def test_matching_policy_i_requires_provider_to_cover_every_service(db_session):
    """The invariant matching-policy (i) exists to guarantee.

    A provider subscribed to only the first service in a two-item cart
    is refused. If this ever regresses, the customer can be assigned a
    provider who cannot perform half their booking.
    """
    _, addr = await _consumer_with_address(db_session)
    svc_a = await _service(db_session, base_price=100.0)
    svc_b = await _service(db_session, base_price=100.0)

    # Partial provider — knows A only.
    partial = await _online_verified_provider(db_session, [svc_a.id])
    # Full provider — knows both.
    full = await _online_verified_provider(db_session, [svc_a.id, svc_b.id])

    location = build_point_wkt(LAT, LNG)
    stmt = eligible_providers_multi_stmt([svc_a.id, svc_b.id], location)
    users = (await db_session.execute(stmt)).scalars().all()
    ids = {u.id for u in users}
    assert full.id in ids
    assert partial.id not in ids


@pytest.mark.asyncio
async def test_null_token_provider_excluded_from_multi_broadcast_and_accept(db_session):
    """SVC-276: a provider without an FCM token is unreachable, so the
    multi-service broadcast must not select them, and — by the SVC-153
    invariant — the accept-side membership check must refuse them for the
    same reason. Otherwise a stranded provider could still be shown as
    "eligible" and receive nothing.
    """
    _, _ = await _consumer_with_address(db_session)
    svc_a = await _service(db_session, base_price=100.0)
    svc_b = await _service(db_session, base_price=100.0)

    reachable = await _online_verified_provider(db_session, [svc_a.id, svc_b.id])
    unreachable = await _online_verified_provider(db_session, [svc_a.id, svc_b.id], has_token=False)
    location = build_point_wkt(LAT, LNG)

    stmt = eligible_providers_multi_stmt([svc_a.id, svc_b.id], location)
    ids = {u.id for u in (await db_session.execute(stmt)).scalars().all()}
    assert reachable.id in ids
    assert unreachable.id not in ids, "unreachable provider was still broadcast to"

    # And the accept-side check refuses them — the two sets must not drift.
    still_ok = (
        await db_session.execute(
            is_eligible_provider_multi_stmt([svc_a.id, svc_b.id], location, reachable.id)
        )
    ).scalar_one_or_none()
    assert still_ok is not None

    now_no = (
        await db_session.execute(
            is_eligible_provider_multi_stmt([svc_a.id, svc_b.id], location, unreachable.id)
        )
    ).scalar_one_or_none()
    assert now_no is None, "accept let a provider through the broadcast had excluded"


@pytest.mark.asyncio
async def test_is_eligible_multi_agrees_with_eligible_multi(db_session):
    """The endpoint that decides "may this provider resolve this offer" and
    the broadcast that decides "who to push to" must use the same rule —
    the invariant SVC-153 established, upheld here for multi-service.
    """
    _, addr = await _consumer_with_address(db_session)
    svc_a = await _service(db_session, base_price=100.0)
    svc_b = await _service(db_session, base_price=100.0)

    partial = await _online_verified_provider(db_session, [svc_a.id])
    full = await _online_verified_provider(db_session, [svc_a.id, svc_b.id])
    location = build_point_wkt(LAT, LNG)

    # The membership predicate for the full provider succeeds …
    ok = (
        await db_session.execute(
            is_eligible_provider_multi_stmt([svc_a.id, svc_b.id], location, full.id)
        )
    ).scalar_one_or_none()
    assert ok is not None

    # … and for the partial provider fails.
    not_ok = (
        await db_session.execute(
            is_eligible_provider_multi_stmt([svc_a.id, svc_b.id], location, partial.id)
        )
    ).scalar_one_or_none()
    assert not_ok is None


@pytest.mark.asyncio
async def test_single_item_cart_delegates_to_single_service_form(db_session):
    """Preserve the pre-cart eligibility behaviour when the cart has one item
    — same predicate, same result. Belt-and-braces to make sure the
    multi-item HAVING clause doesn't accidentally shift semantics for the
    common case.
    """
    _, addr = await _consumer_with_address(db_session)
    svc = await _service(db_session, base_price=100.0)
    provider = await _online_verified_provider(db_session, [svc.id])
    location = build_point_wkt(LAT, LNG)

    stmt = eligible_providers_multi_stmt([svc.id], location)
    users = (await db_session.execute(stmt)).scalars().all()
    assert provider.id in {u.id for u in users}
