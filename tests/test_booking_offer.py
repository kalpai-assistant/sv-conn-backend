"""SVC-153 — what a provider may see about a broadcast job before accepting it.

The gap this closes: ``POST /bookings/`` pushes ``new_request`` to every nearby
provider, but the payload carries only a booking id and ``GET /bookings/{id}``
refuses anyone who is not the consumer or the *assigned* provider — and a
broadcast recipient is neither, since ``provider_id`` stays NULL until someone
accepts. The push was unactionable: the card had nothing to render.

The load-bearing tests here are the refusals. ``GET /{id}/offer`` hands a
consumer's home address to a provider who has not been assigned to anything, so
"who is refused" *is* the feature. The eligibility rule is shared with the
broadcast query (``app.services.provider_matching``) precisely so that the set of
providers who can open an offer cannot drift from the set who were sent it.

Status codes are the contract the Incoming Request Card keys off — it must not
parse messages:
    403  you were never offered this booking
    410  the offer is over (claimed by someone else, cancelled, expired)
    404  no such booking
"""
import uuid

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

# Consumer's address: Indiranagar, Bengaluru. The provider fixtures sit at known
# offsets from it so the distance assertions mean something.
ADDRESS_POINT = "SRID=4326;POINT(77.6408 12.9784)"
NEAR_POINT = "SRID=4326;POINT(77.6200 12.9700)"   # ~2.4 km away
FAR_POINT = "SRID=4326;POINT(78.2000 12.9700)"    # ~60 km away
FULL_ADDRESS = "12 MG Road, Indiranagar"
SERVICE_NAME = "Deep Cleaning"
SERVICE_AMOUNT = 1499.0


