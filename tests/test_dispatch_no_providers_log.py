"""
Why a booking ended at "No partners found nearby" (SVC-349).

Two dead ends look the same to the consumer, and both now leave a WARNING:

* ``dispatch.no_providers``: the broadcast found nobody. A per-stage funnel
  (``broadcast_funnel``) says which eligibility stage ruled everyone out.
* ``dispatch.no_acceptance``: offers went out but the window ran out; the
  sweep logs each booking with its offer counts.

The funnel's stages are the very expressions eligibility uses, so its last
count must equal the broadcast set; ``test_the_funnel_agrees_with_the_broadcast``
is the guard against drift. Neither log may ever get in the way of the expiry
it describes.

The per-reason tests run the real ``_log_no_providers`` on the test's own
session (``run_sync``), so their rows needn't be committed. The end-to-end ones
run the real Celery task and sweep against committed rows.
"""

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from app.models.address import Address
from app.models.booking import BookingStatus, PaymentMethod, PaymentStatus
from app.models.booking_item import BookingItem
from app.models.booking_offer import BookingOffer, OfferResponse
from app.models.provider_profile import KYCStatus
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.services.provider_matching import broadcast_recipients_stmt
from app.services.provider_service import ProviderService
from app.tasks.notification_tasks import _log_no_providers
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import ADDRESS_POINT, _booking, _eligible_provider, _service
from tests.test_paid_no_taker import _get, _seed, _sweep, committed_sweep_world  # noqa: F401
from tests.test_two_level_dispatch import _team_pro
from tests.test_unbroadcastable_booking import (  # noqa: F401
    _address,
    _run_broadcast,
    _status_of,
    committed,
)

NOTIFY = "app.tasks.notification_tasks"
CLEANUP = "app.tasks.cleanup_tasks"
FAR_POINT = "SRID=4326;POINT(77.2090 28.6139)"  # Delhi, far outside a 10 km radius


def _lines(caplog, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().split(" ", 1)[0] == event]


async def _funnel_log(db, booking, caplog) -> logging.LogRecord:
    """Run the real funnel + log on this session; return the one warning."""
    address = await db.get(Address, booking.consumer_address_id)
    service_ids = (
        await db.scalars(select(BookingItem.service_id).where(BookingItem.booking_id == booking.id))
    ).all()
    with caplog.at_level(logging.WARNING, logger=NOTIFY):
        await db.run_sync(lambda s: _log_no_providers(s, booking, service_ids, address))
    [record] = _lines(caplog, "dispatch.no_providers")
    return record


def _counts(record) -> dict[str, int]:
    return record.args[-1]


async def _switch_off(db, sp):
    """The SP stops taking jobs itself (SVC-326), set directly."""
    self_pro = await ProviderService(db).ensure_self_professional(sp.id)
    self_pro.accepts_direct_jobs = False
    await db.flush()


# ---------------------------------------------------------------------------
# One reason per stage
# ---------------------------------------------------------------------------


