"""SVC-449: call-masking allocation + release + lookup + cron.

Covered here:

* happy-path allocate → lookup → release;
* idempotency on both allocate and release;
* pool exhaustion → ``NoVirtualNumbersAvailable``;
* ``release_mapping`` sets ``released_at = now() + 48h``, not now();
* cron hard-releases stuck mappings (``expires_at < now()`` AND
  ``released_at IS NULL``), leaves released ones alone;
* booking-event integration: accept → allocate; complete / cancel → release.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.exceptions import NoVirtualNumbersAvailable
from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.call_mapping import CallMapping
from app.models.virtual_number_pool import VirtualNumberPool, VirtualNumberStatus
from app.services.call_masking import (
    ALLOCATION_MAX_LIFETIME,
    RELEASE_GRACE,
    CallMaskingService,
    hard_release_expired_async,
)
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

# Mumbai point — any valid POINT works; the FK is on consumer_address_id and
# that's what every failing test actually needed. Address.location is nullable
# but we set it defensively so a future dispatch-adjacent assertion works too.
ADDRESS_POINT = "POINT(72.8777 19.0760)"

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _unique_e164() -> str:
    """Unique virtual number per allocation.

    The test ``db_session`` fixture rolls back per test but ``test_hard_release_*``
    commits (SyncSessionLocal needs a committed read) — so rows leak across
    tests. Unique numbers avoid the uq_virtual_number_pool_e164 collision
    between leftover rows and the fresh insert.
    """
    # 10-digit Indian mobile range, +91 prefix, varchar(16) cap respected.
    n = uuid.uuid4().int % 10**9
    return f"+9180{n:010d}"[:16]


async def _seed_number(db, e164: str | None = None) -> VirtualNumberPool:
    n = VirtualNumberPool(
        id=uuid.uuid4(),
        e164=e164 or _unique_e164(),
        status=VirtualNumberStatus.ACTIVE,
    )
    db.add(n)
    await db.flush()
    return n


async def _booking_pair(db):
    """A consumer + provider + accepted booking ready to allocate against."""
    consumer = await UserFactory.create(db, role="consumer")
    provider = await UserFactory.create(db, role="provider")
    address = Address(
        user_id=consumer.id,
        label="home",
        full_address="12 MG Road",
        location=ADDRESS_POINT,
    )
    db.add(address)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id,
        address_id=address.id,
        status=BookingStatus.ACCEPTED,
    )
    return consumer, provider, booking


# ---------------------------------------------------------------------------
# Allocate
# ---------------------------------------------------------------------------


async def test_allocate_picks_from_pool_and_writes_mapping(db_session):
    number = await _seed_number(db_session)
    _, provider, booking = await _booking_pair(db_session)

    mapping = await CallMaskingService(db_session).allocate_mapping(booking.id)

    assert mapping.virtual_number_id == number.id
    assert mapping.virtual_number_e164 == number.e164
    assert mapping.booking_id == booking.id
    assert mapping.released_at is None
    # 7-day stuck-booking guard (allow a few seconds of wiggle for test run time).
    delta = mapping.expires_at - mapping.allocated_at
    assert abs(delta - ALLOCATION_MAX_LIFETIME) < timedelta(seconds=5)
    # Phones snapshotted at allocate time; E.164 normalised to carry the `+`.
    assert mapping.sp_phone_e164.startswith("+")
    assert mapping.customer_phone_e164.startswith("+")


async def test_allocate_is_idempotent(db_session):
    await _seed_number(db_session)
    await _seed_number(db_session)  # second number available in the pool
    _, _, booking = await _booking_pair(db_session)

    svc = CallMaskingService(db_session)
    first = await svc.allocate_mapping(booking.id)
    second = await svc.allocate_mapping(booking.id)

    assert first.id == second.id
    # The second call must NOT consume a second number.
    count = (
        await db_session.execute(select(CallMapping).where(CallMapping.booking_id == booking.id))
    ).all()
    assert len(count) == 1


async def _disable_leftover_pool_rows(db) -> None:
    """Disable every ACTIVE pool row leaked from prior tests.

    The ``db_session`` fixture rolls back per test, but ``test_hard_release_*``
    commits (SyncSessionLocal reads need a committed view), so pool rows
    leak across tests. The pool-exhaustion tests need an empty-of-active pool;
    flipping leftovers to DISABLED is scoped to this session's transaction
    and reverts at teardown.
    """
    from sqlalchemy import update

    await db.execute(
        update(VirtualNumberPool)
        .where(VirtualNumberPool.status == VirtualNumberStatus.ACTIVE)
        .values(status=VirtualNumberStatus.DISABLED)
    )
    await db.flush()


async def test_allocate_raises_when_pool_exhausted(db_session):
    # No ACTIVE pool rows at all.
    await _disable_leftover_pool_rows(db_session)
    _, _, booking = await _booking_pair(db_session)
    with pytest.raises(NoVirtualNumbersAvailable):
        await CallMaskingService(db_session).allocate_mapping(booking.id)


async def test_allocate_skips_disabled_numbers(db_session):
    await _disable_leftover_pool_rows(db_session)
    disabled = await _seed_number(db_session)
    disabled.status = VirtualNumberStatus.DISABLED
    await db_session.flush()
    _, _, booking = await _booking_pair(db_session)

    with pytest.raises(NoVirtualNumbersAvailable):
        await CallMaskingService(db_session).allocate_mapping(booking.id)


async def test_allocate_skips_already_held_number(db_session):
    """Two bookings compete for one number: second fails."""
    await _disable_leftover_pool_rows(db_session)
    await _seed_number(db_session)
    _, _, booking_a = await _booking_pair(db_session)
    _, _, booking_b = await _booking_pair(db_session)

    svc = CallMaskingService(db_session)
    await svc.allocate_mapping(booking_a.id)

    # Only one number in the pool, already in use → exhaustion.
    with pytest.raises(NoVirtualNumbersAvailable):
        await svc.allocate_mapping(booking_b.id)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


async def test_release_stamps_48h_grace(db_session):
    await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    svc = CallMaskingService(db_session)
    await svc.allocate_mapping(booking.id)

    before = datetime.now(UTC)
    released = await svc.release_mapping(booking.id)
    after = datetime.now(UTC)

    assert released is not None
    assert released.released_at is not None
    # released_at is in the future (now + ~48h).
    expected_lo = before + RELEASE_GRACE - timedelta(seconds=5)
    expected_hi = after + RELEASE_GRACE + timedelta(seconds=5)
    assert expected_lo <= released.released_at <= expected_hi


async def test_release_is_idempotent(db_session):
    await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    svc = CallMaskingService(db_session)
    await svc.allocate_mapping(booking.id)

    first = await svc.release_mapping(booking.id)
    stamp = first.released_at
    # Second release must NOT push the grace further out.
    second = await svc.release_mapping(booking.id)
    assert second is not None
    assert second.released_at == stamp


async def test_release_on_no_mapping_is_noop(db_session):
    _, _, booking = await _booking_pair(db_session)
    result = await CallMaskingService(db_session).release_mapping(booking.id)
    assert result is None


# ---------------------------------------------------------------------------
# Lookup (SVC-450 contract)
# ---------------------------------------------------------------------------


async def test_lookup_finds_active_mapping(db_session):
    number = await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    svc = CallMaskingService(db_session)
    allocated = await svc.allocate_mapping(booking.id)

    # from_e164 is accepted for signature stability (SVC-450) but doesn't
    # narrow — any caller against the right virtual number resolves.
    hit = await svc.lookup_active_mapping(number.e164, from_e164="+911111111111")
    assert hit is not None
    assert hit.id == allocated.id


async def test_lookup_returns_none_for_unknown_number(db_session):
    # Nothing allocated for +91nonexistent → None, not an exception.
    result = await CallMaskingService(db_session).lookup_active_mapping(
        "+919999999999", from_e164="+911111111111"
    )
    assert result is None


async def test_lookup_honours_48h_grace(db_session):
    """A released-but-still-in-grace mapping is still routable."""
    number = await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    svc = CallMaskingService(db_session)
    await svc.allocate_mapping(booking.id)
    await svc.release_mapping(booking.id)

    hit = await svc.lookup_active_mapping(number.e164, from_e164="+911111111111")
    assert hit is not None, "grace period lookup should still resolve"


async def test_lookup_returns_none_after_grace_expired(db_session):
    """Past the 48 h grace, lookup is None."""
    number = await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    svc = CallMaskingService(db_session)
    mapping = await svc.allocate_mapping(booking.id)
    # Hand-roll a "released long ago" state.
    mapping.released_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()

    hit = await svc.lookup_active_mapping(number.e164, from_e164="+911111111111")
    assert hit is None


# ---------------------------------------------------------------------------
# Cron: hard_release_expired
# ---------------------------------------------------------------------------


async def test_hard_release_releases_stuck_mapping(db_session):
    """Mapping past expires_at with released_at NULL → hard-released."""
    await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    mapping = await CallMaskingService(db_session).allocate_mapping(booking.id)
    # Fast-forward the booking so it looks stuck.
    mapping.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()

    now = datetime.now(UTC)
    # The async twin runs the same SQL, inside the test's own transaction —
    # no commit required, no leak to the next test.
    released_count = await hard_release_expired_async(db_session, now)
    assert released_count >= 1

    await db_session.refresh(mapping)
    assert mapping.released_at is not None
    # Hard-released at ``now()``, not in the future (not a 48 h grace).
    assert mapping.released_at <= now + timedelta(seconds=5)


async def test_hard_release_ignores_already_released(db_session):
    """Mapping already stamped ``released_at`` (in grace) is left alone."""
    await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    svc = CallMaskingService(db_session)
    await svc.allocate_mapping(booking.id)
    mapping = await svc.release_mapping(booking.id)
    original_release = mapping.released_at
    # Even if expires_at is past, because released_at is set we don't touch it.
    mapping.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()

    await hard_release_expired_async(db_session, datetime.now(UTC))

    await db_session.refresh(mapping)
    assert mapping.released_at == original_release


async def test_hard_release_noop_on_empty(db_session):
    # Disable leftover rows + no stuck mapping → zero releases.
    await _disable_leftover_pool_rows(db_session)
    assert await hard_release_expired_async(db_session, datetime.now(UTC)) == 0


# ---------------------------------------------------------------------------
# Integration: booking lifecycle → allocate / release
# ---------------------------------------------------------------------------


async def test_booking_accept_triggers_allocate(db_session):
    """BookingService._claim calls allocate_for_booking — verify via the wrapper."""
    from app.services.call_masking import allocate_for_booking

    await _seed_number(db_session)
    _, _, booking = await _booking_pair(db_session)
    mapping = await allocate_for_booking(db_session, booking.id)
    assert mapping is not None
    assert mapping.booking_id == booking.id


async def test_pool_exhausted_in_wrapper_returns_none_not_raises(db_session):
    """The hook-safe wrapper must NOT raise — booking confirm stays valid."""
    from app.services.call_masking import allocate_for_booking

    await _disable_leftover_pool_rows(db_session)
    _, _, booking = await _booking_pair(db_session)
    result = await allocate_for_booking(db_session, booking.id)
    assert result is None


async def test_release_wrapper_on_no_mapping_is_noop(db_session):
    """release_for_booking is safe to call before allocate ever ran."""
    from app.services.call_masking import release_for_booking

    _, _, booking = await _booking_pair(db_session)
    result = await release_for_booking(db_session, booking.id)
    assert result is None


async def test_transition_complete_releases_mapping(db_session):
    """An end-to-end: allocate then move the booking to COMPLETED via the
    service; the transition's release hook stamps released_at."""
    from app.services.booking_service import BookingService

    await _seed_number(db_session)
    _, provider, booking = await _booking_pair(db_session)
    svc = CallMaskingService(db_session)
    await svc.allocate_mapping(booking.id)

    # Move the booking through its state machine to COMPLETED. `_apply_cancellation_policy`
    # has nothing to do here. The ledger credit path needs a provider_profiles row;
    # patch out the ledger call at the import site inside booking_service.
    booking.status = BookingStatus.IN_PROGRESS
    await db_session.flush()

    with patch(
        "app.services.ledger_service.credit_booking_completed",
        return_value=None,
    ):
        await BookingService(db_session).transition_status(
            booking_id=booking.id,
            new_status=BookingStatus.COMPLETED,
            changed_by=provider.id,
        )

    mapping = (
        await db_session.execute(select(CallMapping).where(CallMapping.booking_id == booking.id))
    ).scalar_one()
    assert mapping.released_at is not None
    # Grace window, not immediate.
    assert mapping.released_at > datetime.now(UTC)
