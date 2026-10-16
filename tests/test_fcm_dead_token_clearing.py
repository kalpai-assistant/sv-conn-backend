"""SVC-301 — clear ``users.fcm_token`` when FCM confirms the token is dead.

Companion to SVC-276. SVC-276 stopped the broadcast from picking providers whose
``fcm_token`` was NULL; this closes the other side of the loop by turning a live
row containing a *dead* token into a NULL, so a provider whose app was
uninstalled or whose OS-side notification permission was revoked stops being
re-picked broadcast after broadcast for a push that will never land.

The load-bearing assertions here are:

* the User row is nulled in the SAME task run — not on the next retry, not by a
  cleanup script — so SVC-276 excludes them by construction on the next
  broadcast;
* only the two definitively-dead error classes clear (``UnregisteredError``,
  ``SenderIdMismatchError``). A transient error (throttle, network blip) must
  NOT clear, or one bad five-minute window loses every provider's token;
* a single dead recipient no longer aborts the broadcast to the other
  recipients — pre-SVC-301 the outer ``except Exception`` caught the first
  UnregisteredError and short-circuited the rest of the loop.

Tests use the ``cancelled`` event when only a single recipient is needed — same
try/except block, far less setup than a full ``new_request`` broadcast — and
switch to the broadcast for the "one dead does not kill the batch" pin.
"""

from unittest.mock import patch

import pytest
from firebase_admin import messaging as fcm_messaging

from app.models.booking import BookingStatus
from app.tasks.notification_tasks import send_booking_notification, send_payout_notification
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory


@pytest.fixture
async def committed(db_session):
    """Commit rows the Celery task's own connection can see, then remove them.

    Same shape as ``test_cancel_notification.committed``. The task opens its
    own ``SyncSessionLocal`` and cannot see this transaction until commit.
    Committing leaks into ``sc_test`` though, so clean up on the way out —
    other suites count table-wide.
    """
    from sqlalchemy import delete

    from app.models.address import Address as AddressModel
    from app.models.booking import Booking as BookingModel
    from app.models.booking_item import BookingItem
    from app.models.booking_offer import BookingOffer
    from app.models.provider_profile import ProviderProfile
    from app.models.provider_service import ProviderService
    from app.models.user import User

    created: list = []
    yield created

    booking_ids = [o.id for o in created if isinstance(o, BookingModel)]
    user_ids = [o.id for o in created if isinstance(o, User)]
    address_ids = [o.id for o in created if isinstance(o, AddressModel)]

    if booking_ids:
        await db_session.execute(
            delete(BookingOffer).where(BookingOffer.booking_id.in_(booking_ids))
        )
        await db_session.execute(delete(BookingItem).where(BookingItem.booking_id.in_(booking_ids)))
        await db_session.execute(delete(BookingModel).where(BookingModel.id.in_(booking_ids)))
    if user_ids:
        await db_session.execute(
            delete(ProviderService).where(ProviderService.provider_id.in_(user_ids))
        )
        await db_session.execute(
            delete(ProviderProfile).where(ProviderProfile.user_id.in_(user_ids))
        )
    if address_ids:
        await db_session.execute(delete(AddressModel).where(AddressModel.id.in_(address_ids)))
    await db_session.flush()
    if user_ids:
        await db_session.execute(delete(User).where(User.id.in_(user_ids)))
    await db_session.commit()


async def _address(db, user_id):
    from app.models.address import Address
    from app.utils.geo import build_point_wkt

    addr = Address(
        user_id=user_id,
        label="home",
        full_address="Somewhere in Pune",
        location=build_point_wkt(18.5204, 73.8567),
    )
    db.add(addr)
    await db.flush()
    return addr


def _run_task(task_callable, **task_kwargs):
    """Run a Celery task against sc_test with FCM patched at the caller site.

    ``task_callable`` gets executed inside the sync-session and FCM patches;
    the caller passes a ``side_effect`` dict via ``fcm_side_effect`` if the
    default no-op send is not what the test wants.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_url = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(sync_url)
    test_factory = sessionmaker(bind=engine)
    fcm_side_effect = task_kwargs.pop("fcm_side_effect", lambda token, **_: None)
    calls: list[str] = []

    def _tracking(token, **kwargs):
        calls.append(token)
        return fcm_side_effect(token, **kwargs)

    with (
        patch("app.database.SyncSessionLocal", test_factory),
        patch("app.tasks.notification_tasks.send_push_notification", side_effect=_tracking),
    ):
        try:
            task_callable(**task_kwargs)
        finally:
            engine.dispose()
    return calls


async def _current_token(db_session, user_id):
    """Read the User's ``fcm_token`` from a fresh transaction (post-commit)."""
    from sqlalchemy import select

    from app.models.user import User

    await db_session.commit()  # drop the read-transaction snapshot
    return (await db_session.execute(select(User.fcm_token).where(User.id == user_id))).scalar_one()


# ---------------------------------------------------------------------------
# Single-recipient dead-token clearing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(fcm_messaging.UnregisteredError("token expired"), id="unregistered"),
        pytest.param(fcm_messaging.SenderIdMismatchError("wrong project"), id="sender-id-mismatch"),
    ],
)
async def test_dead_token_response_nulls_the_row(db_session, committed, error):
    """The ticket's AC: after FCM says the token is dead, the DB reflects it.

    Uses ``cancelled`` because it hits the same try/except with a single
    recipient and none of the ``new_request`` setup — one provider, one
    push, one clear.
    """
    consumer = await UserFactory.create(db_session, role="consumer", fcm_token=None)
    provider = await UserFactory.create(db_session, role="provider", fcm_token="dead-token")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.ACCEPTED,
    )
    committed.extend([booking, addr, provider, consumer])
    await db_session.commit()

    def _boom(token, **_):
        raise error

    _run_task(
        send_booking_notification,
        booking_id=str(booking.id),
        event="cancelled",
        actor_id=str(consumer.id),
        fcm_side_effect=_boom,
    )

    assert await _current_token(db_session, provider.id) is None


