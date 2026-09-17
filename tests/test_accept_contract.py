"""SVC-154 + SVC-167 + SVC-168 — the accept contract.

Accept is the "fastest finger first" mechanic, so most calls to it *fail* by
design: one provider wins and every other recipient of the broadcast loses.

The contract these tests pin, which the Incoming Request Card branches on by
status code alone — never by message:

    403  you weren't offered this booking (SVC-168 eligibility check — wrong
         role, no ProviderProfile, out of radius, KYC unverified, offline,
         not subscribed to the service)
    404  no such booking
    410  the offer is over — cancelled, expired, OR claimed by another
         provider (SVC-167 unified these three under one code)
    200  yours already — an idempotent retry, and crucially *no second push*

The load-bearing one is ``test_reaccept_does_not_notify_the_consumer_again``.
Returning 200 for a retry is only correct if the consumer is not told twice; the
naive version of this fix fires "Booking Confirmed" again on every retry, which
is worse than the error it replaces.
"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.services.booking_service import BookingService
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

# Consumer's address: Indiranagar, Bengaluru. Provider fixtures sit right on
# top so radius always includes them. Same shape as test_booking_offer.py so
# a reader can compare the two files' setup at a glance.
ADDRESS_POINT = "SRID=4326;POINT(77.6408 12.9784)"
NEAR_POINT = "SRID=4326;POINT(77.6200 12.9700)"  # ~2.4 km away


async def _service(db):
    slug = f"cleaning-{uuid.uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db.add(category)
    await db.flush()
    service = Service(
        category_id=category.id,
        name="Deep Cleaning",
        base_price=1499.0,
        duration_minutes=90,
        is_active=True,
    )
    db.add(service)
    await db.flush()
    return service


async def _booking(db, *, service=None, provider_id=None, status=BookingStatus.REQUESTED):
    consumer = await UserFactory.create(db, role="consumer")
    service = service or await _service(db)
    address = Address(
        user_id=consumer.id,
        label="home",
        full_address="12 MG Road",
        # Location is required for the SVC-168 eligibility check to have
        # anything to compare against. In production the geocoder fills this
        # in on address create; tests set it directly.
        location=ADDRESS_POINT,
    )
    db.add(address)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider_id,
        service_id=service.id,
        address_id=address.id,
        status=status,
    )
    return consumer, booking


async def _eligible_provider(
    db,
    service,
    *,
    online: bool = True,
    kyc: KYCStatus = KYCStatus.VERIFIED,
    subscribed: bool = True,
    point: str = NEAR_POINT,
    radius_km: int = 10,
    has_token: bool = True,
):
    """A provider set up to pass the SVC-168 eligibility check for `service`.

    Every knob here is one clause of the eligibility rule — flip one and the
    provider becomes ineligible in that specific way. Tests use this to prove
    each ineligibility path returns 403. ``has_token`` covers SVC-276:
    a provider still marked online but without an FCM token.
    """
    user = await UserFactory.create(
        db, role="provider", fcm_token=(f"fcm-{uuid.uuid4().hex[:16]}" if has_token else None)
    )
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


def _accept(client, booking_id, user, auth_headers, role: str = "provider"):
    return client.patch(
        f"/api/v1/bookings/{booking_id}/accept",
        headers=auth_headers(str(user.id), role),
    )


# ---------------------------------------------------------------------------
# 410 — the offer is over (claimed by someone else, cancelled, or expired)
# SVC-167: all three under one code the card can key on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_provider_gets_410_not_403(client, db_session, auth_headers):
    """SVC-167: the lost race used to be 403; unified with cancelled/expired
    under 410. The card was already treating 403/410 alike on accept, so no
    frontend change was needed — but two codes for one outcome was drift the
    next reader would misread as different failures."""
    service = await _service(db_session)
    winner = await _eligible_provider(db_session, service)
    loser = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    assert (await _accept(client, booking.id, winner, auth_headers)).status_code == 200

    resp = await _accept(client, booking.id, loser, auth_headers)

    assert resp.status_code == 410, resp.text
    body = resp.json()
    assert body["status_code"] == 410
    # Message is generic ("no longer available") — safe to show, no winner id leaked.
    assert str(winner.id) not in resp.text
    assert "accepted" not in body["message"]


@pytest.mark.asyncio
async def test_losing_does_not_touch_the_winner_or_the_booking(client, db_session, auth_headers):
    service = await _service(db_session)
    winner = await _eligible_provider(db_session, service)
    loser = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    await _accept(client, booking.id, winner, auth_headers)
    await _accept(client, booking.id, loser, auth_headers)

    await db_session.refresh(booking)
    assert booking.provider_id == winner.id
    assert booking.status is BookingStatus.ACCEPTED

    # And the loser has not acquired it in their own list.
    listing = await client.get("/api/v1/bookings/", headers=auth_headers(str(loser.id), "provider"))
    assert listing.status_code == 200
    assert str(booking.id) not in listing.text


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", [BookingStatus.CANCELLED, BookingStatus.EXPIRED])
async def test_cancelled_or_expired_returns_410(client, db_session, auth_headers, gone):
    """Matches GET /bookings/{id}/offer, which already returns 410 for these."""
    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service, status=gone)

    resp = await _accept(client, booking.id, provider, auth_headers)

    assert resp.status_code == 410, resp.text
    assert resp.json()["status_code"] == 410


@pytest.mark.asyncio
async def test_cancelled_after_you_claimed_it_is_410_not_a_stale_200(
    client, db_session, auth_headers
):
    """Ordering guard: the gone-check runs before the idempotency check.

    A provider who claimed a booking that was later cancelled must not get a
    cheerful 200 back — they would navigate into a job that no longer exists.
    """
    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service)
    _, booking = await _booking(
        db_session, service=service, provider_id=provider.id, status=BookingStatus.CANCELLED
    )

    resp = await _accept(client, booking.id, provider, auth_headers)

    assert resp.status_code == 410, resp.text


# ---------------------------------------------------------------------------
# 403 — SVC-168 eligibility check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_cannot_accept_any_booking(client, db_session, auth_headers):
    """The security bug SVC-168 was written for. Before the role gate a
    consumer with the booking id (their own or otherwise) could hit /accept
    and become the assigned provider — then run the booking through en-route
    → in_progress → completed and appear as owed the 80% payout on
    /admin/providers/pending-settlements. A job that never happened.

    The consumer's own booking id is the sharpest attack: they always know it,
    no leak / guessing needed.
    """
    service = await _service(db_session)
    consumer, booking = await _booking(db_session, service=service)

    resp = await _accept(client, booking.id, consumer, auth_headers, role="consumer")

    assert resp.status_code == 403, resp.text
    # Booking must still be REQUESTED with no assigned provider.
    await db_session.refresh(booking)
    assert booking.provider_id is None
    assert booking.status is BookingStatus.REQUESTED


@pytest.mark.asyncio
async def test_admin_cannot_accept_a_booking(client, db_session, auth_headers):
    """Admin role is orthogonal to provider role. Even /admin can't self-assign."""
    admin = await UserFactory.create(db_session, role="admin")
    service = await _service(db_session)
    _, booking = await _booking(db_session, service=service)

    resp = await _accept(client, booking.id, admin, auth_headers, role="admin")

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_provider_with_no_profile_cannot_accept(client, db_session, auth_headers):
    """A user with role='provider' but no ProviderProfile row is not really a
    provider. Eligibility check refuses them — 403 not 200."""
    stranger = await UserFactory.create(db_session, role="provider")
    service = await _service(db_session)
    _, booking = await _booking(db_session, service=service)

    resp = await _accept(client, booking.id, stranger, auth_headers)

    assert resp.status_code == 403, resp.text
    assert "were not offered" in resp.json()["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_ineligible",
    [
        pytest.param({"online": False}, id="offline"),
        pytest.param({"kyc": KYCStatus.SUBMITTED}, id="kyc-unverified"),
        pytest.param({"subscribed": False}, id="not-subscribed-to-service"),
        pytest.param(
            {"point": "SRID=4326;POINT(78.2000 12.9700)", "radius_km": 5},
            id="out-of-radius",
        ),
        # SVC-276: unreachable — no FCM token, so the broadcast never
        # selected them and accept must refuse for the same reason.
        pytest.param({"has_token": False}, id="no-fcm-token"),
    ],
)
async def test_ineligible_provider_cannot_accept(client, db_session, auth_headers, make_ineligible):
    """SVC-168: eligibility drift protection. A provider eligible when the
    broadcast fired but who has since gone offline, lost KYC, unsubscribed
    from the service, (for a bookmarked id) never was in radius, or has no
    reachable push token — cannot accept. The set of accepters cannot drift
    from the offered set."""
    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service, **make_ineligible)
    _, booking = await _booking(db_session, service=service)

    resp = await _accept(client, booking.id, provider, auth_headers)

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 200 — idempotent retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaccepting_your_own_booking_is_idempotent(client, db_session, auth_headers):
    """A retry after a lost response must not tell the winner they lost."""
    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    first = await _accept(client, booking.id, provider, auth_headers)
    second = await _accept(client, booking.id, provider, auth_headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["provider_id"] == str(provider.id)
    assert second.json()["status"] == BookingStatus.ACCEPTED.value


@pytest.mark.asyncio
async def test_reaccept_does_not_notify_the_consumer_again(client, db_session, auth_headers):
    """The reason idempotency is not just `return booking`.

    The router notifies the consumer after a successful accept. If a retry counts
    as success, "Booking Confirmed" fires again every time the network drops a
    response — so the fix is only correct if the second call sends nothing.
    """
    from unittest.mock import patch

    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    with patch("app.routers.bookings.send_booking_notification") as push:
        await _accept(client, booking.id, provider, auth_headers)
        assert push.delay.call_count == 1, "the real accept should notify"

        await _accept(client, booking.id, provider, auth_headers)
        assert (
            push.delay.call_count == 1
        ), "the idempotent retry notified the consumer a second time"


@pytest.mark.asyncio
async def test_idempotent_retry_survives_provider_going_offline(client, db_session, auth_headers):
    """Edge case for SVC-168: the eligibility check has to SKIP when the caller
    already owns the booking. If they were eligible at accept-time and have
    since gone offline / lost KYC, their retry (after a lost response) must
    still succeed as an idempotent 200 — else the fix regresses the
    idempotency guarantee test_reaccept_does_not_notify guards."""
    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)

    first = await _accept(client, booking.id, provider, auth_headers)
    assert first.status_code == 200

    # Provider goes offline AFTER acceptance — mirrors what happens in prod
    # when a network blip resets the client's online-state. ProviderProfile's
    # PK is its own `id`, not `user_id` — provider.id is the user id, so a
    # keyed `db.get(...)` returns None; query by user_id instead.
    from sqlalchemy import select

    profile = (
        await db_session.execute(
            select(ProviderProfile).where(ProviderProfile.user_id == provider.id)
        )
    ).scalar_one()
    profile.is_online = False
    await db_session.flush()

    second = await _accept(client, booking.id, provider, auth_headers)
    assert second.status_code == 200, second.text
    assert second.json()["provider_id"] == str(provider.id)


