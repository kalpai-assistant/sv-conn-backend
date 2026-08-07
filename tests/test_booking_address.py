"""The consumer's address reaches both booking parties, and nobody else (SVC-170).

``BookingResponse`` carried no address at all, so the destination reached a
provider exactly once — on the pre-accept offer card — and vanished the moment
they tapped Accept. The flow SVC-41 shipped is *travel to the customer's home*,
so the app was asking providers to set out with a service name and a price.

Three things are pinned here:

* **Who sees it.** Both parties to the booking, on every route that returns one.
  Not a non-party, and not the bulk admin listing — see ``for_viewer``.
* **Coordinates survive the round trip.** ``lat``/``lng`` are decoded from the
  PostGIS value in Python (``point_lat_lng``), which has to handle the geography
  in both the forms it takes: a ``WKBElement`` off a loaded row, and the EWKT
  string an address assigned in this session still holds. Production is the
  first; the test suite is mostly the second; both are covered below.
* **No N+1.** The list route serves up to 50 rows, and the per-row
  ``db.get(Address, ...)`` this codebase uses elsewhere would be one query per
  row. ``test_list_route_loads_addresses_in_one_query`` counts them.

The doorstep-PIN rule this sits beside lives in ``test_doorstep_otp_visibility.py``.
"""

import uuid

import pytest
from geoalchemy2.elements import WKBElement
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.service import Service
from app.schemas.booking import BookingResponse
from app.utils.geo import build_point_wkt, point_lat_lng
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

# Bengaluru, and deliberately not a round number in either axis: WKT is written
# x-then-y, so a lat/lng swap anywhere in the decode would sail through if the
# two values were interchangeable.
LAT, LNG = 12.9716, 77.5946


