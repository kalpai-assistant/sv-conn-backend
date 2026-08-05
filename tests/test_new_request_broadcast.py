"""SVC-153 — the new_request FCM broadcast.

Pins the two invariants of the push:

1. **Body identifies the job** (service name), so a provider seeing the tray
   notification can decide whether to open the app before it opens.
2. **Payload carries no consumer PII** — no address, no amount, no name. The
   card fetches these from GET /bookings/{id}/offer, which stops answering
   the moment the booking is claimed. A push cannot be taken back; an
   endpoint can. See the SVC-153 rationale in
   `app/tasks/notification_tasks.py::build_new_request_body`.

Also pins the eligibility invariant SVC-153 established: the set of push
recipients must equal the set who can then open the offer. Both come from
`eligible_providers_stmt`; if they ever drift, a provider gets a push for a
job they can't open.
"""

from unittest.mock import patch

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.category import Category
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.tasks.notification_tasks import build_new_request_body
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

ADDRESS_POINT = "SRID=4326;POINT(77.6408 12.9784)"  # Indiranagar
NEAR_POINT = "SRID=4326;POINT(77.6200 12.9700)"  # ~2.4 km
FAR_POINT = "SRID=4326;POINT(78.2000 12.9700)"  # ~60 km


# ---------------------------------------------------------------------------
# build_new_request_body — pure function, no DB / Celery
# ---------------------------------------------------------------------------


def test_body_names_the_service():
    """The whole point — the provider sees WHAT the job is, not just that
    something arrived."""
    body = build_new_request_body("Deep Cleaning")
    assert "Deep Cleaning" in body
    assert "accept" in body.lower()


def test_body_falls_back_when_service_missing():
    """FK makes service-missing unreachable in prod, but a literal
    `{service}` in a user-facing string would be worse than a vague one.
    Belt-and-braces."""
    body = build_new_request_body(None)
    assert "{" not in body and "}" not in body
    assert "new service request" in body.lower()


@pytest.mark.parametrize("bad_input", ["", None])
def test_body_never_renders_placeholder_literal(bad_input):
    body = build_new_request_body(bad_input)
    assert "{service}" not in body


# ---------------------------------------------------------------------------
# send_booking_notification("new_request") — full path with sync-session mock
# ---------------------------------------------------------------------------


async def _seed_booking_with_geo(db):
    """A REQUESTED, geolocated booking — the state the broadcast runs against."""
    from uuid import uuid4

    consumer = await UserFactory.create(db, role="consumer")
    slug = f"cleaning-{uuid4().hex[:8]}"
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
    address = Address(
        user_id=consumer.id,
        label="home",
        full_address="12 MG Road, Indiranagar",
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
    )
    return consumer, service, address, booking


async def _seed_provider(
    db,
    service,
    *,
    fcm_token: str,
    online: bool = True,
    kyc: KYCStatus = KYCStatus.VERIFIED,
    subscribed: bool = True,
    point: str = NEAR_POINT,
    radius_km: int = 10,
):
    user = await UserFactory.create(db, role="provider")
    user.fcm_token = fcm_token
    db.add(
        ProviderProfile(
            user_id=user.id,
            kyc_status=kyc,
            is_online=online,
            service_area_center=point,
            service_area_radius_km=radius_km,
        )
    )
    # Flush the profile BEFORE adding the subscription — provider_services.
    # provider_id FKs to provider_profiles.user_id, and SQLAlchemy's async
    # flush doesn't reliably order inserts by that FK path (it does for
    # PK-referencing FKs). Without this intermediate flush we get:
    # `provider_services_provider_id_fkey: Key not present in
    # provider_profiles`.
    await db.flush()
    if subscribed:
        db.add(ProviderServiceModel(provider_id=user.id, service_id=service.id))
        await db.flush()
    return user


def _run_broadcast(booking_id, db):
    """Invoke the Celery task with the test's session patched in for the
    sync SessionLocal it normally opens. Runs synchronously (Celery task
    body is sync), so no eager-mode config needed."""
    from app.tasks import notification_tasks

    # The task does `from app.database import SyncSessionLocal` INSIDE its
    # try block — so `SyncSessionLocal` isn't a module attribute of
    # `notification_tasks` and patch.object(notification_tasks, ...) fails
    # with "does not have the attribute". Patch at the source instead;
    # the inner `import` picks up the mock.
    class _FakeSyncSessionCtx:
        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    with (
        patch("app.database.SyncSessionLocal", lambda: _FakeSyncSessionCtx()),
        patch.object(notification_tasks, "send_push_notification") as mock_push,
    ):
        # `.apply()` runs the task synchronously in-process, with `self`
        # bound correctly. Using `__wrapped__` directly here double-binds
        # `self` and Celery complains about multiple values for the same
        # kwarg — apply avoids that whole layer.
        notification_tasks.send_booking_notification.apply(
            kwargs={"booking_id": str(booking_id), "event": "new_request"}
        )
    return mock_push