@pytest.mark.asyncio
async def test_reaccepting_after_moving_past_accept_is_still_409(client, db_session, auth_headers):
    """Idempotency is scoped to ACCEPTED — not a licence to rewind the machine.

    A provider already en route who calls accept is making a genuinely illegal
    move, not retrying. That stays a transition error.
    """
    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service)
    _, booking = await _booking(
        db_session, service=service, provider_id=provider.id, status=BookingStatus.EN_ROUTE
    )

    resp = await _accept(client, booking.id, provider, auth_headers)

    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# everything else must be unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_invalid_transitions_still_return_409(client, db_session, auth_headers):
    """The fix must not blanket-convert transition errors into 403/410."""
    service = await _service(db_session)
    provider = await _eligible_provider(db_session, service)
    _, booking = await _booking(
        db_session, service=service, provider_id=provider.id, status=BookingStatus.ACCEPTED
    )

    # accepted → in_progress skips en_route, and is illegal regardless of OTP.
    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/start",
        json={"otp": "1234"},
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_unknown_booking_returns_404(client, db_session, auth_headers):
    stranger = await UserFactory.create(db_session, role="provider")

    resp = await _accept(client, uuid.uuid4(), stranger, auth_headers)

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# the race, for real
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_providers_racing_produce_exactly_one_winner(db_session):
    """Genuinely concurrent accepts, not a simulated sequence.

    Runs on its own committed data and its own sessions, because the shared
    ``db_session`` fixture holds one uncommitted transaction that a second
    connection cannot see. Cleaned up in a finally block.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = {}

    try:
        async with sessions() as setup:
            service = await _service(setup)
            a = await _eligible_provider(setup, service)
            b = await _eligible_provider(setup, service)
            _, booking = await _booking(setup, service=service)
            await setup.commit()
            ids = {"a": a.id, "b": b.id, "booking": booking.id}

        # Overlap has to be forced. Simply gathering two accepts lets asyncio
        # run them end-to-end and the test passes even with the lock removed.
        # A holds its transaction open until B has entered, so the two really
        # contend for the row.
        a_holds_lock = asyncio.Event()

        async def first(provider_id):
            async with sessions() as s:
                try:
                    _, newly = await BookingService(s).accept_booking(
                        booking_id=ids["booking"], provider_id=provider_id
                    )
                    a_holds_lock.set()
                    await asyncio.sleep(0.5)
                    await s.commit()
                    return newly
                except Exception as exc:
                    a_holds_lock.set()
                    await s.rollback()
                    return exc

        async def second(provider_id):
            await a_holds_lock.wait()
            async with sessions() as s:
                try:
                    _, newly = await BookingService(s).accept_booking(
                        booking_id=ids["booking"], provider_id=provider_id
                    )
                    await s.commit()
                    return newly
                except Exception as exc:
                    await s.rollback()
                    return exc

        results = await asyncio.wait_for(
            asyncio.gather(first(ids["a"]), second(ids["b"])), timeout=30
        )

        winners = [r for r in results if r is True]
        losers = [r for r in results if isinstance(r, Exception)]

        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        assert len(losers) == 1, f"expected exactly one loser, got {results}"
        # SVC-167: the loser now gets 410, not 403.
        assert (
            losers[0].status_code == 410
        ), f"loser got {losers[0].status_code}: {losers[0].message}"

        async with sessions() as check:
            from app.models.booking import Booking

            final = await check.get(Booking, ids["booking"])
            assert final.status is BookingStatus.ACCEPTED
            assert final.provider_id in (ids["a"], ids["b"])

    finally:
        if ids:
            async with sessions() as cleanup:
                from sqlalchemy import delete, text

                from app.models.booking import Booking

                await cleanup.execute(
                    text("DELETE FROM booking_status_logs WHERE booking_id = :b"),
                    {"b": str(ids["booking"])},
                )
                await cleanup.execute(delete(Booking).where(Booking.id == ids["booking"]))
                await cleanup.commit()
        await engine.dispose()
