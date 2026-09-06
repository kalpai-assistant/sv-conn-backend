"""
Address soft delete and copy-on-write edit (SVC-277).

Two Profile controls could not be wired safely before this.
`bookings.consumer_address_id` is a foreign key with no ON DELETE rule, so
removing any address a booking has ever used raises — and editing had the
opposite problem, no constraint at all, so an update silently rewrote where a
completed job happened.

The fix creates one rule that is easy to get wrong in a way nothing reports:
**the address book hides soft-deleted rows; anything resolving a booking's
address must not.** Filter in the wrong place and the geo-fence stops finding
providers, which surfaces to a customer as "no partners found nearby" with
nothing logged anywhere. `test_geofence_still_matches_after_the_address_is_deleted`
is the test that makes that loud.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.address import Address
from app.models.booking import Booking
from app.services.booking_service import BookingService
from tests.factories.user_factory import UserFactory

PUNE = (18.5204, 73.8567)


async def _address(db, user_id, *, label="home", full="Somewhere in Pune", default=False):
    from app.utils.geo import build_point_wkt

    addr = Address(
        user_id=user_id,
        label=label,
        full_address=full,
        location=build_point_wkt(*PUNE),
        is_default=default,
    )
    db.add(addr)
    await db.flush()
    return addr


# ---------------------------------------------------------------------------
# Delete — hidden, not removed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_address_leaves_the_address_book(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id)

    await client.delete(
        f"/api/v1/users/me/addresses/{addr.id}",
        headers=auth_headers(str(user.id), "consumer"),
    )

    listed = await client.get(
        "/api/v1/users/me/addresses", headers=auth_headers(str(user.id), "consumer")
    )
    assert [a["id"] for a in listed.json()] == []


@pytest.mark.asyncio
async def test_the_row_survives_so_the_foreign_key_holds(client, auth_headers, db_session):
    """The point of soft delete: a booking may still point at this row."""
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id)

    await client.delete(
        f"/api/v1/users/me/addresses/{addr.id}",
        headers=auth_headers(str(user.id), "consumer"),
    )

    await db_session.refresh(addr)
    assert addr.deleted_at is not None


@pytest.mark.asyncio
async def test_a_deleted_address_cannot_stay_default(client, auth_headers, db_session):
    """Otherwise the default lookup prefills a booking with a removed address.

    Uses the *only* address, deliberately. With a survivor present the
    SVC-242 promotion runs `_clear_default`, which would unset the flag as a
    side effect and hide a missing `is_default = False` in the delete itself.
    Alone, nothing else can clear it — so this asserts the delete does.
    """
    user = await UserFactory.create(db_session, role="consumer")
    only = await _address(db_session, user.id, full="Old flat", default=True)

    await client.delete(
        f"/api/v1/users/me/addresses/{only.id}",
        headers=auth_headers(str(user.id), "consumer"),
    )

    await db_session.refresh(only)
    assert only.is_default is False


@pytest.mark.asyncio
async def test_deleting_the_default_promotes_a_live_address(client, auth_headers, db_session):
    """SVC-242: the next booking must still auto-fill an address."""
    user = await UserFactory.create(db_session, role="consumer")
    gone = await _address(db_session, user.id, full="Old flat", default=True)
    kept = await _address(db_session, user.id, full="New flat")

    await client.delete(
        f"/api/v1/users/me/addresses/{gone.id}",
        headers=auth_headers(str(user.id), "consumer"),
    )

    await db_session.refresh(kept)
    assert kept.is_default is True


@pytest.mark.asyncio
async def test_deleting_twice_is_a_no_op(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id)
    headers = auth_headers(str(user.id), "consumer")

    first = await client.delete(f"/api/v1/users/me/addresses/{addr.id}", headers=headers)
    second = await client.delete(f"/api/v1/users/me/addresses/{addr.id}", headers=headers)

    assert first.status_code == 204
    # Already hidden — nothing further to do, and certainly not an error.
    assert second.status_code == 204


# ---------------------------------------------------------------------------
# The silent failure this design risks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bookings_address_still_resolves_after_it_is_deleted(
    client, auth_headers, db_session
):
    """The load-bearing test for the filtering rule.

    Booking paths resolve an address by `consumer_address_id` and must NOT
    exclude soft-deleted rows. Adding that filter looks like a correctness fix
    to anyone auditing for soft delete — and it is exactly wrong: the lookup
    returns nothing, the broadcast bails, and the booking dies as "no partners
    found nearby". No error, no log line, no Sentry event. Only a customer
    whose booking quietly expired.

    Goes through `attach_addresses`, the real path every BookingResponse uses,
    rather than querying the address directly — a test that holds its own
    Address object proves only that PostGIS works.
    """
    from tests.factories.booking_factory import BookingFactory

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    addr = await _address(db_session, consumer.id, full="Flat 302, Karve Nagar")

    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
    )

    # Delete it exactly as the user would from Profile.
    await client.delete(
        f"/api/v1/users/me/addresses/{addr.id}",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    # The booking must still be able to find where the job is going.
    fresh = await db_session.get(Booking, booking.id)
    await BookingService(db_session).attach_addresses([fresh])

    assert fresh.consumer_address is not None, (
        "a booking could no longer resolve its address — the soft-delete filter "
        "has been applied to a booking lookup, which silently breaks dispatch"
    )
    assert fresh.consumer_address.full_address == "Flat 302, Karve Nagar"
    assert fresh.consumer_address.location is not None, "geo-fence would find no providers"


# ---------------------------------------------------------------------------
# Edit — copy-on-write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_returns_the_updated_address(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id, full="Flat 302, Karve Nagar")

    response = await client.patch(
        f"/api/v1/users/me/addresses/{addr.id}",
        json={"full_address": "Flat 701, Baner Road"},
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["full_address"] == "Flat 701, Baner Road"


@pytest.mark.asyncio
async def test_edit_does_not_rewrite_where_a_past_job_happened(client, auth_headers, db_session):
    """The reason edit is copy-on-write rather than an in-place update.

    An in-place update would change a completed booking's record to an address
    the job never happened at — and, mid-job, would redirect a provider already
    en route. Neither errors; both are simply wrong afterwards.
    """
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id, full="Flat 302, Karve Nagar")
    original_id = addr.id

    await client.patch(
        f"/api/v1/users/me/addresses/{original_id}",
        json={"full_address": "Flat 701, Baner Road"},
        headers=auth_headers(str(user.id), "consumer"),
    )

    # The row a booking would be pointing at still says where the job went.
    old = await db_session.get(Address, original_id)
    assert old is not None
    assert old.full_address == "Flat 302, Karve Nagar"


@pytest.mark.asyncio
async def test_edit_leaves_exactly_one_visible_address(client, auth_headers, db_session):
    """Copy-on-write must not leave the user looking at both versions."""
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id, full="Flat 302, Karve Nagar")
    headers = auth_headers(str(user.id), "consumer")

    await client.patch(
        f"/api/v1/users/me/addresses/{addr.id}",
        json={"full_address": "Flat 701, Baner Road"},
        headers=headers,
    )

    listed = (await client.get("/api/v1/users/me/addresses", headers=headers)).json()
    assert len(listed) == 1
    assert listed[0]["full_address"] == "Flat 701, Baner Road"


@pytest.mark.asyncio
async def test_edit_carries_the_default_flag_across(client, auth_headers, db_session):
    """Editing the default must not silently leave the user without one."""
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id, default=True)
    headers = auth_headers(str(user.id), "consumer")

    await client.patch(
        f"/api/v1/users/me/addresses/{addr.id}",
        json={"label": "work"},
        headers=headers,
    )

    listed = (await client.get("/api/v1/users/me/addresses", headers=headers)).json()
    assert len(listed) == 1
    assert listed[0]["is_default"] is True


@pytest.mark.asyncio
async def test_unset_fields_keep_their_values(client, auth_headers, db_session):
    """It is a PATCH: omitting a field means "leave it", not "clear it"."""
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id, label="home", full="Flat 302")
    headers = auth_headers(str(user.id), "consumer")

    response = await client.patch(
        f"/api/v1/users/me/addresses/{addr.id}",
        json={"label": "work"},
        headers=headers,
    )

    assert response.json()["full_address"] == "Flat 302"
    assert response.json()["label"] == "work"


@pytest.mark.asyncio
async def test_cannot_edit_someone_elses_address(client, auth_headers, db_session):
    owner = await UserFactory.create(db_session, role="consumer")
    stranger = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, owner.id, full="Not yours")

    response = await client.patch(
        f"/api/v1/users/me/addresses/{addr.id}",
        json={"full_address": "Mine now"},
        headers=auth_headers(str(stranger.id), "consumer"),
    )

    assert response.status_code == 404
    await db_session.refresh(addr)
    assert addr.full_address == "Not yours"


@pytest.mark.asyncio
async def test_cannot_edit_an_already_deleted_address(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id)
    headers = auth_headers(str(user.id), "consumer")

    await client.delete(f"/api/v1/users/me/addresses/{addr.id}", headers=headers)
    response = await client.patch(
        f"/api/v1/users/me/addresses/{addr.id}",
        json={"label": "work"},
        headers=headers,
    )

    # Hidden rows are gone as far as the address book is concerned.
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_editing_a_missing_address_is_404(client, auth_headers, db_session):
    user = await UserFactory.create(db_session, role="consumer")

    response = await client.patch(
        f"/api/v1/users/me/addresses/{uuid.uuid4()}",
        json={"label": "work"},
        headers=auth_headers(str(user.id), "consumer"),
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_booking_count_is_untouched_by_either_operation(client, auth_headers, db_session):
    """Neither delete nor edit may cascade into bookings."""
    user = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, user.id)
    headers = auth_headers(str(user.id), "consumer")

    before = (await db_session.execute(select(Booking))).scalars().all()

    await client.patch(
        f"/api/v1/users/me/addresses/{addr.id}", json={"label": "work"}, headers=headers
    )
    await client.delete(f"/api/v1/users/me/addresses/{addr.id}", headers=headers)

    after = (await db_session.execute(select(Booking))).scalars().all()
    assert len(after) == len(before)
