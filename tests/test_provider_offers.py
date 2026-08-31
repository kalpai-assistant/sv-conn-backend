"""SVC-254 — the polling fallback for offers, and declining one.

The gap this closes: dispatch was push-only. ``send_booking_notification`` found
eligible providers, fired an FCM push at each, and kept no record — so a push
that failed to deliver (stale token, denied permission, iOS Safari, incognito)
left the provider silently deaf to a paid job, with nothing anywhere recording
that they had been asked.

``booking_offers`` persists the invitation, ``GET /providers/me/offers`` serves
it, and ``PATCH /bookings/{id}/decline`` lets a provider stop being asked.

Two behaviours here are easy to break and worth stating plainly:

* **The top-up.** Rows are written when the booking dispatches, so a provider
  who came online mid-window has none. The read inserts them. Without it, Option
  1's persisted-offer model would be strictly worse than computing eligibility
  at read time for exactly this case — and the push for that booking has already
  fired, so polling is the only way they could ever learn of it.
* **The losers keep their rows.** When someone accepts, only the winner's row is
  stamped. Everyone else's stays unanswered and simply stops being served,
  because the read joins to the booking and filters on REQUESTED + unclaimed.
  That keeps a bulk UPDATE off the accept path, where money moves.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.booking_offer import BookingOffer, OfferResponse
from app.models.category import Category
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

ADDRESS_POINT = "SRID=4326;POINT(77.6408 12.9784)"
NEAR_POINT = "SRID=4326;POINT(77.6200 12.9700)"  # ~2.4 km away
FAR_POINT = "SRID=4326;POINT(78.2000 12.9700)"  # ~60 km away
FULL_ADDRESS = "12 MG Road, Indiranagar"
SERVICE_NAME = "Deep Cleaning"
SERVICE_AMOUNT = 1499.0


async def _service(db):
    slug = f"cleaning-{uuid.uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db.add(category)
    await db.flush()
    service = Service(
        category_id=category.id,
        name=SERVICE_NAME,
        base_price=SERVICE_AMOUNT,
        duration_minutes=90,
        is_active=True,
    )
    db.add(service)
    await db.flush()
    return service


async def _provider(
    db,
    service,
    *,
    point: str = NEAR_POINT,
    online: bool = True,
    kyc: KYCStatus = KYCStatus.VERIFIED,
    subscribed: bool = True,
    radius_km: int = 10,
):
    """A provider positioned relative to the consumer address.

    Each knob is one clause of the eligibility rule, so a refusal test flips
    exactly one and leaves the rest valid.
    """
    user = await UserFactory.create(db, role="provider")
    db.add(
        ProviderProfile(
            user_id=user.id,
            kyc_status=kyc,
            is_online=online,
            service_area_center=point,
            service_area_radius_km=radius_km,
        )
    )
    if subscribed:
        db.add(ProviderServiceModel(provider_id=user.id, service_id=service.id))
    await db.flush()
    return user


async def _open_booking(db, service, *, age_seconds: int = 0, **kwargs):
    """A REQUESTED, unassigned booking with a geolocated address."""
    consumer = await UserFactory.create(db, role="consumer")
    address = Address(
        user_id=consumer.id,
        label="home",
        full_address=FULL_ADDRESS,
        location=ADDRESS_POINT,
    )
    db.add(address)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=None,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.REQUESTED,
        service_amount=SERVICE_AMOUNT,
        **kwargs,
    )
    if age_seconds:
        booking.created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
        await db.flush()
    return booking


async def _offer(db, booking, provider, response=None):
    """The row the dispatch task would have written."""
    row = BookingOffer(
        id=uuid.uuid4(),
        booking_id=booking.id,
        provider_id=provider.id,
        response=response,
    )
    db.add(row)
    await db.flush()
    return row


def _list(client, provider, auth_headers):
    return client.get(
        "/api/v1/providers/me/offers",
        headers=auth_headers(str(provider.id), "provider"),
    )


def _decline(client, booking_id, provider, auth_headers):
    return client.patch(
        f"/api/v1/bookings/{booking_id}/decline",
        headers=auth_headers(str(provider.id), "provider"),
    )


# --- the happy path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_offered_provider_sees_the_offer(client, db_session, auth_headers):
    """The whole point: the row survives a push that never arrived."""
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    await _offer(db_session, booking, provider)

    resp = await _list(client, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["booking_id"] == str(booking.id)
    assert body[0]["service_name"] == SERVICE_NAME
    assert body[0]["full_address"] == FULL_ADDRESS


@pytest.mark.asyncio
async def test_polled_offer_matches_the_pushed_card(client, db_session, auth_headers):
    """Both delivery paths must render identically — same schema, one builder.

    ``doorstep_otp`` is the field that matters: it gates en_route → in_progress
    and must reach the provider from the consumer at the door, never from an API
    served to everyone nearby.
    """
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    booking = await _open_booking(db_session, service, doorstep_otp="9182")
    await _offer(db_session, booking, provider)

    resp = await _list(client, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    assert set(resp.json()[0]) == {
        "booking_id",
        "service_name",
        "service_amount",
        "full_address",
        "distance_km",
        "expires_at",
        "seconds_remaining",
    }


# --- the top-up ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_who_came_online_late_is_topped_up(client, db_session, auth_headers):
    """No row at dispatch, eligible now — the read has to write one.

    This is the case Option 1 would otherwise lose to read-time computation: the
    booking's push has already fired and will not be re-sent, so if the poll
    doesn't add them they never hear about it at all.
    """
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    # Deliberately no _offer() — they weren't around when it dispatched.

    resp = await _list(client, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    assert [o["booking_id"] for o in resp.json()] == [str(booking.id)]

    row = (
        await db_session.execute(
            select(BookingOffer).where(
                BookingOffer.booking_id == booking.id,
                BookingOffer.provider_id == provider.id,
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "the poll should have persisted the top-up"
    assert row.response is None


@pytest.mark.asyncio
async def test_top_up_is_not_offered_to_the_ineligible(client, db_session, auth_headers):
    """The top-up re-applies the full eligibility rule, not just 'has no row'."""
    service = await _service(db_session)
    offline = await _provider(db_session, service, online=False)
    unverified = await _provider(db_session, service, kyc=KYCStatus.SUBMITTED)
    far = await _provider(db_session, service, point=FAR_POINT)
    unsubscribed = await _provider(db_session, service, subscribed=False)
    await _open_booking(db_session, service)

    for provider in (offline, unverified, far, unsubscribed):
        resp = await _list(client, provider, auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == [], f"{provider.id} should not have been topped up"


# --- what drops out -----------------------------------------------------------


@pytest.mark.asyncio
async def test_declined_offer_stops_being_served(client, db_session, auth_headers):
    """Without this the card would return every 15s — worse than never showing."""
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    await _offer(db_session, booking, provider, response=OfferResponse.DECLINED)

    resp = await _list(client, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_claimed_booking_drops_out_for_everyone_else(client, db_session, auth_headers):
    """The loser's row is never written to — the booking filter does the work."""
    service = await _service(db_session)
    winner = await _provider(db_session, service)
    loser = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    await _offer(db_session, booking, winner)
    loser_row = await _offer(db_session, booking, loser)

    booking.provider_id = winner.id
    booking.status = BookingStatus.ACCEPTED
    await db_session.flush()

    resp = await _list(client, loser, auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    await db_session.refresh(loser_row)
    assert loser_row.response is None, "the loser was offered it and never answered"


@pytest.mark.asyncio
async def test_expired_window_drops_out(client, db_session, auth_headers):
    """`expire_stale_bookings` sweeps every 30s, so a booking can sit past its
    deadline still REQUESTED. The read must not serve it in that gap."""
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    timeout = get_settings().BOOKING_REQUEST_TIMEOUT_SECONDS
    booking = await _open_booking(db_session, service, age_seconds=timeout + 60)
    await _offer(db_session, booking, provider)

    resp = await _list(client, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_provider_who_went_offline_is_served_nothing(client, db_session, auth_headers):
    """Offered while online, offline now — the row is not enough.

    ``accept_booking`` re-checks the full eligibility rule, ``is_online``
    included, so serving this card would hand them a job that 403s the instant
    they tap it.
    """
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    await _offer(db_session, booking, provider)

    profile = (
        await db_session.execute(
            select(ProviderProfile).where(ProviderProfile.user_id == provider.id)
        )
    ).scalar_one()
    profile.is_online = False
    await db_session.flush()

    resp = await _list(client, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_missing_service_area_centre_does_not_500(client, db_session, auth_headers):
    """A profile with no centre has no point to measure a distance from.

    Regression: the distance came back NULL and ``round()`` raised, which took
    down the whole endpoint — so one bad profile meant the provider saw none of
    their offers rather than one fewer.
    """
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    await _offer(db_session, booking, provider)

    profile = (
        await db_session.execute(
            select(ProviderProfile).where(ProviderProfile.user_id == provider.id)
        )
    ).scalar_one()
    profile.service_area_center = None
    await db_session.flush()

    resp = await _list(client, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# --- ordering -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_soonest_expiring_first(client, db_session, auth_headers):
    """The card shows one job at a time, so order decides which one that is."""
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    older = await _open_booking(db_session, service, age_seconds=60)
    newer = await _open_booking(db_session, service, age_seconds=5)
    await _offer(db_session, older, provider)
    await _offer(db_session, newer, provider)

    resp = await _list(client, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    assert [o["booking_id"] for o in resp.json()] == [str(older.id), str(newer.id)]


# --- decline ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decline_records_the_refusal(client, db_session, auth_headers):
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    row = await _offer(db_session, booking, provider)

    resp = await _decline(client, booking.id, provider, auth_headers)

    assert resp.status_code == 204, resp.text
    await db_session.refresh(row)
    assert row.response is OfferResponse.DECLINED
    assert row.responded_at is not None


@pytest.mark.asyncio
async def test_decline_is_idempotent(client, db_session, auth_headers):
    """A double tap is the common cause; the intent is satisfied either way."""
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    await _offer(db_session, booking, provider)

    assert (await _decline(client, booking.id, provider, auth_headers)).status_code == 204
    assert (await _decline(client, booking.id, provider, auth_headers)).status_code == 204


@pytest.mark.asyncio
async def test_decline_refuses_a_non_recipient(client, db_session, auth_headers):
    """403, the same answer /offer gives — so decline can't be used to probe
    which bookings exist."""
    service = await _service(db_session)
    stranger = await _provider(db_session, service, point=FAR_POINT)
    booking = await _open_booking(db_session, service)

    resp = await _decline(client, booking.id, stranger, auth_headers)

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_declining_does_not_withdraw_the_booking_from_others(
    client, db_session, auth_headers
):
    """One provider saying no must not take the job off anyone else's list."""
    service = await _service(db_session)
    decliner = await _provider(db_session, service)
    other = await _provider(db_session, service)
    booking = await _open_booking(db_session, service)
    await _offer(db_session, booking, decliner)
    await _offer(db_session, booking, other)

    assert (await _decline(client, booking.id, decliner, auth_headers)).status_code == 204

    resp = await _list(client, other, auth_headers)
    assert resp.status_code == 200, resp.text
    assert [o["booking_id"] for o in resp.json()] == [str(booking.id)]