async def _ineligible(db, service, how: str):
    """An SP who passes every stage before ``how`` and fails ``how``."""
    if how == "kyc":
        return await _eligible_provider(db, service, kyc=KYCStatus.PENDING)
    if how == "token":
        return await _eligible_provider(db, service, has_token=False)
    if how == "offline":
        return await _eligible_provider(db, service, online=False)
    if how == "far":
        return await _eligible_provider(db, service, point=FAR_POINT)
    if how == "no_center":
        return await _eligible_provider(db, service, point=None)
    sp = await _eligible_provider(db, service)
    if how == "inactive":
        sp.is_active = False
    elif how == "self_off_no_team":
        await _switch_off(db, sp)
    elif how == "self_off_team_offline":
        await _switch_off(db, sp)
        await _team_pro(db, sp.id, online=False)
    await db.flush()
    return sp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("how", "reason", "surviving_stages"),
    [
        ("unsubscribed", "no_provider_covers_services", 0),
        ("kyc", "none_kyc_verified", 1),
        ("inactive", "none_account_active", 2),
        ("token", "none_has_push_token", 3),
        ("offline", "none_online", 4),
        ("far", "none_in_service_area", 5),
        # The gap we expect on beta: the app never sets a service-area center,
        # and ST_DWithin on NULL is NULL, which FILTER treats as false.
        ("no_center", "none_in_service_area", 5),
        ("self_off_no_team", "none_has_someone_to_do_it", 6),
        ("self_off_team_offline", "none_has_someone_to_do_it", 6),
    ],
)
async def test_each_stage_is_named_when_it_rules_everyone_out(
    db_session, caplog, how, reason, surviving_stages
):
    service = await _service(db_session)
    if how == "unsubscribed":
        await _eligible_provider(db_session, service, subscribed=False)
    else:
        await _ineligible(db_session, service, how)
    _, booking = await _booking(db_session, service=service)

    record = await _funnel_log(db_session, booking, caplog)

    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert f"booking_id={booking.id} " in message
    assert f"reason={reason} " in message
    # Rounded to ~1 km (SVC-302); the address is at 12.9784, 77.6408.
    assert " lat=12.98 lng=77.64 " in message
    counts = _counts(record)
    assert list(counts.values()) == [1] * surviving_stages + [0] * (8 - surviving_stages)


@pytest.mark.asyncio
async def test_all_already_answered(db_session, caplog):
    """A reopened booking whose only eligible SP already answered it."""
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    _, booking = await _booking(db_session, service=service)
    db_session.add(
        BookingOffer(
            booking_id=booking.id,
            provider_id=sp.id,
            response=OfferResponse.DECLINED,
            responded_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    record = await _funnel_log(db_session, booking, caplog)

    assert "reason=all_already_answered " in record.getMessage()
    assert list(_counts(record).values()) == [1] * 7 + [0]


@pytest.mark.asyncio
async def test_a_cart_needs_an_sp_covering_every_service(db_session, caplog):
    first, second = await _service(db_session), await _service(db_session)
    await _eligible_provider(db_session, first)  # covers only one of the two
    consumer = await UserFactory.create(db_session, role="consumer")
    address = Address(
        user_id=consumer.id, label="home", full_address="1 MG Road", location=ADDRESS_POINT
    )
    db_session.add(address)
    await db_session.flush()
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        item_service_ids=[first.id, second.id],
        address_id=address.id,
        status=BookingStatus.REQUESTED,
    )

    record = await _funnel_log(db_session, booking, caplog)

    assert "reason=no_provider_covers_services " in record.getMessage()
    assert _counts(record)["covers_services"] == 0


@pytest.mark.asyncio
async def test_the_funnel_agrees_with_the_broadcast(db_session, caplog):
    """A mix of SPs, two of them truly eligible: one plain, one switched off with
    an assignable team Pro. The funnel's last stage must be exactly the
    broadcast set, or it has drifted from real eligibility (or a correlated
    EXISTS bound to the wrong table)."""
    service = await _service(db_session)
    for how in ("kyc", "inactive", "token", "offline", "far", "no_center", "self_off_no_team"):
        await _ineligible(db_session, service, how)
    await _eligible_provider(db_session, service, subscribed=False)
    plain = await _eligible_provider(db_session, service)
    team_owner = await _eligible_provider(db_session, service)
    await _switch_off(db_session, team_owner)
    await _team_pro(db_session, team_owner.id)
    _, booking = await _booking(db_session, service=service)
    address = await db_session.get(Address, booking.consumer_address_id)

    broadcast = (
        await db_session.scalars(
            broadcast_recipients_stmt([service.id], address.location, booking.id)
        )
    ).all()
    record = await _funnel_log(db_session, booking, caplog)

    assert {u.id for u in broadcast} == {plain.id, team_owner.id}
    assert _counts(record) == {
        "covers_services": 9,
        "kyc_verified": 8,
        "account_active": 7,
        "has_push_token": 6,
        "online": 5,
        "in_service_area": 3,
        "has_someone_to_do_it": 2,
        "not_already_answered": 2,
    }
    assert "reason=unknown " in record.getMessage()


# ---------------------------------------------------------------------------
# Through the real Celery task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_task_logs_why_before_it_expires(db_session, committed, caplog):  # noqa: F811
    consumer = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )
    committed.extend([booking, addr, consumer])
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=NOTIFY):
        _run_broadcast(booking.id)

    assert await _status_of(db_session, booking) == BookingStatus.EXPIRED
    events = [r.getMessage().split(" ", 1)[0] for r in caplog.records if r.name == NOTIFY]
    assert events == ["broadcast.recipients", "dispatch.no_providers", "booking.unbroadcastable"]
    [record] = _lines(caplog, "dispatch.no_providers")
    assert record.levelno == logging.WARNING
    # PUNE in test_unbroadcastable_booking: 18.5204, 73.8567.
    assert "reason=no_provider_covers_services lat=18.52 lng=73.86 " in record.getMessage()


