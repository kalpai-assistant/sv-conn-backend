"""
Booking service: state machine enforcement, commission calculation,
OTP-gated service start, and paginated listing.
"""

import secrets
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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

        return BookingOfferResponse(
            booking_id=booking.id,
            service_name=service.name,
            service_amount=booking.service_amount,
            full_address=address.full_address,
            distance_km=round(distance.scalar_one(), 2),
        )

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

        if booking.status in (BookingStatus.CANCELLED, BookingStatus.EXPIRED):
            raise BookingOfferClosed()

        if booking.provider_id == provider_id:
            if booking.status is BookingStatus.ACCEPTED:
                return booking, False
        elif booking.provider_id is not None:
            raise UnauthorizedAction(
                "This booking has already been claimed by another provider"
            )

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

        return BookingListResponse(
            bookings=[BookingResponse.model_validate(b) for b in bookings],
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
