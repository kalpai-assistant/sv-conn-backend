"""
Booking service: state machine enforcement, commission calculation,
OTP-gated service start, and paginated listing.
"""

import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.exceptions import (
    BookingNotFound,
    BookingOfferClosed,
    InvalidOTP,
    InvalidStateTransition,
    UnauthorizedAction,
)
from app.models.address import Address
from app.models.booking import (
    VALID_TRANSITIONS,
    Booking,
    BookingStatus,
    PaymentStatus,
)
from app.models.booking_status_log import BookingStatusLog
from app.models.provider_profile import ProviderProfile
from app.models.service import Service
from app.models.user import UserRole
from app.schemas.booking import (
    BookingListResponse,
    BookingOfferResponse,
    BookingResponse,
)
from app.services.provider_matching import is_eligible_provider_stmt
from app.utils.geo import distance_km_to_point

settings = get_settings()


class BookingService:
    """Core booking domain: creation, state transitions, and listing."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Address hydration
    # ------------------------------------------------------------------

    async def attach_addresses(self, bookings: Iterable[Booking]) -> None:
        """Load each booking's consumer address onto it, ready for serialisation.

        Every path that returns a ``BookingResponse`` has to call this first:
        ``BookingResponse.address`` reads ``Booking.consumer_address`` (SVC-170),
        and that relationship is declared ``lazy="raise_on_sql"`` so a path that
        forgets fails by name here instead of by ``MissingGreenlet`` inside
        pydantic.

        Fetching explicitly rather than with ``selectinload`` on the query is
        deliberate, and the two are not interchangeable at these call sites.
        ``Session.get`` answers from the identity map when it can, and a load that
        emits no SQL applies no loader options — so ``get(Booking, id,
        options=[selectinload(...)])`` silently leaves the relationship unloaded
        for any booking already in the session, which is every booking in a test
        and any booking read twice in one request. This module's existing habit of
        an explicit ``db.get(Address, ...)`` does not have that failure mode, and
        this is the batched form of it.

        One query regardless of page size, so the 50-row list route does not turn
        into 50 round-trips. ``set_committed_value`` marks the relationship
        *already loaded* rather than *changed*, which is what keeps this read-path
        hydration out of the unit of work — plain assignment would enrol every
        serialised booking in the next flush.
        """
        bookings = list(bookings)
        address_ids = {b.consumer_address_id for b in bookings if b.consumer_address_id}
        if not address_ids:
            return

        rows = await self.db.execute(select(Address).where(Address.id.in_(address_ids)))
        by_id = {address.id: address for address in rows.scalars()}
        for booking in bookings:
            set_committed_value(booking, "consumer_address", by_id.get(booking.consumer_address_id))

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_booking(
        self,
        consumer_id: uuid.UUID,
        service_id: uuid.UUID,
        address_id: uuid.UUID,
        scheduled_at: datetime | None = None,
        payment_method: str = "online",
    ) -> Booking:
        """Create a new booking and compute the 80/20 commission split."""
        service = await self.db.get(Service, service_id)
        if not service or not service.is_active:
            raise ValueError("Service not found or inactive")

        commission_rate = settings.PLATFORM_COMMISSION_PERCENT / 100
        service_amount = service.base_price
        platform_commission = round(service_amount * commission_rate, 2)
        provider_payout = round(service_amount - platform_commission, 2)

        booking = Booking(
            consumer_id=consumer_id,
            provider_id=None,  # Unassigned until a provider accepts the broadcast
            service_id=service_id,
            consumer_address_id=address_id,
            status=BookingStatus.REQUESTED,
            scheduled_at=scheduled_at,
            doorstep_otp=self._generate_doorstep_otp(),
            service_amount=service_amount,
            platform_commission=platform_commission,
            provider_payout=provider_payout,
            payment_method=payment_method,
        )
        self.db.add(booking)
        await self.db.flush()

        # The 201 carries the address like every other booking response (SVC-170).
        await self.attach_addresses([booking])

        await self._log_transition(
            booking_id=booking.id,
            from_status="none",
            to_status=BookingStatus.REQUESTED.value,
            changed_by=consumer_id,
        )

        return booking

    # ------------------------------------------------------------------
    # Fetch single booking
    # ------------------------------------------------------------------

    async def get_booking(self, booking_id: uuid.UUID, user_id: uuid.UUID) -> Booking:
        """Fetch a booking, enforcing that caller is the consumer or assigned provider."""
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)
        if user_id not in (booking.consumer_id, booking.provider_id):
            raise UnauthorizedAction("You do not have access to this booking")
        # After the access check, not before: a caller who is refused never
        # causes a lookup of the address they were refused.
        await self.attach_addresses([booking])
        return booking

    async def get_offer(
        self, booking_id: uuid.UUID, provider_id: uuid.UUID
    ) -> BookingOfferResponse:
        """What a broadcast recipient may see about a job before accepting it.

        ``get_booking`` cannot serve this: it admits only the consumer or the
        *assigned* provider, and a broadcast recipient is neither — ``provider_id``
        stays NULL until someone accepts. Without this the provider gets a push
        carrying a booking id they are forbidden to resolve (SVC-153).

        Eligibility is checked **before** the still-open check, deliberately. The
        reverse order would tell any caller whether a given booking exists and has
        been taken; this way a provider who was never a recipient learns nothing
        beyond 403.
        """
        booking = await self.db.get(Booking, booking_id)
        if not booking:
            raise BookingNotFound(booking_id)

        address = await self.db.get(Address, booking.consumer_address_id)
        if address is None or address.location is None:
            # The broadcast bails out on the same condition, so nobody was ever
            # offered this booking — answer as we would to any non-recipient.
            raise UnauthorizedAction("You were not offered this booking")

        eligible = await self.db.execute(
            is_eligible_provider_stmt(booking.service_id, address.location, provider_id)
        )
        if eligible.scalar_one_or_none() is None:
            raise UnauthorizedAction("You were not offered this booking")

        # Open means REQUESTED — the only state a booking can be accepted from.
        # Anything else (claimed, cancelled, expired) ends the offer.
        if booking.status is not BookingStatus.REQUESTED:
            raise BookingOfferClosed()

        service = await self.db.get(Service, booking.service_id)

        distance = await self.db.execute(
            select(
                distance_km_to_point(ProviderProfile.service_area_center, address.location)
            ).where(ProviderProfile.user_id == provider_id)
        )

        expires_at, seconds_remaining = self._offer_expiry(booking)

        return BookingOfferResponse(
            booking_id=booking.id,
            service_name=service.name,
            service_amount=booking.service_amount,
            full_address=address.full_address,
            distance_km=round(distance.scalar_one(), 2),
            expires_at=expires_at,
            seconds_remaining=seconds_remaining,
        )

    @staticmethod
    def _offer_expiry(booking: Booking) -> tuple[datetime, int]:
        """When this offer dies, and how long the card has left (SVC-166).

        Derived from ``created_at`` + the configured timeout — the same pair
        ``expire_stale_bookings`` sweeps on — so changing the timeout moves both
        the real expiry and what we advertise. Nothing is stored; there is no
        column and no migration behind this.

        Two deliberate choices:

        * The nominal deadline is reported, not the worst case. The sweep runs
          every 30s, so a booking can survive up to ``timeout + 30s`` in practice.
          Advertising that would promise time the server may not honour; the card
          giving up a little early is the safe direction.
        * ``seconds_remaining`` is floored at zero. Inside that sweep lag a
          booking is past its deadline but still REQUESTED, so this endpoint
          still serves it — a negative countdown would be nonsense, and 0 tells
          the card to dismiss.
        """
        timeout = timedelta(seconds=get_settings().BOOKING_REQUEST_TIMEOUT_SECONDS)
        expires_at = booking.created_at + timeout
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        return expires_at, max(0, int(remaining))

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def accept_booking(
        self, booking_id: uuid.UUID, provider_id: uuid.UUID
    ) -> tuple[Booking, bool]:
        """Claim a broadcast booking. Returns ``(booking, newly_accepted)``.

        Accept needs outcomes the generic state machine cannot express, so it gets
        its own entry point rather than more special cases inside
        ``transition_status`` — whose signature five other routes depend on
        (SVC-154).

        The ``newly_accepted`` flag exists for one reason: the router notifies the
        consumer after a successful accept, and an idempotent retry must not fire
        that push a second time. Returning a bare booking would make a lost
        response send "Booking Confirmed" again on every retry — worse than the
        error it replaces.

        Order matters, and each branch is a case the card has to tell apart:

        * cancelled/expired first, so a provider re-accepting a booking they had
          claimed and which was later cancelled gets 410, not a stale 200;
        * then their own accepted booking → idempotent, no state change, no push;
        * then anyone else's → 403, the lost race. Checked before the transition
          guard, which would otherwise always fire first and report the useless
          "cannot move from accepted to accepted".

        A provider whose booking has moved past ACCEPTED (en route, in progress)
        falls through to the transition guard and still gets 409 — re-accepting
        there is a genuinely illegal move, not a retry.
        """
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)

        # SVC-168: eligibility check before we reveal anything about the booking.
        # `get_offer` uses the same shared predicate (`is_eligible_provider_stmt`),
        # so the set of providers who may accept can never drift from the set who
        # were offered — the invariant SVC-153 established.
        #
        # Skip the check when the caller already owns the booking: they were
        # eligible when they accepted, and re-checking would refuse a legitimate
        # idempotent retry from a provider who has since gone offline / had KYC
        # revoked / unsubscribed from the service.
        if booking.provider_id != provider_id:
            address = await self.db.get(Address, booking.consumer_address_id)
            if address is None or address.location is None:
                # Address gone / never geocoded → the broadcast bailed too, so
                # nobody was ever offered this. Refuse like any non-recipient.
                raise UnauthorizedAction("You were not offered this booking")
            eligible = await self.db.execute(
                is_eligible_provider_stmt(booking.service_id, address.location, provider_id)
            )
            if eligible.scalar_one_or_none() is None:
                # 403 here is correct — this is "you should never have been able
                # to do this" (SVC-168), which is different from "you lost the
                # race" (410, below) or "the booking's over" (also 410). All
                # three are distinguishable by status code alone.
                raise UnauthorizedAction("You were not offered this booking")

        if booking.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED):
            raise BookingOfferClosed()

        if booking.provider_id == provider_id:
            if booking.status is BookingStatus.ACCEPTED:
                # Idempotent retry: returns without reaching `transition_status`,
                # so this is the one accept path that has to hydrate for itself.
                await self.attach_addresses([booking])
                return booking, False
        elif booking.provider_id is not None:
            # SVC-167: another provider won the race. From the card's point of
            # view this is the same outcome as cancelled/expired — the offer is
            # gone, dismiss it quietly. Was 403; the code drift with the
            # cancelled/expired path forced the frontend into
            # `if (403 || 410) dismiss()`. 410 unifies the vocabulary.
            raise BookingOfferClosed()

        booking = await self.transition_status(
            booking_id=booking_id,
            new_status=BookingStatus.ACCEPTED,
            changed_by=provider_id,
        )
        return booking, True

    async def transition_status(
        self,
        booking_id: uuid.UUID,
        new_status: BookingStatus,
        changed_by: uuid.UUID,
        otp: str | None = None,
        cancellation_reason: str | None = None,
        metadata: dict | None = None,
    ) -> Booking:
        """
        Enforce state machine transitions.
        Raises InvalidStateTransition if the move is illegal.
        """
        # Acquire a row-level DB lock to safely handle concurrent acceptance attempts
        booking = await self.db.get(Booking, booking_id, with_for_update=True)
        if not booking:
            raise BookingNotFound(booking_id)

        allowed = VALID_TRANSITIONS.get(booking.status, [])
        if new_status not in allowed:
            raise InvalidStateTransition(
                f"Cannot move from {booking.status.value} to {new_status.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )

        # Broadcasting logic: The first provider to accept claims the booking globally
        if new_status == BookingStatus.ACCEPTED:
            if booking.provider_id is None:
                booking.provider_id = changed_by
            elif booking.provider_id != changed_by:
                raise UnauthorizedAction(
                    "This booking has already been claimed by another provider"
                )
        elif changed_by not in (booking.provider_id, booking.consumer_id):
            # For other transitions (start, complete), only the assigned parts can mutate
            raise UnauthorizedAction(
                "Only the assigned provider or consumer can perform this action"
            )

        # OTP gate for doorstep service start
        if new_status == BookingStatus.IN_PROGRESS:
            if not otp or otp != booking.doorstep_otp:
                raise InvalidOTP("Doorstep OTP does not match")

        if new_status == BookingStatus.CANCELLED:
            booking.cancellation_reason = cancellation_reason

        old_status = booking.status
        booking.status = new_status

        # Zero the doorstep OTP as soon as the transition it protects has
        # happened. Consumer's view of the OTP is stale from this point
        # anyway, and there's no legitimate reason to keep a plaintext
        # 4-digit secret live in the DB row for the life of the booking —
        # a DB dump would expose every used OTP alongside consumer id.
        # Same treatment on CANCELLED (the OTP is now meaningless).
        if new_status in (
            BookingStatus.IN_PROGRESS,
            BookingStatus.COMPLETED,
            BookingStatus.CANCELLED,
        ):
            booking.doorstep_otp = None

        if new_status == BookingStatus.COMPLETED:
            booking.payment_status = PaymentStatus.PENDING
            # Fix #6: increment provider completed jobs counter
            await self._increment_completed_jobs(booking.provider_id)

        await self._log_transition(
            booking_id=booking.id,
            from_status=old_status.value,
            to_status=new_status.value,
            changed_by=changed_by,
            metadata=metadata,
        )

        # Every transition route answers with a BookingResponse (SVC-170).
        await self.attach_addresses([booking])

        return booking

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def list_bookings(
        self,
        user_id: uuid.UUID,
        role: UserRole,
        status_filter: str | None,
        page: int,
        per_page: int,
    ) -> BookingListResponse:
        if role == UserRole.CONSUMER:
            where = Booking.consumer_id == user_id
        else:
            where = Booking.provider_id == user_id

        stmt = select(Booking).where(where)
        count_stmt = select(func.count()).select_from(Booking).where(where)

        if status_filter:
            try:
                status_enum = BookingStatus(status_filter)
                stmt = stmt.where(Booking.status == status_enum)
                count_stmt = count_stmt.where(Booking.status == status_enum)
            except ValueError:
                pass  # Ignore unknown status filters rather than crashing

        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = stmt.offset((page - 1) * per_page).limit(per_page)
        result = await self.db.execute(stmt)
        bookings = list(result.scalars().all())

        # One query for the whole page, not one per row — the address is on every
        # row of a provider's job list, and this route serves up to 50 (SVC-170).
        await self.attach_addresses(bookings)

        return BookingListResponse(
            # Per-row redaction, not a role check: `role` says what kind of user
            # is asking, `for_viewer` asks whether *this* caller owns the booking
            # they're being shown. A provider's page is filtered to their own
            # jobs, so every row here is one where they are not the consumer and
            # the PIN must be blanked (SVC-169).
            bookings=[BookingResponse.for_viewer(b, user_id) for b in bookings],
            total=total,
            page=page,
            per_page=per_page,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _log_transition(
        self,
        booking_id: uuid.UUID,
        from_status: str,
        to_status: str,
        changed_by: uuid.UUID,
        metadata: dict | None = None,
    ) -> None:
        log = BookingStatusLog(
            booking_id=booking_id,
            from_status=from_status,
            to_status=to_status,
            changed_by=changed_by,
            metadata_=metadata or {},
        )
        self.db.add(log)

    async def _increment_completed_jobs(self, provider_user_id: uuid.UUID) -> None:
        """Atomically increment total_completed_jobs on the provider's profile."""
        from sqlalchemy import update

        await self.db.execute(
            update(ProviderProfile)
            .where(ProviderProfile.user_id == provider_user_id)
            .values(total_completed_jobs=ProviderProfile.total_completed_jobs + 1)
        )

    @staticmethod
    def _generate_doorstep_otp() -> str:
        return "".join(str(secrets.randbelow(10)) for _ in range(4))
