"""SVC-154 — what PATCH /bookings/{id}/accept tells a provider when it fails.

Accept is the "fastest finger first" mechanic, so most calls to it *fail* by
design: one provider wins and every other recipient of the broadcast loses. Until
this ticket all of those failures came back as the same 409, carrying a
state-machine diagnostic ("Cannot move from accepted to accepted") that cannot be
shown to a provider and cannot be told apart from any other 409.

The contract these tests pin, which the Incoming Request Card branches on by
status code alone — never by message:

    410  the offer is over — claimed by another provider, cancelled, or expired
    404  no such booking
    200  yours already — an idempotent retry, and crucially *no second push*

The lost race returned 403 until SVC-167. It is 410 now, with the other two
"offer is over" causes: 403 is this codebase's "your identity was rejected",
and losing a race is not that — the provider was entitled to the offer and was
simply a moment late. Nothing here may assert 403 for a lost race again.

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
from app.models.service import Service
from app.services.booking_service import BookingService
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


async def _booking(db, *, provider_id=None, status=BookingStatus.REQUESTED):
    consumer = await UserFactory.create(db, role="consumer")
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
    address = Address(user_id=consumer.id, label="home", full_address="12 MG Road")
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


def _accept(client, booking_id, user, auth_headers):
    return client.patch(
        f"/api/v1/bookings/{booking_id}/accept",
        headers=auth_headers(str(user.id), "provider"),
    )


# --- 410: someone else got there first ----------------------------------------


@pytest.mark.asyncio
async def test_second_provider_gets_410_not_409(client, db_session, auth_headers):
    """The bug this ticket exists for.

    Before SVC-154 this was a 409 reading "Cannot move from accepted to accepted.
    Allowed: ['en_route', 'cancelled']" — the state machine's internal vocabulary,
    leaked to a provider who simply lost a race.

    SVC-154 made it 403; SVC-167 made it 410. Both the 409 and the 403 are
    regressions if they come back, hence the explicit assertions below.
    """
    winner = await UserFactory.create(db_session, role="provider")
    loser = await UserFactory.create(db_session, role="provider")
    _, booking = await _booking(db_session)

    assert (await _accept(client, booking.id, winner, auth_headers)).status_code == 200

    resp = await _accept(client, booking.id, loser, auth_headers)

    assert resp.status_code == 410, resp.text
    assert resp.status_code != 403, "a lost race is not an auth failure (SVC-167)"
    body = resp.json()
    assert body["status_code"] == 410
    # Safe to put in a toast: no internal status names, no winner's identity.
    assert "already been claimed" in body["message"]
    assert "accepted" not in body["message"]
    assert str(winner.id) not in resp.text


@pytest.mark.asyncio
async def test_lost_race_is_indistinguishable_from_expiry_by_status_code(
    client, db_session, auth_headers
):
    """All three "offer is over" causes answer 410, so the card needs one branch.

    This is the point of SVC-167: claimed / cancelled / expired are the same
    event as far as the Incoming Request Card is concerned — dismiss quietly.
    Splitting one of them onto a different code is what made 403 read as an auth
    failure on the majority path.
    """
    winner = await UserFactory.create(db_session, role="provider")
    loser = await UserFactory.create(db_session, role="provider")
    _, claimed = await _booking(db_session)
    await _accept(client, claimed.id, winner, auth_headers)

    _, expired = await _booking(db_session, status=BookingStatus.EXPIRED)
    _, cancelled = await _booking(db_session, status=BookingStatus.CANCELLED)

    codes = {
        name: (await _accept(client, booking.id, loser, auth_headers)).status_code
        for name, booking in (
            ("claimed", claimed),
            ("expired", expired),
            ("cancelled", cancelled),
        )
    }

    assert codes == {"claimed": 410, "expired": 410, "cancelled": 410}, codes


@pytest.mark.asyncio
async def test_losing_does_not_touch_the_winner_or_the_booking(client, db_session, auth_headers):
    winner = await UserFactory.create(db_session, role="provider")
    loser = await UserFactory.create(db_session, role="provider")
    _, booking = await _booking(db_session)

    await _accept(client, booking.id, winner, auth_headers)
    await _accept(client, booking.id, loser, auth_headers)

    await db_session.refresh(booking)
    assert booking.provider_id == winner.id
    assert booking.status is BookingStatus.ACCEPTED

    # And the loser has not acquired it in their own list.
    listing = await client.get("/api/v1/bookings/", headers=auth_headers(str(loser.id), "provider"))
    assert listing.status_code == 200
    assert str(booking.id) not in listing.text


# --- 410: the booking is gone -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", [BookingStatus.CANCELLED, BookingStatus.EXPIRED])
async def test_cancelled_or_expired_returns_410(client, db_session, auth_headers, gone):
    """Matches GET /bookings/{id}/offer, which already returns 410 for these.

    Two codes for one condition would force the card into
    `if (offer410 || accept409) dismiss()` — the special-casing this ticket removes.
    """
    provider = await UserFactory.create(db_session, role="provider")
    _, booking = await _booking(db_session, status=gone)

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
    provider = await UserFactory.create(db_session, role="provider")
    _, booking = await _booking(db_session, provider_id=provider.id, status=BookingStatus.CANCELLED)

    resp = await _accept(client, booking.id, provider, auth_headers)

    assert resp.status_code == 410, resp.text


# --- 200: idempotent retry ----------------------------------------------------


@pytest.mark.asyncio
async def test_reaccepting_your_own_booking_is_idempotent(client, db_session, auth_headers):
    """A retry after a lost response must not tell the winner they lost."""
    provider = await UserFactory.create(db_session, role="provider")
    _, booking = await _booking(db_session)

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

    provider = await UserFactory.create(db_session, role="provider")
    _, booking = await _booking(db_session)

    with patch("app.routers.bookings.send_booking_notification") as push:
        await _accept(client, booking.id, provider, auth_headers)
        assert push.delay.call_count == 1, "the real accept should notify"

        await _accept(client, booking.id, provider, auth_headers)
        assert (
            push.delay.call_count == 1
        ), "the idempotent retry notified the consumer a second time"


@pytest.mark.asyncio
async def test_reaccepting_after_moving_past_accept_is_still_409(client, db_session, auth_headers):
    """Idempotency is scoped to ACCEPTED — not a licence to rewind the machine.

    A provider already en route who calls accept is making a genuinely illegal
    move, not retrying. That stays a transition error.
    """
    provider = await UserFactory.create(db_session, role="provider")
    _, booking = await _booking(db_session, provider_id=provider.id, status=BookingStatus.EN_ROUTE)

    resp = await _accept(client, booking.id, provider, auth_headers)

    assert resp.status_code == 409, resp.text


# --- everything else must be unchanged ----------------------------------------


@pytest.mark.asyncio
async def test_other_invalid_transitions_still_return_409(client, db_session, auth_headers):
    """Neither fix may blanket-convert transition errors into 403/410.

    SVC-167 widened 410 to cover the lost race, which makes this guard more
    load-bearing, not less: a genuinely illegal move is still 409.
    """
    provider = await UserFactory.create(db_session, role="provider")
    _, booking = await _booking(db_session, provider_id=provider.id, status=BookingStatus.ACCEPTED)

    # accepted → in_progress skips en_route, and is illegal regardless of OTP.
    resp = await client.patch(
        f"/api/v1/bookings/{booking.id}/start",
        json={"otp": "1234"},
        headers=auth_headers(str(provider.id), "provider"),
    )

    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_unknown_booking_returns_404(client, db_session, auth_headers):
    provider = await UserFactory.create(db_session, role="provider")

    resp = await _accept(client, uuid.uuid4(), provider, auth_headers)

    assert resp.status_code == 404, resp.text


# --- the race, for real -------------------------------------------------------


@pytest.mark.asyncio
async def test_two_providers_racing_produce_exactly_one_winner(db_session):
    """Genuinely concurrent accepts, not a simulated sequence.

    The suite had no concurrency test at all before this. Sequential tests prove
    the *contract* the loser sees; only this proves the row lock actually
    serialises two live transactions, so a tie cannot assign the booking twice.

    Runs on its own committed data and its own sessions, because the shared
    ``db_session`` fixture holds one uncommitted transaction that a second
    connection cannot see. Cleaned up in a finally block.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = {}

    try:
        async with sessions() as setup:
            a = await UserFactory.create(setup, role="provider")
            b = await UserFactory.create(setup, role="provider")
            _, booking = await _booking(setup)
            await setup.commit()
            ids = {"a": a.id, "b": b.id, "booking": booking.id}

        # Overlap has to be forced. Simply gathering two accepts lets asyncio run
        # them end to end, and the test then passes even with the lock removed —
        # verified by deleting `with_for_update` and watching it still go green.
        # A holds its transaction open until B has entered, so the two genuinely
        # contend for the row.
        a_holds_lock = asyncio.Event()

        async def first(provider_id):
            async with sessions() as s:
                try:
                    _, newly = await BookingService(s).accept_booking(
                        booking_id=ids["booking"], provider_id=provider_id
                    )
                    a_holds_lock.set()
                    # Transaction still open: the row stays locked while B tries.
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
                    # Blocks here until A commits, if the lock is doing its job.
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
        # The loser is told the offer is gone — not handed a state-machine error
        # (409) and not an auth failure (403, which this was until SVC-167).
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

                # Logs first — they carry an FK to the booking.
                await cleanup.execute(
                    text("DELETE FROM booking_status_logs WHERE booking_id = :b"),
                    {"b": str(ids["booking"])},
                )
                await cleanup.execute(delete(Booking).where(Booking.id == ids["booking"]))
                await cleanup.commit()
        await engine.dispose()
