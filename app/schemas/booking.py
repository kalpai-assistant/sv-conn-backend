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


class StartBookingRequest(BaseModel):
    otp: str = Field(..., min_length=4, max_length=4)


class CancelBookingRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)


class BookingListResponse(BaseModel):
    bookings: list[BookingResponse]
    total: int
    page: int
    per_page: int