async def _booking_with_address(
    db_session,
    *,
    status: BookingStatus = BookingStatus.ACCEPTED,
    located: bool = True,
    full_address: str = "42 Residency Road, Bengaluru 560025",
    label: str = "home",
):
    """One booking, its consumer, its assigned provider, and its address."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    cat = Category(name="Cleaning", slug="cleaning", is_active=True, sort_order=1)
    db_session.add(cat)
    await db_session.flush()
    svc = Service(
        category_id=cat.id,
        name="Basic Clean",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(svc)
    address = Address(
        user_id=consumer.id,
        label=label,
        full_address=full_address,
        location=build_point_wkt(LAT, LNG) if located else None,
    )
    db_session.add(address)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=svc.id,
        address_id=address.id,
        status=status,
    )
    return consumer, provider, booking, address


# ------------------------------------------------------------------
# GET /bookings/{id} — the two parties
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assigned_provider_gets_the_address(client, auth_headers, db_session):
    """The ticket in one test: after accepting, the provider can see where to go."""
    _, provider, booking, address = await _booking_with_address(db_session)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    body = response.json()["address"]
    assert body["id"] == str(address.id)
    assert body["label"] == "home"
    assert body["full_address"] == "42 Residency Road, Bengaluru 560025"
    assert (body["lat"], body["lng"]) == (LAT, LNG)


@pytest.mark.asyncio
async def test_address_survives_the_database_round_trip(client, auth_headers, db_session):
    """The production representation, which the other tests do not exercise.

    Everywhere else in this file the Address was created in this session, so the
    identity map hands back the EWKT *string* that was assigned to it. A real
    request reads a row written by an earlier one and gets a ``WKBElement``
    instead — a different branch of ``point_lat_lng`` entirely. Expunging forces
    the handler to load it from Postgres and decode the binary form.
    """
    _, provider, booking, address = await _booking_with_address(db_session)
    db_session.expunge(address)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    body = response.json()["address"]
    assert (body["lat"], body["lng"]) == (LAT, LNG)


@pytest.mark.asyncio
async def test_consumer_gets_the_address(client, auth_headers, db_session):
    consumer, _, booking, address = await _booking_with_address(db_session)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["address"]["id"] == str(address.id)


@pytest.mark.asyncio
async def test_address_without_coordinates_returns_null_lat_lng(client, auth_headers, db_session):
    """`location` is nullable, so the coordinates are an enhancement on the text.

    The provider must still get the address they can read, rather than a 500 or a
    dropped field, when the consumer saved it without a geocode.
    """
    _, provider, booking, _ = await _booking_with_address(db_session, located=False)

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    body = response.json()["address"]
    assert body["full_address"] == "42 Residency Road, Bengaluru 560025"
    assert body["lat"] is None
    assert body["lng"] is None


# ------------------------------------------------------------------
# GET /bookings/ — every row
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_list_carries_the_address_on_every_row(client, auth_headers, db_session):
    """The job list is where the provider picks the next job — one row with the
    address and one without would be worse than none having it."""
    consumer, provider, first, address = await _booking_with_address(db_session)
    second = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=first.service_id,
        address_id=address.id,
        status=BookingStatus.EN_ROUTE,
    )

    response = await client.get(
        "/api/v1/bookings/",
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    rows = response.json()["bookings"]
    assert {row["id"] for row in rows} == {str(first.id), str(second.id)}
    assert all(row["address"]["id"] == str(address.id) for row in rows)
    assert all((row["address"]["lat"], row["address"]["lng"]) == (LAT, LNG) for row in rows)


@pytest.mark.asyncio
async def test_consumer_list_carries_the_address(client, auth_headers, db_session):
    consumer, _, booking, address = await _booking_with_address(db_session)

    response = await client.get(
        "/api/v1/bookings/",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    assert [row["address"]["id"] for row in response.json()["bookings"]] == [str(address.id)]


@pytest.mark.asyncio
async def test_list_route_loads_addresses_in_one_query(client, auth_headers, db_session):
    """Three bookings, one addresses query — not one per row.

    The explicit ``db.get(Address, ...)`` this codebase uses on single-booking
    paths is the natural thing to reach for here and is N+1 at up to 50 rows a
    page. Counting statements is the only way this stays true: a per-row fetch
    passes every other assertion in this file.
    """
    consumer, provider, first, address = await _booking_with_address(db_session)
    for _ in range(2):
        await BookingFactory.create(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            service_id=first.service_id,
            address_id=address.id,
            status=BookingStatus.ACCEPTED,
        )

    executed: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        response = await client.get(
            "/api/v1/bookings/",
            headers=auth_headers(str(provider.id), "provider"),
        )
    finally:
        event.remove(Engine, "before_cursor_execute", _record)

    assert response.status_code == 200, response.text
    assert len(response.json()["bookings"]) == 3

    address_queries = [s for s in executed if "FROM addresses" in s]
    assert len(address_queries) == 1, (
        f"expected one addresses query for the page, got {len(address_queries)}:\n"
        + "\n".join(address_queries)
    )


# ------------------------------------------------------------------
# Everyone else
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unassigned_provider_gets_no_booking_at_all(client, auth_headers, db_session):
    """A provider who is not on this booking is refused before serialisation.

    The address is not the only thing withheld — the whole booking is (SVC-153,
    and ``get_booking``'s own check). Pinned here so a future route that widens
    access has to come past this test.
    """
    _, _, booking, _ = await _booking_with_address(db_session)
    outsider = await UserFactory.create(db_session, role="provider")

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(outsider.id), "provider"),
    )

    assert response.status_code == 403, response.text
    assert "address" not in response.text


@pytest.mark.asyncio
async def test_another_consumer_is_refused(client, auth_headers, db_session):
    _, _, booking, _ = await _booking_with_address(db_session)
    stranger = await UserFactory.create(db_session, role="consumer")

    response = await client.get(
        f"/api/v1/bookings/{booking.id}",
        headers=auth_headers(str(stranger.id), "consumer"),
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_list_shows_only_the_callers_own_bookings(client, auth_headers, db_session):
    """The other half of the listing guarantee: a provider's page is filtered to
    their own jobs, so no address reaches them via someone else's row."""
    _, _, booking, _ = await _booking_with_address(db_session)
    outsider = await UserFactory.create(db_session, role="provider")

    response = await client.get(
        "/api/v1/bookings/",
        headers=auth_headers(str(outsider.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["bookings"] == []
    assert str(booking.id) not in response.text


@pytest.mark.asyncio
async def test_non_party_is_blanked_at_the_serialisation_boundary(db_session):
    """Defence in depth, and the reason the rule lives in ``for_viewer``.

    No route can reach this today — ``get_booking`` raises for a non-party and
    ``list_bookings`` filters — so it is asserted against the schema directly.
    The guarantee should survive a future route that forgets the access check,
    which is exactly how ``doorstep_otp`` came to leak (SVC-169).
    """
    _, _, booking, _ = await _booking_with_address(db_session)

    response = BookingResponse.for_viewer(booking, viewer_id=uuid.uuid4())

    assert response.address is None


@pytest.mark.asyncio
async def test_admin_listing_carries_no_addresses(client, auth_headers, db_session):
    """A deliberate call, not an oversight — see ``for_viewer``'s docstring.

    ``GET /admin/bookings`` is a bulk page of up to 200 rows spanning every
    consumer on the platform, and a table of bookings does not need one address
    to render, let alone 200. The reasoning differs from the doorstep PIN next to
    it: minimising a wholesale PII surface, not keeping a secret. The targeted
    form of the question is served — see the two tests below.
    """
    _, _, booking, _ = await _booking_with_address(db_session)
    admin = await UserFactory.create(db_session, role="admin")

    response = await client.get(
        "/api/v1/admin/bookings",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 200, response.text
    rows = response.json()["bookings"]
    assert str(booking.id) in {row["id"] for row in rows}
    assert all(row["address"] is None for row in rows)


@pytest.mark.asyncio
async def test_admin_detail_route_does_carry_the_address(client, auth_headers, db_session):
    """The other half of that decision, and the reason it is defensible.

    Withholding on the listing is about *bulk*, not about admins: one booking
    asked for by id is a support question about a specific job. If this returned
    nothing either, an admin fielding "the provider can't find the address" would
    have to go query the database by hand — which is strictly worse for privacy,
    being unaudited and unscoped.
    """
    _, _, booking, address = await _booking_with_address(db_session)
    admin = await UserFactory.create(db_session, role="admin")

    response = await client.get(
        f"/api/v1/admin/bookings/{booking.id}",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 200, response.text
    body = response.json()["address"]
    assert body["id"] == str(address.id)
    assert body["full_address"] == "42 Residency Road, Bengaluru 560025"
    assert (body["lat"], body["lng"]) == (LAT, LNG)


@pytest.mark.asyncio
async def test_admin_detail_route_still_withholds_the_doorstep_pin(
    client, auth_headers, db_session
):
    """The asymmetry that makes `for_admin` narrower than "admins see all".

    The PIN's whole value is that the provider cannot obtain it except from the
    customer at the door (SVC-169), so an admin holding it could start a job
    remotely. Widening the address must not quietly widen that too — this is the
    test that fails if `for_admin` ever stops being built on `for_viewer`.
    """
    _, _, booking, _ = await _booking_with_address(db_session, status=BookingStatus.EN_ROUTE)
    admin = await UserFactory.create(db_session, role="admin")

    response = await client.get(
        f"/api/v1/admin/bookings/{booking.id}",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["doorstep_otp"] is None


# ------------------------------------------------------------------
# The decoder itself
# ------------------------------------------------------------------


def test_point_lat_lng_reads_the_ewkt_form():
    """The in-session form: what `build_point_wkt` wrote, before any round trip."""
    assert point_lat_lng(build_point_wkt(LAT, LNG)) == (LAT, LNG)


def test_point_lat_lng_reads_the_wkb_form():
    """The loaded form. This hex is a real value read back from Postgres — see
    the round-trip test above for the same thing through the API."""
    element = WKBElement(bytes.fromhex("0101000000e78c28ed0d6653405396218e75f12940"), srid=4326)

    lat, lng = point_lat_lng(element)

    assert (round(lat, 4), round(lng, 4)) == (LAT, LNG)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not a geometry at all",
        "SRID=4326;LINESTRING(1 2, 3 4)",  # right shape, wrong geometry
        "SRID=4326;POINT(east north)",  # unparseable ordinates
    ],
)
def test_point_lat_lng_degrades_to_none(value):
    """Coordinates enhance the address text; they never fail the response.

    Anything unreadable answers "no coordinates", so a booking whose geometry is
    missing or unexpected still tells the provider where to go in words.
    """
    assert point_lat_lng(value) == (None, None)


def test_point_lat_lng_rejects_a_truncated_body():
    """A point header with no coordinates behind it must not read past the end."""
    assert point_lat_lng(WKBElement(bytes.fromhex("010100000000"), srid=4326)) == (None, None)