@pytest.mark.asyncio
async def test_a_paid_booking_kept_searching_is_logged_too(db_session, committed, caplog):  # noqa: F811
    consumer = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
        payment_status=PaymentStatus.PAID,
    )
    committed.extend([booking, addr, consumer])
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=NOTIFY):
        _run_broadcast(booking.id)

    assert await _status_of(db_session, booking) == BookingStatus.REQUESTED
    assert len(_lines(caplog, "dispatch.no_providers")) == 1
    assert len(_lines(caplog, "booking.unbroadcastable_paid_kept")) == 1


@pytest.mark.asyncio
async def test_a_normal_broadcast_runs_no_funnel(db_session, committed, caplog):  # noqa: F811
    from app.models.provider_profile import ProviderProfile
    from app.utils.geo import build_point_wkt
    from tests.test_unbroadcastable_booking import PUNE

    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(
        db_session, role="provider", fcm_token=f"fcm-{uuid.uuid4().hex[:8]}"
    )
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )
    db_session.add(
        ProviderProfile(
            user_id=provider.id,
            is_online=True,
            kyc_status=KYCStatus.VERIFIED,
            service_area_center=build_point_wkt(*PUNE),
            service_area_radius_km=25,
        )
    )
    await db_session.flush()
    for service_id in (
        await db_session.scalars(
            select(BookingItem.service_id).where(BookingItem.booking_id == booking.id)
        )
    ).all():
        db_session.add(ProviderServiceModel(provider_id=provider.id, service_id=service_id))
    committed.extend([booking, addr, provider, consumer])
    await db_session.commit()

    with (
        patch("app.services.provider_matching.broadcast_funnel") as funnel,
        caplog.at_level(logging.INFO, logger=NOTIFY),
    ):
        _run_broadcast(booking.id)

    funnel.assert_not_called()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "count=1" in _lines(caplog, "broadcast.recipients")[0].getMessage()


@pytest.mark.asyncio
async def test_a_funnel_failing_in_postgres_still_expires(db_session, committed, caplog):  # noqa: F811
    """Failing inside Postgres aborts the transaction, not just the Python call;
    without the SAVEPOINT the EXPIRED commit would fail too and the booking
    would sit at REQUESTED."""
    consumer = await UserFactory.create(db_session, role="consumer")
    addr = await _address(db_session, consumer.id)
    booking = await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=None,
        address_id=addr.id,
        status=BookingStatus.REQUESTED,
    )
    committed.extend([booking, addr, consumer])
    await db_session.commit()

    def broken_funnel(db, *_):
        db.execute(text("SELECT * FROM no_such_table_svc349"))

    with (
        patch("app.services.provider_matching.broadcast_funnel", broken_funnel),
        caplog.at_level(logging.INFO, logger=NOTIFY),
    ):
        _run_broadcast(booking.id)

    assert await _status_of(db_session, booking) == BookingStatus.EXPIRED
    [failure] = _lines(caplog, "dispatch.no_providers_funnel_failed")
    assert failure.levelno == logging.ERROR and failure.exc_info
    assert not _lines(caplog, "dispatch.no_providers")
    assert len(_lines(caplog, "booking.unbroadcastable")) == 1


# ---------------------------------------------------------------------------
# Offers went out, nobody took it: the sweep
# ---------------------------------------------------------------------------