@pytest.mark.asyncio
async def test_transient_error_does_not_clear_the_token(db_session, committed):
    """The dangerous inverse: a temporary FCM failure must NOT null tokens.

    ``QuotaExceededError`` and its siblings mean "try again shortly", not
    "this device is gone". Treating them as dead would empty out every
    reachable provider's token during any five-minute FCM incident and
    stop dispatch cold on their recovery.
    """
    consumer = await UserFactory.create(db_session, role="consumer", fcm_token=None)
    provider = await UserFactory.create(db_session, role="provider", fcm_token="alive-token")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=addr.id,
        status=BookingStatus.ACCEPTED,
    )
    committed.extend([booking, addr, provider, consumer])
    await db_session.commit()

    def _throttle(token, **_):
        raise fcm_messaging.QuotaExceededError("slow down")

    # The push is retried on its own (resend_booking_push), not by retrying
    # the task. The assertion that matters is still the row: the token must
    # survive a transient error unscathed.
    with patch("app.tasks.notification_tasks.resend_booking_push.apply_async") as resend:
        _run_task(
            send_booking_notification,
            booking_id=str(booking.id),
            event="cancelled",
            actor_id=str(consumer.id),
            fcm_side_effect=_throttle,
        )

    assert await _current_token(db_session, provider.id) == "alive-token"
    [call] = resend.call_args_list
    assert call.kwargs["kwargs"]["user_id"] == str(provider.id)


# ---------------------------------------------------------------------------
# Multi-recipient broadcast: one dead does not kill the rest
# ---------------------------------------------------------------------------


async def _service(db):
    """Minimal service + category, unique per test."""
    import uuid as _uuid

    from app.models.category import Category
    from app.models.service import Service

    slug = f"deep-cleaning-{_uuid.uuid4().hex[:8]}"
    category = Category(name=slug, slug=slug, is_active=True, sort_order=1)
    db.add(category)
    await db.flush()
    svc = Service(
        category_id=category.id,
        name="Deep Cleaning",
        base_price=1499.0,
        duration_minutes=90,
        is_active=True,
    )
    db.add(svc)
    await db.flush()
    return svc


async def _eligible_provider(db, service, *, token):
    from app.models.provider_profile import KYCStatus, ProviderProfile
    from app.models.provider_service import ProviderService as ProviderServiceModel
    from app.utils.geo import build_point_wkt

    user = await UserFactory.create(db, role="provider", fcm_token=token)
    db.add(
        ProviderProfile(
            user_id=user.id,
            kyc_status=KYCStatus.VERIFIED,
            is_online=True,
            service_area_center=build_point_wkt(18.5204, 73.8567),
            service_area_radius_km=25,
        )
    )
    db.add(ProviderServiceModel(provider_id=user.id, service_id=service.id))
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_one_dead_token_does_not_abort_broadcast_to_others(db_session, committed):
    """Pre-SVC-301, the first UnregisteredError in the loop escaped to the
    outer try/except and the remaining recipients never got their push.
    After SVC-301, the dead recipient's token is cleared and the loop
    continues — every other reachable provider still hears about the job.

    This is arguably the more valuable half of the ticket. A dead-token
    provider whose row happened to sort first would silence every other
    eligible provider for the same booking.
    """
    service = await _service(db_session)
    dead_provider = await _eligible_provider(db_session, service, token="dead-token")
    live_provider = await _eligible_provider(db_session, service, token="live-token")

    consumer = await UserFactory.create(db_session, role="consumer", fcm_token=None)
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        service_id=service.id,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )

    committed.extend([booking, addr, dead_provider, live_provider, consumer])
    await db_session.commit()

    def _boom_for_dead(token, **_):
        if token == "dead-token":
            raise fcm_messaging.UnregisteredError("token expired")

    calls = _run_task(
        send_booking_notification,
        booking_id=str(booking.id),
        event="new_request",
        fcm_side_effect=_boom_for_dead,
    )

    # Both tokens were attempted. The live one is untouched; the dead one is
    # nulled. Ordering of the two is not asserted — the broadcast query is
    # ORDER BY-less and this test would false-fail if a future query added
    # an order the code did not actually depend on.
    assert set(calls) == {"dead-token", "live-token"}
    assert await _current_token(db_session, dead_provider.id) is None
    assert await _current_token(db_session, live_provider.id) == "live-token"


# ---------------------------------------------------------------------------
# Payout path — same handling on the single-recipient send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payout_notification_clears_dead_token(db_session, committed):
    """The ticket scope is "any FCM dead-token response", not "broadcast only".

    ``send_payout_notification`` is a 1-1 send but writes to the same
    ``users.fcm_token`` column and hits the same failure mode. Clearing here
    keeps a batch payout run from re-firing an undeliverable push every
    time an eligible batch settles for the same provider.
    """
    provider = await UserFactory.create(db_session, role="provider", fcm_token="dead-token")
    committed.append(provider)
    await db_session.commit()

    def _boom(token, **_):
        raise fcm_messaging.UnregisteredError("token expired")

    _run_task(
        send_payout_notification,
        provider_id=str(provider.id),
        event="completed",
        amount_paise=100000,
        fcm_side_effect=_boom,
    )

    assert await _current_token(db_session, provider.id) is None
