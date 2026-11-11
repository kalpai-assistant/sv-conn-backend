"""SVC-431: the admin disputes + refunds surface (/admin/disputes/*).

Money is rupees, sent as JSON numbers, never Decimal strings or paise. The
enum-like values are plain strings checked by ``Literal``.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.booking import BookingStatus, PaymentMethod, PaymentStatus, SettlementStatus

ReasonCode = Literal["NO_SHOW", "QUALITY", "BILLING", "BEHAVIOR", "OTHER"]
DisputeStatus = Literal["RAISED", "IN_REVIEW", "RESOLVED", "CLOSED"]
ResolutionType = Literal["FULL_REFUND", "PARTIAL_REFUND", "NO_REFUND"]
RefundChannel = Literal["RAZORPAY", "MANUAL"]
RefundStatus = Literal["PENDING", "PROCESSED", "FAILED"]

TEXT_MAX = 2000


def _required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


# -- reads --------------------------------------------------------------------


class AdminDisputeRow(BaseModel):
    """One dispute.

    ``refund_channel`` RAZORPAY: refunded on the booking's Razorpay payment, and
    ``refund_status`` tracks the webhook (PENDING → PROCESSED, or FAILED with
    ``refund_failed_reason``). MANUAL: a cash booking refunded outside the app;
    the amount is a record only and ``refund_status`` is null."""

    id: uuid.UUID
    booking_id: uuid.UUID
    booking_status: BookingStatus
    payment_method: PaymentMethod | None
    reason_code: ReasonCode
    description: str
    status: DisputeStatus
    resolution_type: ResolutionType | None
    refund_amount: float | None
    refund_channel: RefundChannel | None
    refund_status: RefundStatus | None
    refund_failed_reason: str | None
    razorpay_refund_id: str | None
    resolution_notes: str | None
    customer_id: uuid.UUID
    customer_name: str | None
    customer_phone: str | None
    created_by: uuid.UUID | None
    created_by_name: str | None
    resolved_by: uuid.UUID | None
    resolved_by_name: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None


class AdminDisputeListResponse(BaseModel):
    items: list[AdminDisputeRow]
    total: int
    page: int
    per_page: int


class AdminDisputeParty(BaseModel):
    id: uuid.UUID
    name: str | None


class AdminDisputeBookingContext(BaseModel):
    """The booking as support needs it to decide a dispute."""

    id: uuid.UUID
    status: BookingStatus
    payment_method: PaymentMethod | None
    payment_status: PaymentStatus
    settlement_status: SettlementStatus
    service_amount: float
    discount_amount: float
    tip_amount: float
    scheduled_at: datetime | None
    created_at: datetime
    cancellation_reason: str | None
    customer: AdminDisputeParty
    customer_phone: str | None
    provider: AdminDisputeParty | None
    professional: AdminDisputeParty | None


class AdminDisputeRefunds(BaseModel):
    """What has been, and can still be, refunded on the booking.

    ``captured`` is what the customer paid for the service (service − discount;
    the tip is never refundable), 0 when nothing was paid. ``cancel_refund`` is
    the cancel-time refund (``payments.refund_*``), counted whatever its state.
    ``dispute_refunds`` sums earlier dispute refunds, a failed Razorpay one left
    out. ``refundable`` = captured − both, never below 0. ``channel`` is how a
    refund here would be paid: RAZORPAY (online), MANUAL (cash) or null
    (nothing was paid)."""

    channel: RefundChannel | None
    captured: float
    cancel_refund: float
    cancel_refund_id: str | None
    cancel_refunded_at: datetime | None
    dispute_refunds: float
    refundable: float
    support_refund_limit: float


class AdminDisputeDetail(BaseModel):
    """A booking's dispute page. ``open_dispute`` is the RAISED / IN_REVIEW one,
    if any; ``disputes`` is every dispute on the booking, newest first."""

    booking: AdminDisputeBookingContext
    refunds: AdminDisputeRefunds
    open_dispute: AdminDisputeRow | None
    disputes: list[AdminDisputeRow]


# -- writes -------------------------------------------------------------------


class AdminDisputeCreate(BaseModel):
    """Log a dispute on the customer's behalf. Only on a COMPLETED or CANCELLED
    booking, and one open dispute per booking."""

    model_config = ConfigDict(extra="forbid")

    booking_id: uuid.UUID
    reason_code: ReasonCode
    description: str = Field(max_length=TEXT_MAX)

    _description = field_validator("description")(_required_text)


class AdminDisputeResolve(BaseModel):
    """Settle a RAISED or IN_REVIEW dispute.

    * ``FULL_REFUND``: everything still refundable. ``refund_amount`` may be left
      out; if sent it must equal the refundable amount.
    * ``PARTIAL_REFUND``: ``refund_amount`` is required, > 0 and ≤ refundable.
    * ``NO_REFUND``: no ``refund_amount``.

    ``notes`` is required: the decision, and for a cash booking how the money was
    paid back."""

    model_config = ConfigDict(extra="forbid")

    resolution_type: ResolutionType
    refund_amount: Decimal | None = None
    notes: str = Field(max_length=TEXT_MAX)

    _notes = field_validator("notes")(_required_text)
