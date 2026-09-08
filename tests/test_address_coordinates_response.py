"""
Saved addresses hand back the pin that was dropped on them (SVC-281).

`CreateAddressRequest` has always accepted `lat`/`lng` and `addresses.location`
has always stored them, but no response returned them. Write-only — the same
shape the provider service area had before #156.

The cost was visible on the address map. With no coordinates to open on, it fell
back to a hardcoded `[12.9716, 77.5946]` for everyone: a consumer in Pune with a
saved Pune address got a map of Bangalore, ~840km away, and had to find a small
locate button to escape it.

Two failures here would be silent rather than loud, and they are what these
tests are for: a null pin read as 0,0 (a real place in the Gulf of Guinea), and
lat/lng transposed (still decodes, still renders, wrong hemisphere-ish).
"""

import pytest

from tests.factories.user_factory import UserFactory

PUNE_LAT, PUNE_LNG = 18.5204, 73.8567


async def _address(db, user_id, *, lat=None, lng=None, label="home"):
    from app.models.address import Address
    from app.utils.geo import build_point_wkt

    addr = Address(
        user_id=user_id,
        label=label,
        full_address="Lane Number 7, Karve Nagar, Pune",
        location=build_point_wkt(lat, lng) if lat is not None else None,
    )
    db.add(addr)
    await db.flush()
    return addr


@pytest.mark.asyncio
async def test_the_pin_comes_back(client, auth_headers, db_session):
    """The gap itself: coordinates went in and never came out."""
    user = await UserFactory.create(db_session, role="consumer")
    await _address(db_session, user.id, lat=PUNE_LAT, lng=PUNE_LNG)

    response = await client.get(
        "/api/v1/users/me/addresses", headers=auth_headers(str(user.id), "consumer")
    )

    assert response.status_code == 200
    [body] = response.json()
    assert body["lat"] == pytest.approx(PUNE_LAT, abs=1e-4)
    assert body["lng"] == pytest.approx(PUNE_LNG, abs=1e-4)


@pytest.mark.asyncio
async def test_lat_and_lng_are_not_transposed(client, auth_headers, db_session):
    """A swap survives every round-trip and looks entirely plausible.

    WKB stores a point as (X, Y) = (lng, lat), the reverse of how callers here
    read it. Transposed, a Pune address still decodes and still renders a pin —
    in the Arabian Sea. Coordinates chosen so a swap cannot pass.
    """
    user = await UserFactory.create(db_session, role="consumer")
    await _address(db_session, user.id, lat=PUNE_LAT, lng=PUNE_LNG)

    [body] = (
        await client.get(
            "/api/v1/users/me/addresses", headers=auth_headers(str(user.id), "consumer")
        )
    ).json()

    assert 8 < body["lat"] < 37, "not a plausible Indian latitude — lat/lng are swapped"
    assert 68 < body["lng"] < 98, "not a plausible Indian longitude — lat/lng are swapped"


@pytest.mark.asyncio
async def test_an_address_with_no_pin_reports_null_not_zero(client, auth_headers, db_session):
    """`location` is optional on create, and older rows predate it.

    Null must stay null. Coercing to 0,0 would open the map in the Gulf of
    Guinea and, worse, look like a real answer — the caller cannot tell it from
    a genuine pin.
    """
    user = await UserFactory.create(db_session, role="consumer")
    await _address(db_session, user.id, lat=None, lng=None)

    [body] = (
        await client.get(
            "/api/v1/users/me/addresses", headers=auth_headers(str(user.id), "consumer")
        )
    ).json()

    assert body["lat"] is None
    assert body["lng"] is None


@pytest.mark.asyncio
async def test_a_freshly_created_address_returns_its_pin(client, auth_headers, db_session):
    """POST returns the object it just wrote — the #151 trap.

    A geography column hands back a `WKBElement` once the row has round-tripped
    through the database, but the raw string that was assigned if it has not.
    Handling only one form breaks the endpoint exactly when a user saves, which
    is the moment it matters.
    """
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.post(
        "/api/v1/users/me/addresses",
        json={
            "label": "home",
            "full_address": "Lane Number 7, Karve Nagar, Pune",
            "lat": PUNE_LAT,
            "lng": PUNE_LNG,
        },
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["lat"] == pytest.approx(PUNE_LAT, abs=1e-4)
    assert body["lng"] == pytest.approx(PUNE_LNG, abs=1e-4)


@pytest.mark.asyncio
async def test_the_existing_fields_are_untouched(client, auth_headers, db_session):
    """Additive only.

    Every current caller destructures the fields it wants and ignores the rest,
    so widening the response is safe — but only while the existing four keep
    their names and meanings.
    """
    user = await UserFactory.create(db_session, role="consumer")
    await _address(db_session, user.id, lat=PUNE_LAT, lng=PUNE_LNG, label="work")

    [body] = (
        await client.get(
            "/api/v1/users/me/addresses", headers=auth_headers(str(user.id), "consumer")
        )
    ).json()

    assert body["label"] == "work"
    assert body["full_address"] == "Lane Number 7, Karve Nagar, Pune"
    assert "id" in body
    assert "is_default" in body
