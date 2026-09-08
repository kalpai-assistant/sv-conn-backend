"""
The provider's service-area centre comes back out again (SVC-279).

`PATCH /providers/me` has always accepted `service_area_lat` / `service_area_lng`,
but no response schema returned them — the centre was write-only. A provider
could overwrite their service area and never see what it was.

That makes three of SVC-279's acceptance criteria unbuildable: view the location
on a map (nothing to centre on), edit it (nothing to pre-fill), and confirm it
persists across logout (nothing to read back).

The interesting failure is not "the field is missing" — that is loud. It is the
field coming back **wrong**: a geography column hands back two different Python
types depending on whether the row has round-tripped through the database, and
mixing them up is what shipped a broken address edit in #151.
"""

import pytest

from tests.factories.user_factory import UserFactory

PUNE_LAT, PUNE_LNG = 18.5204, 73.8567


async def _provider_with_area(db, *, lat=None, lng=None, radius=10):
    """A provider profile, optionally with a service-area centre set."""
    from app.models.provider_profile import KYCStatus, ProviderProfile
    from app.utils.geo import build_point_wkt

    user = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(
        user_id=user.id,
        kyc_status=KYCStatus.VERIFIED,
        service_area_center=build_point_wkt(lat, lng) if lat is not None else None,
        service_area_radius_km=radius,
    )
    db.add(profile)
    await db.flush()
    return user, profile


@pytest.mark.asyncio
async def test_the_centre_is_returned(client, auth_headers, db_session):
    """The gap itself: lat/lng went in and never came out."""
    user, _ = await _provider_with_area(db_session, lat=PUNE_LAT, lng=PUNE_LNG)

    response = await client.get(
        "/api/v1/providers/me", headers=auth_headers(str(user.id), "provider")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["service_area_lat"] == pytest.approx(PUNE_LAT, abs=1e-4)
    assert body["service_area_lng"] == pytest.approx(PUNE_LNG, abs=1e-4)


@pytest.mark.asyncio
async def test_lat_and_lng_are_not_swapped(client, auth_headers, db_session):
    """Worth its own test: a swap survives every round-trip and looks plausible.

    WKB stores a point as (X, Y) = (lng, lat) — the reverse of how every caller
    here writes and reads it. A transposed pair still decodes, still renders a
    pin, and puts a Pune provider in the Arabian Sea. Chosen coordinates differ
    enough that a swap cannot pass.
    """
    user, _ = await _provider_with_area(db_session, lat=PUNE_LAT, lng=PUNE_LNG)

    body = (
        await client.get("/api/v1/providers/me", headers=auth_headers(str(user.id), "provider"))
    ).json()

    # Pune is ~18.5N, ~73.9E. Swapped, the latitude would be out of range for
    # India entirely.
    assert 8 < body["service_area_lat"] < 37, "latitude is not a plausible Indian latitude"
    assert 68 < body["service_area_lng"] < 98, "longitude is not a plausible Indian longitude"


@pytest.mark.asyncio
async def test_a_provider_who_never_set_one_gets_null(client, auth_headers, db_session):
    """Not an error state.

    Anyone who onboarded before SVC-257 has no centre. The Location screen needs
    to tell "never set" apart from "set to 0,0" — the latter is a real place in
    the Gulf of Guinea, and defaulting to it would put every such provider there.
    """
    user, _ = await _provider_with_area(db_session, lat=None, lng=None)

    body = (
        await client.get("/api/v1/providers/me", headers=auth_headers(str(user.id), "provider"))
    ).json()

    assert body["service_area_lat"] is None
    assert body["service_area_lng"] is None


@pytest.mark.asyncio
async def test_a_freshly_patched_centre_reads_back_in_the_same_response(
    client, auth_headers, db_session
):
    """The #151 trap, in its exact shape.

    A geography column hands back a `WKBElement` once the row has been read from
    the database, but the raw string that was assigned if it has not round-tripped
    yet — and PATCH returns the object it just wrote. If the decode only handles
    one of those forms, this endpoint breaks precisely when it matters: right
    after a provider saves.
    """
    user, _ = await _provider_with_area(db_session, lat=None, lng=None)

    response = await client.patch(
        "/api/v1/providers/me",
        json={
            "service_area_lat": PUNE_LAT,
            "service_area_lng": PUNE_LNG,
            "service_area_radius_km": 15,
        },
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["service_area_lat"] == pytest.approx(PUNE_LAT, abs=1e-4)
    assert body["service_area_lng"] == pytest.approx(PUNE_LNG, abs=1e-4)
    assert body["service_area_radius_km"] == 15


@pytest.mark.asyncio
async def test_the_centre_survives_a_reload(client, auth_headers, db_session):
    """AC6: persists after logout/login.

    A second request is a new session and a genuine database read, so this
    exercises the `WKBElement` path rather than the just-assigned one.
    """
    user, _ = await _provider_with_area(db_session, lat=None, lng=None)
    headers = auth_headers(str(user.id), "provider")

    await client.patch(
        "/api/v1/providers/me",
        json={"service_area_lat": PUNE_LAT, "service_area_lng": PUNE_LNG},
        headers=headers,
    )
    body = (await client.get("/api/v1/providers/me", headers=headers)).json()

    assert body["service_area_lat"] == pytest.approx(PUNE_LAT, abs=1e-4)
    assert body["service_area_lng"] == pytest.approx(PUNE_LNG, abs=1e-4)
