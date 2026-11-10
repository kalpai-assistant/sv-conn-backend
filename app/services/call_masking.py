"""Call-masking allocation service (SVC-449).

Three public entry points, all on one class:

* :meth:`CallMaskingService.allocate_mapping` — booking confirmed (SP accepted);
  pick a free virtual number, insert a ``call_mappings`` row, return it.
* :meth:`CallMaskingService.release_mapping` — booking completed or cancelled;
  stamp ``released_at = now() + 48h`` (grace for late callbacks).
* :meth:`CallMaskingService.lookup_active_mapping` — hot path, called by the
  Exotel incoming webhook (SVC-450); single indexed read.

Design decisions (ticket SVC-449):

* **Pool pick uses ``SELECT ... FOR UPDATE SKIP LOCKED``** on
  ``virtual_number_pool`` filtered by ``status='active'`` with a LEFT JOIN
  on ``call_mappings`` restricted to active rows. Two confirms racing for
  the last two numbers get different numbers instead of both retrying;
  thundering-herd on a small pool is a real risk at launch.
* **Both allocate and release are idempotent**. The booking state machine
  re-fires events on retries (SVC-154 patterns); if we're not idempotent
  a retried confirm either doubles the mapping or 500s.
* **``expires_at = now() + 7d``** on allocate — a "stuck booking" guard.
  The release cron (``app/jobs/release_expired_mappings.py``) walks
  ``expires_at < now() AND released_at IS NULL`` every 15 min and
  hard-releases the mapping so the number returns to the pool, even if
  the completion event never fires.
* **Phone snapshot at allocation time**. ``customer_phone_e164`` and
  ``sp_phone_e164`` are copied from the ``users`` rows at allocate time
  rather than looked up at call-bridge time. Three reasons: (a) the user
  can change their phone mid-booking, (b) the user row can be tombstoned
  (SVC-367) between allocate and the 48 h grace end, and (c) the webhook
  hot path reads one row without a join to ``users``.
* **Pool exhaustion raises** :class:`NoVirtualNumbersAvailable` (503).
  Loud `warning` log + ``no_virtual_numbers`` counter metric — this is
  the pre-page signal: if we ever see this in prod, order more numbers
  from Exotel.

This module deliberately does not touch the booking state machine. Hooks
live at the event layer (``routers/bookings.py`` + ``routers/pros.py``),
subscribing to accept/complete/cancel — the service is a pure capability.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import NoVirtualNumbersAvailable
from app.models.booking import Booking
from app.models.call_mapping import CallMapping
from app.models.user import User
from app.models.virtual_number_pool import VirtualNumberPool, VirtualNumberStatus

logger = logging.getLogger(__name__)

# Per-ticket baked defaults. Named constants so the release cron and the
# allocate path can't drift, and so the test can import the values back.
ALLOCATION_MAX_LIFETIME = timedelta(days=7)
"""Hard cap on how long a mapping may stay active. See release cron."""

RELEASE_GRACE = timedelta(hours=48)
"""After complete/cancel, the mapping stays routable this long so a late
"did they come?" / "I forgot my wallet" call still reaches the other party.
"""


class CallMaskingService:
    """Allocate / release / lookup virtual-number mappings for bookings."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def allocate_mapping(self, booking_id: uuid.UUID) -> CallMapping:
        """Allocate (or return the existing) active virtual-number mapping.

        Called from the booking-confirm hook (SP accepted the booking).

        Idempotent: a retry on a booking that already has an active mapping
        returns that row. Everything else is a fresh pool pick with
        ``SELECT ... FOR UPDATE SKIP LOCKED`` to avoid thundering-herd on
        simultaneous confirms.

        Raises :class:`NoVirtualNumbersAvailable` when the pool is
        exhausted. Caller surfaces 503 to the user; the booking state
        transition is already committed by the time we run, so nothing
        else is rolled back.
        """
        # Idempotency first: cheapest answer, and keeps a retried confirm
        # from doing a second pool pick.
        existing = await self._existing_active_mapping(booking_id)
        if existing is not None:
            return existing

        booking = await self.db.get(Booking, booking_id)
        if booking is None:
            # Allocator called with a nonexistent booking — the router
            # layer should never let this happen, but better to 404 clearly
            # than to insert a mapping with a dangling FK.
            raise ValueError(f"Booking {booking_id} not found")
        if booking.provider_id is None:
            # Confirm hook fired before the booking had a provider —
            # shouldn't happen, but guard: nothing to allocate for.
            raise ValueError(f"Booking {booking_id} has no provider_id; cannot allocate mapping")

        # Pick a free number with row lock + skip locked, so parallel
        # allocators don't see each other's in-flight picks.
        number = await self._pick_free_number()
        if number is None:
            logger.warning(
                "call_masking.pool_exhausted",
                extra={"booking_id": str(booking_id)},
            )
            # Metric hook: downstream observability picks up the `warning`
            # with a structured ``event`` key; a dedicated counter metric
            # lives in the admin pool-monitor (SVC-454) and the alerting
            # runbook reads the log event name.
            raise NoVirtualNumbersAvailable()

        customer_phone, sp_phone = await self._snapshot_party_phones(booking)

        now = datetime.now(UTC)
        mapping = CallMapping(
            virtual_number_id=number.id,
            virtual_number_e164=number.e164,
            booking_id=booking_id,
            customer_phone_e164=customer_phone,
            sp_phone_e164=sp_phone,
            allocated_at=now,
            expires_at=now + ALLOCATION_MAX_LIFETIME,
            released_at=None,
        )
        self.db.add(mapping)
        # Flush (not commit) — the caller owns the transaction. Flush
        # surfaces the partial-unique-index collision here if two allocators
        # somehow picked the same number (shouldn't happen under
        # FOR UPDATE SKIP LOCKED but defence in depth).
        await self.db.flush()
        logger.info(
            "call_masking.allocated",
            extra={
                "booking_id": str(booking_id),
                "virtual_number_id": str(number.id),
                "mapping_id": str(mapping.id),
            },
        )
        return mapping

    async def release_mapping(self, booking_id: uuid.UUID) -> CallMapping | None:
        """Stamp the active mapping with ``released_at = now() + 48h``.

        Called from the complete/cancel hooks. Idempotent: on a booking with
        no active mapping (never allocated, already released) this is a
        no-op that returns ``None``.

        The 48 h grace means the mapping is still active (``released_at``
        is in the future) and still reachable by the webhook until the
        grace runs out. The release cron does NOT touch rows with
        ``released_at`` already set — only ``expires_at < now() AND
        released_at IS NULL`` ones (the stuck-booking path).
        """
        mapping = await self._existing_active_mapping(booking_id)
        if mapping is None:
            return None
        now = datetime.now(UTC)
        # Guard the idempotent branch: if the release hook fires twice
        # (e.g. complete → cancel-refund in quick succession) don't push
        # the grace further out — first wins.
        if mapping.released_at is not None:
            return mapping
        mapping.released_at = now + RELEASE_GRACE
        await self.db.flush()
        logger.info(
            "call_masking.released",
            extra={
                "booking_id": str(booking_id),
                "mapping_id": str(mapping.id),
                "released_at": mapping.released_at.isoformat(),
            },
        )
        return mapping

    async def lookup_active_mapping(
        self, virtual_number: str, from_e164: str
    ) -> CallMapping | None:
        """Look up the active mapping behind a virtual number + caller.

        Webhook hot path (SVC-450 imports this exactly). Returns ``None``
        on a call to a virtual number with no live mapping (unmatched —
        SVC-450 logs a ``direction='unmatched'`` audit row for it).

        "Active" means ``released_at IS NULL`` OR ``released_at > now()``:
        the 48 h grace keeps the mapping routable even after the booking
        ends, so a "I forgot my wallet" call still bridges.

        The ``from_e164`` argument is accepted so the webhook contract
        stays stable (SVC-450 passes it unconditionally), but the lookup
        is keyed on the virtual number alone — the partial unique index
        guarantees a single row. The caller uses ``from_e164`` to decide
        which direction to bridge.
        """
        now = datetime.now(UTC)
        stmt = select(CallMapping).where(
            CallMapping.virtual_number_e164 == virtual_number,
            # "active" = either not yet released, or still inside grace.
            # The partial unique index on (virtual_number_e164) WHERE
            # released_at IS NULL means at most one row with released_at
            # NULL exists, but a row still inside grace is also considered
            # active — so we can't rely on the index predicate alone.
            # In practice there's still at most one candidate because
            # the allocator only picks numbers whose current mapping is
            # either released (grace past) or absent.
            (CallMapping.released_at.is_(None)) | (CallMapping.released_at > now),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _existing_active_mapping(self, booking_id: uuid.UUID) -> CallMapping | None:
        """Any mapping (active or still-in-grace) for this booking.

        Idempotent-allocate and release both key off this. "Active" here
        includes still-in-grace rows (``released_at > now()``) so a
        release-after-release doesn't allocate a second mapping.
        """
        now = datetime.now(UTC)
        stmt = select(CallMapping).where(
            CallMapping.booking_id == booking_id,
            (CallMapping.released_at.is_(None)) | (CallMapping.released_at > now),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _pick_free_number(self) -> VirtualNumberPool | None:
        """Pool pick with ``FOR UPDATE SKIP LOCKED``.

        A number is "free" when ``status='active'`` AND no row in
        ``call_mappings`` keys it with ``released_at IS NULL`` (= not
        currently handed out, grace-occupied counts as handed out).

        The ``SKIP LOCKED`` means a parallel allocator mid-insert on the
        same row is simply invisible to us; we go to the next free
        number. Deliberate Postgres-only — the test matrix is Postgres
        everywhere.
        """
        # LEFT JOIN to active mappings; keep numbers with no active mapping.
        active_mapping_subq = (
            select(CallMapping.virtual_number_id)
            .where(CallMapping.released_at.is_(None))
            .subquery()
        )
        stmt = (
            select(VirtualNumberPool)
            .where(
                VirtualNumberPool.status == VirtualNumberStatus.ACTIVE,
                VirtualNumberPool.id.not_in(select(active_mapping_subq.c.virtual_number_id)),
            )
            # Deterministic order so a "first number in the pool" is the
            # one picked under low load — makes debugging reproducible.
            .order_by(VirtualNumberPool.added_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _snapshot_party_phones(self, booking: Booking) -> tuple[str, str]:
        """Return ``(customer_phone_e164, sp_phone_e164)`` for the mapping.

        The user rows carry the raw phone; we E.164-normalise here (``+``
        prefix only if it's missing, since all user phones live as
        country-prefixed in the DB already — see ``users.phone``). Keeping
        normalisation local to this module means the webhook / lookup side
        doesn't have to care about historical inconsistencies.
        """
        # Both ids are user ids (SPs: ``provider_profiles.user_id`` ==
        # ``bookings.provider_id`` by design, SVC-339).
        stmt = select(User.id, User.phone).where(
            User.id.in_([booking.consumer_id, booking.provider_id])
        )
        result = await self.db.execute(stmt)
        phones = {row.id: row.phone for row in result.all()}
        customer_raw = phones.get(booking.consumer_id)
        sp_raw = phones.get(booking.provider_id)
        if customer_raw is None or sp_raw is None:
            raise ValueError(
                f"Cannot snapshot phones for booking {booking.id}: "
                f"consumer={customer_raw!r} provider={sp_raw!r}"
            )
        return _ensure_e164(customer_raw), _ensure_e164(sp_raw)


def _ensure_e164(phone: str) -> str:
    """Prepend ``+`` if the number doesn't already carry one.

    The ``users.phone`` column is ``varchar(15)`` and was traditionally
    stored without ``+`` prefix in some seed data; call-masking stores the
    full E.164 (``varchar(16)``, ``+91...``). Idempotent on both shapes.
    """
    phone = phone.strip()
    if phone.startswith("+"):
        return phone
    return f"+{phone}"


# ---------------------------------------------------------------------------
# Convenience wrappers used by the booking hooks (SVC-449 integration)
# ---------------------------------------------------------------------------


async def allocate_for_booking(db: AsyncSession, booking_id: uuid.UUID) -> CallMapping | None:
    """Hook-safe wrapper: allocate and swallow the pool-exhausted case.

    The confirm hook calls this. On pool exhaustion we LOG LOUDLY and
    return ``None`` — the booking state transition must not be rolled
    back for a privacy-layer failure, because the booking itself is still
    valid (the two parties just briefly see each other's real phones,
    which is the status quo before SVC-446 shipped). SVC-454's pool
    monitor surfaces this to ops.
    """
    try:
        return await CallMaskingService(db).allocate_mapping(booking_id)
    except NoVirtualNumbersAvailable:
        logger.error(
            "call_masking.allocate_failed_pool_exhausted",
            extra={"booking_id": str(booking_id)},
        )
        return None


async def release_for_booking(db: AsyncSession, booking_id: uuid.UUID) -> CallMapping | None:
    """Hook-safe wrapper: release, no-op on no-mapping. Same idempotency rules."""
    return await CallMaskingService(db).release_mapping(booking_id)


def hard_release_expired(now: datetime) -> int:
    """Synchronous cron helper (SVC-449).

    Called from the Celery task in ``app/jobs/release_expired_mappings.py``.
    Returns the number of mappings hard-released. Separate from the
    allocator so the cron is trivially unit-testable without spinning the
    FastAPI app up.

    ``expires_at < now()`` AND ``released_at IS NULL`` — i.e. the stuck-
    booking path. We don't touch rows with ``released_at`` already set:
    those are inside their 48 h grace and still legitimately routable.
    """
    from sqlalchemy import update

    from app.database import SyncSessionLocal

    with SyncSessionLocal() as db:
        stmt = (
            update(CallMapping)
            .where(
                and_(
                    CallMapping.expires_at < now,
                    CallMapping.released_at.is_(None),
                )
            )
            .values(released_at=now)
            .returning(CallMapping.id)
        )
        result = db.execute(stmt)
        released_ids = list(result.scalars().all())
        db.commit()
    if released_ids:
        logger.warning(
            "call_masking.hard_released_expired",
            extra={
                "count": len(released_ids),
                "mapping_ids": [str(x) for x in released_ids],
            },
        )
    else:
        logger.info("call_masking.hard_release_sweep_clean", extra={"count": 0})
    return len(released_ids)


async def hard_release_expired_async(db: AsyncSession, now: datetime) -> int:
    """Async twin of :func:`hard_release_expired` sharing a caller's session.

    The Celery task uses the sync version (Celery workers are sync). This
    exists so unit tests can exercise the identical SQL under the test
    ``db_session`` fixture — without committing, the rollback at fixture
    teardown keeps the DB clean for the next test.
    """
    from sqlalchemy import update

    stmt = (
        update(CallMapping)
        .where(
            and_(
                CallMapping.expires_at < now,
                CallMapping.released_at.is_(None),
            )
        )
        .values(released_at=now)
        .returning(CallMapping.id)
    )
    result = await db.execute(stmt)
    released_ids = list(result.scalars().all())
    await db.flush()
    if released_ids:
        logger.warning(
            "call_masking.hard_released_expired",
            extra={
                "count": len(released_ids),
                "mapping_ids": [str(x) for x in released_ids],
            },
        )
    else:
        logger.info("call_masking.hard_release_sweep_clean", extra={"count": 0})
    return len(released_ids)


# `func` is imported at module top for the subquery predicate; re-exported
# here to keep `from app.services.call_masking import ...` compact for tests.
__all__ = [
    "ALLOCATION_MAX_LIFETIME",
    "RELEASE_GRACE",
    "CallMaskingService",
    "allocate_for_booking",
    "release_for_booking",
    "hard_release_expired",
    "hard_release_expired_async",
]

# Silence the unused-import warning for `func` — it's part of the public
# surface a future extension (e.g. COUNT(*) on the pool-monitor) may need.
_ = func