@pytest.mark.asyncio
async def test_broadcast_reaches_eligible_provider_with_service_named_in_body(db_session):
    _, service, _, booking = await _seed_booking_with_geo(db_session)
    await _seed_provider(db_session, service, fcm_token="tok-eligible")

    mock_push = _run_broadcast(booking.id, db_session)

    # Provider was eligible → received exactly one push.
    calls_for_provider = [
        c for c in mock_push.call_args_list if c.kwargs.get("token") == "tok-eligible"
    ]
    assert len(calls_for_provider) == 1
    call = calls_for_provider[0]
    assert "Deep Cleaning" in call.kwargs["body"]


@pytest.mark.asyncio
async def test_broadcast_payload_contains_no_consumer_pii(db_session):
    """The load-bearing privacy invariant: no address, no name, no amount in
    the FCM data payload. A push cannot be un-sent once delivered."""
    _, service, address, booking = await _seed_booking_with_geo(db_session)
    await _seed_provider(db_session, service, fcm_token="tok-eligible")

    mock_push = _run_broadcast(booking.id, db_session)

    assert mock_push.call_count >= 1
    for call in mock_push.call_args_list:
        data = call.kwargs["data"]
        assert set(data.keys()) == {"booking_id", "event"}
        # Belt-and-braces: the specific values that would be a leak are absent
        # from BOTH the data payload AND the visible body/title.
        blob = f"{data} {call.kwargs['body']} {call.kwargs['title']}"
        assert address.full_address not in blob
        assert str(booking.service_amount) not in blob


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_ineligible",
    [
        pytest.param({"online": False}, id="offline"),
        pytest.param({"kyc": KYCStatus.SUBMITTED}, id="kyc-unverified"),
        pytest.param({"subscribed": False}, id="not-subscribed"),
        pytest.param({"point": FAR_POINT, "radius_km": 5}, id="out-of-radius"),
    ],
)
async def test_broadcast_skips_ineligible_providers(db_session, make_ineligible):
    """The eligibility predicate that gates the push is the same one that
    gates GET /offer (SVC-153 invariant). A push must not land on a device
    whose owner can't then open the job."""
    _, service, _, booking = await _seed_booking_with_geo(db_session)
    await _seed_provider(db_session, service, fcm_token="tok-should-not-fire", **make_ineligible)

    mock_push = _run_broadcast(booking.id, db_session)

    for call in mock_push.call_args_list:
        assert call.kwargs["token"] != "tok-should-not-fire"


@pytest.mark.asyncio
async def test_broadcast_skips_providers_without_fcm_token(db_session):
    """A provider whose FCM token is null (never registered, or logged out)
    passes eligibility but has nowhere to receive the push. The task must
    not crash and must not fabricate a null-token send."""
    _, service, _, booking = await _seed_booking_with_geo(db_session)
    # Note: fcm_token intentionally not set — user.fcm_token stays None.
    provider = await UserFactory.create(db_session, role="provider")
    db_session.add(
        ProviderProfile(
            user_id=provider.id,
            kyc_status=KYCStatus.VERIFIED,
            is_online=True,
            service_area_center=NEAR_POINT,
            service_area_radius_km=10,
        )
    )
    db_session.add(ProviderServiceModel(provider_id=provider.id, service_id=service.id))
    await db_session.flush()

    mock_push = _run_broadcast(booking.id, db_session)

    # No call for this provider (they have no token). Any calls must have a
    # non-empty token — never None / empty.
    for call in mock_push.call_args_list:
        assert call.kwargs.get("token")


@pytest.mark.asyncio
async def test_broadcast_no_op_when_address_has_no_location(db_session):
    """Ungeocoded consumer address: the query has nothing to compute
    ST_DWithin against. Broadcast bails without crashing; nobody gets a push."""
    consumer = await UserFactory.create(db_session, role="consumer")
    slug = f"cleaning-{__import__('uuid').uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db_session.add(category)
    await db_session.flush()
    service = Service(
        category_id=category.id,
        name="Deep Cleaning",
        base_price=100.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)
    address = Address(
        user_id=consumer.id, label="home", full_address="Missing coords", location=None
    )
    db_session.add(address)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.REQUESTED,
    )

    # Even an eligible-shaped provider gets nothing when the origin has no coords.
    await _seed_provider(db_session, service, fcm_token="tok-eligible")

    mock_push = _run_broadcast(booking.id, db_session)

    assert mock_push.call_count == 0
