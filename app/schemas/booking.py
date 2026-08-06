from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from app.models.booking import Booking


class CreateBookingRequest(BaseModel):
    service_id: UUID
    address_id: UUID
    scheduled_at: datetime | None = None  # None = instant booking
    payment_method: str = Field(default="online", pattern=r"^(cash|online)$")


class BookingResponse(BaseModel):
    id: UUID
    consumer_id: UUID
    provider_id: UUID | None
    service_id: UUID
    status: str
    service_amount: float
    platform_commission: float
    provider_payout: float
    payment_method: str | None
    payment_status: str
    # Only visible to the consumer — and, unlike before SVC-169, actually
    # enforced. Build this schema with `for_viewer`, never `model_validate`,
    # on any path that can be reached by someone other than the consumer.
    doorstep_otp: str | None = None
    scheduled_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def for_viewer(cls, booking: "Booking", viewer_id: UUID | None) -> "BookingResponse":
        """Serialise a booking for one caller, blanking what they may not see.

        ``doorstep_otp`` gates en_route → in_progress, so it has to reach the
        provider from the consumer at the door rather than from this API — the
        rule ``BookingOfferResponse`` already documents (SVC-153) and which
        ``BookingResponse`` was declaring in a comment but never applying
        (SVC-169). Serving it to the assigned provider made the doorstep check
        ceremonial: they could read their own booking and skip the door.

        Anyone who is not the consumer gets ``None``, including a ``viewer_id``
        of ``None`` (admin listings, which have no booking-party identity).

        The blanking happens on this validated *copy*, deliberately. The
        assignment must never be made against the ORM object on a read path::

            booking = await self.get_booking(booking_id, user_id)
            booking.doorstep_otp = None   # persists — do not do this

        ``get_booking`` returns a session-attached ``Booking`` and ``get_db``
        commits at the end of every successful request (``app/database.py:65``),
        so that would erase the PIN from the row instead of hiding it from the
        response, and ``/start`` would then have nothing left to match. The
        column is nullable since PR #59, so no IntegrityError would catch it —
        the read would quietly wipe the row and return 200. The one place that
        assignment belongs is ``transition_status``, which wipes the OTP on
        IN_PROGRESS / COMPLETED / CANCELLED because it is spent by then.
        """
        response = cls.model_validate(booking)
        if viewer_id is None or viewer_id != booking.consumer_id:
            response.doorstep_otp = None
        return response


class BookingOfferResponse(BaseModel):
    """What a broadcast recipient may see about a job they have not yet accepted.

    Deliberately its own schema rather than a reuse of ``BookingResponse`` (SVC-153).
    This is served to *every* nearby provider, none of whom is assigned yet, so it
    carries only what the Incoming Request Card renders — and notably not
    ``doorstep_otp``, which gates the en_route → in_progress transition and must
    reach the provider from the consumer at the door, not from this API.

    ``service_amount`` is returned as a plain number; the app formats the ₹ string.
    Rupee formatting is not done server-side here — see the ticket's money note.

    ``expires_at`` and ``seconds_remaining`` both describe the same instant and are
    both returned on purpose (SVC-166). The card's countdown must use
    ``seconds_remaining``: it is computed server-side, so a device with a wrong
    clock still counts down correctly, and clock skew is unbounded on a 120-second
    window. ``expires_at`` is the absolute form, for anything needing a real
    instant rather than a duration.

    Neither field carries a default — a defaulted field falls out of OpenAPI
    ``required`` and the generated client types it optional, which invites a
    ``?? 120`` fallback and reintroduces the bug this exists to fix.
    """

    booking_id: UUID
    service_name: str
    service_amount: float
    full_address: str
    distance_km: float
    expires_at: datetime
    seconds_remaining: int


class StartBookingRequest(BaseModel):
    otp: str = Field(..., min_length=4, max_length=4)


class CancelBookingRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)


class BookingListResponse(BaseModel):
    bookings: list[BookingResponse]
    total: int
    page: int
    per_page: int
