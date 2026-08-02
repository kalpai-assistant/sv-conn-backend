from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


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
    doorstep_otp: str | None = None  # Only visible to consumer
    scheduled_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class BookingOfferResponse(BaseModel):
    """What a broadcast recipient may see about a job they have not yet accepted.

    Deliberately its own schema rather than a reuse of ``BookingResponse`` (SVC-153).
    This is served to *every* nearby provider, none of whom is assigned yet, so it
    carries only what the Incoming Request Card renders — and notably not
    ``doorstep_otp``, which gates the en_route → in_progress transition and must
    reach the provider from the consumer at the door, not from this API.

    ``service_amount`` is returned as a plain number; the app formats the ₹ string.
    Rupee formatting is not done server-side here — see the ticket's money note.
    """

    booking_id: UUID
    service_name: str
    service_amount: float
    full_address: str
    distance_km: float


class StartBookingRequest(BaseModel):
    otp: str = Field(..., min_length=4, max_length=4)


class CancelBookingRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)


class BookingListResponse(BaseModel):
    bookings: list[BookingResponse]
    total: int
    page: int
    per_page: int