async def _service(db):
    slug = f"cleaning-{uuid.uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db.add(category)
    await db.flush()
    service = Service(
        category_id=category.id, name=SERVICE_NAME,
        base_price=SERVICE_AMOUNT, duration_minutes=90, is_active=True,
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

    Every knob here is one clause of the eligibility rule, so each refusal test
    flips exactly one and leaves the rest valid.
    """
    user = await UserFactory.create(db, role="provider")
    db.add(ProviderProfile(
        user_id=user.id, kyc_status=kyc, is_online=online,
        service_area_center=point, service_area_radius_km=radius_km,
    ))
    if subscribed:
        db.add(ProviderServiceModel(provider_id=user.id, service_id=service.id))
    await db.flush()
    return user


async def _open_booking(
    db, service, consumer=None, service_amount: float = SERVICE_AMOUNT, **kwargs
):
    """A REQUESTED booking with a geolocated address — i.e. one that broadcast."""
    consumer = consumer or await UserFactory.create(db, role="consumer")
    address = Address(
        user_id=consumer.id, label="home",
        full_address=FULL_ADDRESS, location=ADDRESS_POINT,
    )
    db.add(address)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=None,  # unassigned — the broadcast state
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.REQUESTED,
        service_amount=service_amount,
        **kwargs,
    )
    return consumer, address, booking


def _get(client, booking_id, user, auth_headers):
    return client.get(
        f"/api/v1/bookings/{booking_id}/offer",
        headers=auth_headers(str(user.id), "provider"),
    )


# --- the happy path -----------------------------------------------------------

@pytest.mark.asyncio
async def test_eligible_provider_sees_the_card_fields(client, db_session, auth_headers):
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    _, _, booking = await _open_booking(db_session, service)

    resp = await _get(client, booking.id, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["booking_id"] == str(booking.id)
    assert body["service_name"] == SERVICE_NAME
    assert body["service_amount"] == SERVICE_AMOUNT
    assert body["full_address"] == FULL_ADDRESS
    # ~2.4 km apart; asserted as a range because the exact value is PostGIS's,
    # not ours — pinning it to 2 decimals would test the geoid, not our code.
    assert 2.0 < body["distance_km"] < 3.0


@pytest.mark.asyncio
async def test_offer_exposes_nothing_beyond_the_card(client, db_session, auth_headers):
    """The offer goes to providers assigned to nothing — so it must stay minimal.

    ``doorstep_otp`` is the one that matters: it gates en_route → in_progress and
    is meant to reach the provider from the consumer at the door. Serving it here
    would let any nearby provider skip that gate.
    """
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    _, _, booking = await _open_booking(db_session, service, doorstep_otp="9182")

    resp = await _get(client, booking.id, provider, auth_headers)

    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {
        "booking_id", "service_name", "service_amount", "full_address", "distance_km",
    }
    assert "9182" not in resp.text
    for leaked in ("consumer_id", "doorstep_otp", "provider_payout", "platform_commission"):
        assert leaked not in resp.text


@pytest.mark.asyncio
async def test_two_open_bookings_do_not_cross_wire(client, db_session, auth_headers):
    """Each offer must describe its own booking, not whichever was found first."""
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    _, _, first = await _open_booking(db_session, service, service_amount=1499.0)
    _, _, second = await _open_booking(db_session, service, service_amount=2599.0)

    first_resp = await _get(client, first.id, provider, auth_headers)
    second_resp = await _get(client, second.id, provider, auth_headers)

    assert first_resp.json()["booking_id"] == str(first.id)
    assert second_resp.json()["booking_id"] == str(second.id)
    assert first_resp.json()["service_amount"] == 1499.0
    assert second_resp.json()["service_amount"] == 2599.0


# --- 403: you were never offered this ----------------------------------------
#
# One test per clause of the eligibility rule. Each flips a single knob, so a
# passing test proves that clause alone is what refuses.

@pytest.mark.asyncio
async def test_provider_outside_their_own_radius_is_refused(
    client, db_session, auth_headers
):
    service = await _service(db_session)
    provider = await _provider(db_session, service, point=FAR_POINT)
    _, _, booking = await _open_booking(db_session, service)

    resp = await _get(client, booking.id, provider, auth_headers)

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_radius_is_per_provider_not_a_global_constant(
    client, db_session, auth_headers
):
    """The same distance is in range or out of it depending on the provider.

    The broadcast matches on each provider's own ``service_area_radius_km``, so a
    fixed-radius reimplementation would pass every other test here and fail this
    one.
    """
    service = await _service(db_session)
    narrow = await _provider(db_session, service, point=FAR_POINT, radius_km=10)
    wide = await _provider(db_session, service, point=FAR_POINT, radius_km=100)
    _, _, booking = await _open_booking(db_session, service)

    assert (await _get(client, booking.id, narrow, auth_headers)).status_code == 403
    assert (await _get(client, booking.id, wide, auth_headers)).status_code == 200


@pytest.mark.asyncio
async def test_provider_not_subscribed_to_the_service_is_refused(
    client, db_session, auth_headers
):
    service = await _service(db_session)
    provider = await _provider(db_session, service, subscribed=False)
    _, _, booking = await _open_booking(db_session, service)

    resp = await _get(client, booking.id, provider, auth_headers)

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_unverified_provider_is_refused(client, db_session, auth_headers):
    service = await _service(db_session)
    provider = await _provider(db_session, service, kyc=KYCStatus.PENDING)
    _, _, booking = await _open_booking(db_session, service)

    resp = await _get(client, booking.id, provider, auth_headers)

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_offline_provider_is_refused(client, db_session, auth_headers):
    """⚠️ Pins the ticket's AC, which is an assumption not yet confirmed.

    The broadcast only notifies online providers, so refusing an offline one keeps
    the two rules identical — that part is sound. What is NOT verified is the
    client side: if the provider app sets ``is_online=false`` when it backgrounds,
    then every push→tap→open flow lands here, because the app is by definition
    backgrounded when the push arrives, and the feature breaks outright.

    Do not read this test as evidence that behaviour is correct. It records what
    SVC-153 specified. If the app does toggle on background, this test and the
    rule under it both need revisiting — see the note on the ticket.
    """
    service = await _service(db_session)
    provider = await _provider(db_session, service, online=False)
    _, _, booking = await _open_booking(db_session, service)

    resp = await _get(client, booking.id, provider, auth_headers)

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_consumer_cannot_read_the_offer(client, db_session, auth_headers):
    """Not a provider at all — refused by the same rule, no special case."""
    service = await _service(db_session)
    consumer, _, booking = await _open_booking(db_session, service)

    resp = await client.get(
        f"/api/v1/bookings/{booking.id}/offer",
        headers=auth_headers(str(consumer.id), "consumer"),
    )

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_refusal_does_not_reveal_whether_the_booking_was_taken(
    client, db_session, auth_headers
):
    """A non-recipient gets 403 whether the booking is open or already claimed.

    Eligibility is checked before the still-open check for exactly this reason —
    the reverse order would turn this endpoint into an oracle for whether any
    given booking id exists and has been accepted.
    """
    service = await _service(db_session)
    outsider = await _provider(db_session, service, point=FAR_POINT)
    winner = await _provider(db_session, service)
    _, _, booking = await _open_booking(db_session, service)

    while_open = await _get(client, booking.id, outsider, auth_headers)

    booking.status = BookingStatus.ACCEPTED
    booking.provider_id = winner.id
    await db_session.flush()
    once_taken = await _get(client, booking.id, outsider, auth_headers)

    assert while_open.status_code == 403
    assert once_taken.status_code == 403, "leaked that the booking had been claimed"
    assert while_open.json()["message"] == once_taken.json()["message"]


# --- 410: the offer is over ---------------------------------------------------

@pytest.mark.asyncio
async def test_claimed_by_another_provider_returns_410(
    client, db_session, auth_headers
):
    """The losing provider's card dismisses on this — it must not be a 403.

    403 would be indistinguishable from "you were never eligible", and the card
    would have no way to choose between a quiet dismiss and an error.
    """
    service = await _service(db_session)
    loser = await _provider(db_session, service)
    winner = await _provider(db_session, service)
    _, _, booking = await _open_booking(db_session, service)

    assert (await _get(client, booking.id, loser, auth_headers)).status_code == 200

    booking.status = BookingStatus.ACCEPTED
    booking.provider_id = winner.id
    await db_session.flush()

    resp = await _get(client, booking.id, loser, auth_headers)

    assert resp.status_code == 410, resp.text
    assert resp.json()["status_code"] == 410


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [BookingStatus.CANCELLED, BookingStatus.EXPIRED])
async def test_cancelled_or_expired_booking_returns_410(
    client, db_session, auth_headers, status
):
    """Expiry matters most: the card says "Accept within 2 minutes"."""
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    _, _, booking = await _open_booking(db_session, service)

    booking.status = status
    await db_session.flush()

    resp = await _get(client, booking.id, provider, auth_headers)

    assert resp.status_code == 410, resp.text


@pytest.mark.asyncio
async def test_address_without_a_location_is_refused(client, db_session, auth_headers):
    """No geography means the broadcast bailed out and nobody was ever notified."""
    service = await _service(db_session)
    provider = await _provider(db_session, service)
    consumer = await UserFactory.create(db_session, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address=FULL_ADDRESS)
    db_session.add(address)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session, consumer_id=consumer.id, provider_id=None,
        service_id=service.id, address_id=address.id, status=BookingStatus.REQUESTED,
    )

    resp = await _get(client, booking.id, provider, auth_headers)

    assert resp.status_code == 403, resp.text


# --- 404 ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_booking_returns_404(client, db_session, auth_headers):
    service = await _service(db_session)
    provider = await _provider(db_session, service)

    resp = await _get(client, uuid.uuid4(), provider, auth_headers)

    assert resp.status_code == 404, resp.text


# --- the broadcast push itself ------------------------------------------------
#
# The Celery task has never been executed by a test — it is mocked at the router
# in test_bookings.py, so its body is unproven. A real DB test would need fixtures
# committed to sc_test (the task opens its own sync session and cannot see the
# async test transaction), so these drive it with a stubbed session instead. That
# covers the logic; it does not cover the SQL, which the endpoint tests above do.

def _fake_session(booking, address, service, recipients):
    """Stand-in for SyncSessionLocal: dispatches db.get by model class."""
    from unittest.mock import MagicMock

    from app.models.address import Address as AddressModel
    from app.models.booking import Booking as BookingModel
    from app.models.service import Service as ServiceModel

    db = MagicMock()
    db.get.side_effect = lambda model, _id: {
        BookingModel: booking, AddressModel: address, ServiceModel: service,
    }.get(model)
    db.execute.return_value.scalars.return_value.all.return_value = recipients
    db.__enter__.return_value = db
    db.__exit__.return_value = False
    return db


def _run_broadcast(booking, address, service, recipients):
    """Run send_booking_notification for new_request; return the push kwargs."""
    from unittest.mock import MagicMock, patch

    from app.tasks import notification_tasks

    with patch("app.database.SyncSessionLocal",
               return_value=_fake_session(booking, address, service, recipients)), \
         patch.object(notification_tasks, "send_push_notification") as push:
        notification_tasks.send_booking_notification(
            booking_id=str(booking.id), event="new_request"
        )
    return [c.kwargs for c in push.call_args_list] or [MagicMock()]


def _stub(**attrs):
    from unittest.mock import MagicMock

    obj = MagicMock()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def test_broadcast_push_names_the_service():
    booking = _stub(id=uuid.uuid4(), consumer_address_id=uuid.uuid4(),
                    service_id=uuid.uuid4())
    sent = _run_broadcast(
        booking,
        _stub(location=ADDRESS_POINT, full_address=FULL_ADDRESS),
        _stub(name=SERVICE_NAME),
        [_stub(fcm_token="tok_1")],
    )

    assert len(sent) == 1
    assert SERVICE_NAME in sent[0]["body"]
    assert "{service}" not in sent[0]["body"]


def test_broadcast_push_leaks_neither_address_nor_amount():
    """The guarantee behind putting the card's data behind an endpoint.

    A push reaches every nearby provider and cannot be recalled once the job is
    taken, so the consumer's address must not ride along in it. The amount stays
    out too — rendering it here would need a ₹ formatter this branch does not
    have (see build_new_request_body).
    """
    booking = _stub(id=uuid.uuid4(), consumer_address_id=uuid.uuid4(),
                    service_id=uuid.uuid4())
    sent = _run_broadcast(
        booking,
        _stub(location=ADDRESS_POINT, full_address=FULL_ADDRESS),
        _stub(name=SERVICE_NAME),
        [_stub(fcm_token="tok_1")],
    )

    assert set(sent[0]["data"]) == {"booking_id", "event"}
    blob = f"{sent[0]['title']}{sent[0]['body']}{sent[0]['data']}"
    assert FULL_ADDRESS not in blob
    assert "Indiranagar" not in blob
    assert "₹" not in blob
    assert "1499" not in blob


def test_broadcast_push_falls_back_when_the_service_is_missing():
    """Unreachable via the FK today, but a literal "{service}" would be worse."""
    booking = _stub(id=uuid.uuid4(), consumer_address_id=uuid.uuid4(),
                    service_id=uuid.uuid4())
    sent = _run_broadcast(
        booking,
        _stub(location=ADDRESS_POINT, full_address=FULL_ADDRESS),
        None,
        [_stub(fcm_token="tok_1")],
    )

    assert "{service}" not in sent[0]["body"]
    assert "accept within 2 minutes" in sent[0]["body"].lower()


def test_broadcast_skips_providers_without_a_token():
    """No FCM token means nothing to send to — must not raise."""
    booking = _stub(id=uuid.uuid4(), consumer_address_id=uuid.uuid4(),
                    service_id=uuid.uuid4())
    from unittest.mock import patch

    from app.tasks import notification_tasks

    with patch("app.database.SyncSessionLocal", return_value=_fake_session(
        booking,
        _stub(location=ADDRESS_POINT, full_address=FULL_ADDRESS),
        _stub(name=SERVICE_NAME),
        [_stub(fcm_token=None), _stub(fcm_token="tok_2")],
    )), patch.object(notification_tasks, "send_push_notification") as push:
        notification_tasks.send_booking_notification(
            booking_id=str(booking.id), event="new_request"
        )

    assert push.call_count == 1
    assert push.call_args.kwargs["token"] == "tok_2"


# --- the rule is shared, not copied -------------------------------------------

def test_offer_and_broadcast_use_one_eligibility_definition():
    """Guards the invariant the whole design rests on.

    If the endpoint ever grows its own copy of the rule, providers can be pushed
    jobs they cannot open, or open jobs they were never sent — and both failures
    are silent. Cheapest possible guard: the broadcast task must not build the
    predicate itself.
    """
    import inspect

    from app.services import provider_matching
    from app.tasks import notification_tasks

    source = inspect.getsource(notification_tasks)
    assert "eligible_providers_stmt" in source, (
        "the broadcast no longer calls the shared predicate"
    )
    for reimplemented in ("ST_DWithin", "kyc_status ==", "is_online"):
        assert reimplemented not in source, (
            f"broadcast rebuilt {reimplemented!r} instead of using provider_matching"
        )

    # Both consumers of the rule resolve to the same builder.
    assert provider_matching.eligible_provider_conditions.__module__ == (
        "app.services.provider_matching"
    )