def _offers(sync_factory, made, booking_id, *responses):
    """One offer row per response (None = never answered), each to a new SP."""
    from app.models.user import User, UserRole

    with sync_factory() as db:
        for response in responses:
            sp = User(phone=f"+9198{uuid.uuid4().int % 10**8:08d}", role=UserRole.PROVIDER)
            db.add(sp)
            db.flush()
            made["users"].append(sp.id)
            db.add(BookingOffer(booking_id=booking_id, provider_id=sp.id, response=response))
        db.commit()


def _no_acceptance(caplog) -> dict[str, str]:
    return {
        str(r.args[0]): r.getMessage()
        for r in _lines(caplog, "dispatch.no_acceptance")
        if r.levelno == logging.WARNING
    }


def test_each_expired_booking_says_how_many_offers_went_unanswered(committed_sweep_world, caplog):  # noqa: F811
    sync_factory, made = committed_sweep_world
    declined = OfferResponse.DECLINED
    offered = _seed(sync_factory, made, paid=False)
    _offers(sync_factory, made, offered, None, declined, None)
    nobody = _seed(sync_factory, made, paid=False)
    cash = _seed(sync_factory, made, paid=False, method=PaymentMethod.CASH)
    _offers(sync_factory, made, cash, declined)
    fresh = _seed(sync_factory, made, paid=False, age=5)

    with caplog.at_level(logging.WARNING, logger=CLEANUP):
        _sweep()

    for booking_id in (offered, nobody, cash):
        assert _get(sync_factory, booking_id).status is BookingStatus.EXPIRED
    assert _get(sync_factory, fresh).status is BookingStatus.REQUESTED
    lines = _no_acceptance(caplog)
    assert lines[str(offered)] == (
        f"dispatch.no_acceptance booking_id={offered} offers_sent=3 declined=1 "
        "reopened=False outcome=expired"
    )
    assert "offers_sent=0 declined=0 " in lines[str(nobody)]
    assert "offers_sent=1 declined=1 " in lines[str(cash)]
    assert str(fresh) not in lines


def test_a_paid_booking_is_logged_with_what_the_sweep_did(committed_sweep_world, caplog):  # noqa: F811
    """Counted before the second offer, whose broadcast adds offer rows."""
    sync_factory, made = committed_sweep_world
    paid = _seed(sync_factory, made)
    _offers(sync_factory, made, paid, OfferResponse.DECLINED, None)

    with caplog.at_level(logging.WARNING, logger=CLEANUP):
        _sweep()

    assert _get(sync_factory, paid).last_chance_at is not None
    assert _no_acceptance(caplog)[str(paid)] == (
        f"dispatch.no_acceptance booking_id={paid} offers_sent=2 declined=1 "
        "reopened=False outcome=second_offer"
    )


def test_a_counting_failure_in_postgres_never_blocks_the_sweep(committed_sweep_world, caplog):  # noqa: F811
    """The count query fails inside Postgres. Expiry is already committed, the
    session must still serve the paid query, and paid bookings still advance."""
    from sqlalchemy import event

    sync_factory, made = committed_sweep_world
    unpaid = _seed(sync_factory, made, paid=False)
    paid = _seed(sync_factory, made)
    engine = sync_factory.kw["bind"]

    def break_counts(conn, cursor, statement, parameters, context, executemany):
        if "count(booking_offers.id)" in statement:
            return "SELECT * FROM no_such_table_svc349", parameters
        return statement, parameters

    event.listen(engine, "before_cursor_execute", break_counts, retval=True)
    try:
        with caplog.at_level(logging.WARNING, logger=CLEANUP):
            summary, _, _ = _sweep()
    finally:
        event.remove(engine, "before_cursor_execute", break_counts)

    assert _get(sync_factory, unpaid).status is BookingStatus.EXPIRED
    assert _get(sync_factory, paid).last_chance_at is not None
    assert "1 second offer" in summary
    assert len(_lines(caplog, "dispatch.no_acceptance_count_failed")) == 2
    assert not _no_acceptance(caplog)
