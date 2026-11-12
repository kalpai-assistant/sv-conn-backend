"""The support console (SVC-433): lookup, customer profile, notes, retry dispatch
and reassign. Money is a float, never Decimal (a Decimal serialises as a string)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.booking import BookingStatus, PaymentMethod
from app.models.booking_support_note import SUPPORT_NOTE_MAX_LENGTH
from app.models.provider_profile import KYCStatus
from app.models.user import AdminRole

LookupKind = Literal["phone", "booking_id", "booking_id_suffix", "name"]
ResultKind = Literal["customers", "providers", "pros", "bookings"]


# --- Lookup ------------------------------------------------------------------------


class LookupCustomer(BaseModel):
    user_id: UUID
    name: str | None
    phone: str
    created_at: datetime
    is_active: bool
    # Always false from a phone match: a deleted account's phone is a placeholder.
    deleted: bool
    booking_count: int


class LookupProvider(BaseModel):
    user_id: UUID
    name: str | None
    business_name: str | None
    phone: str
    kyc_status: KYCStatus
    is_online: bool
    matched_by: Literal["phone", "name"]


class LookupProOwner(BaseModel):
    user_id: UUID
    name: str | None
    business_name: str | None


class LookupPro(BaseModel):
    """A team Pro. Nobody's Pro-for-self: that person is listed as the provider."""

    professional_id: UUID
    display_name: str
    phone: str
    is_online: bool
    # False for a Pro removed from the team, kept so support can see who they were.
    is_active: bool
    service_provider: LookupProOwner


class LookupBooking(BaseModel):
    id: UUID
    status: BookingStatus
    created_at: datetime
    scheduled_at: datetime | None
    service_summary: str
    consumer_name: str | None
    consumer_phone: str | None
    provider_name: str | None
    matched_by: Literal["id", "id_suffix"]


class LookupResponse(BaseModel):
    """``GET /admin/support/lookup``. ``interpreted_as`` lists every type the
    input fits, and each was searched: "facade" is both a booking id suffix and
    a name. Results are grouped by kind, so the screen never has to guess.
    ``truncated`` names the kinds that had more rows than the cap."""

    query: str
    interpreted_as: list[LookupKind]
    customers: list[LookupCustomer]
    providers: list[LookupProvider]
    pros: list[LookupPro]
    bookings: list[LookupBooking]
    truncated: list[ResultKind]


# --- Customer profile --------------------------------------------------------------


class CustomerBookingProvider(BaseModel):
    user_id: UUID
    name: str | None
    business_name: str | None


class CustomerBookingPro(BaseModel):
    id: UUID
    display_name: str


class CustomerBooking(BaseModel):
    id: UUID
    status: BookingStatus
    created_at: datetime
    scheduled_at: datetime | None
    service_summary: str
    # service_amount − discount_amount + tip_amount.
    total: float
    payment_method: PaymentMethod | None
    provider: CustomerBookingProvider | None
    professional: CustomerBookingPro | None


class CustomerProfile(BaseModel):
    """``GET /admin/support/customers/{id}``.

    ``lifetime_spend``: over COMPLETED bookings, service_amount − discount_amount
    + tip_amount, minus what dispute refunds on those bookings actually paid
    back (a Razorpay refund that is PROCESSED, or a cash refund recorded as
    paid). Cash and online both count. Cancelled and expired bookings count 0.
    """

    user_id: UUID
    name: str | None
    phone: str
    email: str | None
    created_at: datetime
    deleted_at: datetime | None
    is_active: bool
    address_count: int
    lifetime_bookings: int
    completed_bookings: int
    lifetime_spend: float
    # Newest first, at most 10. All of them: GET /admin/bookings?consumer_id=.
    recent_bookings: list[CustomerBooking]


# --- Notes -------------------------------------------------------------------------


class SupportNoteAuthor(BaseModel):
    user_id: UUID
    name: str | None
    email: str | None
    admin_role: AdminRole | None


class SupportNote(BaseModel):
    id: UUID
    booking_id: UUID
    kind: Literal["note", "contacted"]
    body: str | None
    created_at: datetime
    author: SupportNoteAuthor


class SupportNoteCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=SUPPORT_NOTE_MAX_LENGTH)


class ContactedCreate(BaseModel):
    body: str | None = Field(None, max_length=SUPPORT_NOTE_MAX_LENGTH)


# --- Retry dispatch ----------------------------------------------------------------


class RetryDispatchResponse(BaseModel):
    """``recipients``: how many SPs the new broadcast goes to, counted when the
    button was pressed: everyone eligible, minus SPs who already accepted or
    declined this booking. The pushes go out in the background and delivery is
    best-effort."""

    booking_id: UUID
    status: BookingStatus
    recipients: int
    retried_at: datetime
    offer_expires_at: datetime


# --- Reassign ----------------------------------------------------------------------


class EligiblePro(BaseModel):
    """One of the SP's active Pros. ``refusal_code`` is what
    ``POST .../reassign-pro`` would answer for them (a 409 code), or null when
    they can take it. The current Pro is ``is_current`` with no refusal, and not
    ``eligible``: there is nothing to change."""

    professional_id: UUID
    display_name: str
    phone: str
    is_self: bool
    is_online: bool
    kyc_status: KYCStatus
    is_current: bool
    eligible: bool
    refusal_code: str | None


class EligibleProsResponse(BaseModel):
    booking_id: UUID
    service_provider_id: UUID
    current_professional_id: UUID | None
    pros: list[EligiblePro]


class ReassignProRequest(BaseModel):
    professional_id: UUID
