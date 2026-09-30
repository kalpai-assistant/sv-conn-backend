import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.booking import PaymentMethod, SettlementStatus
from app.models.provider_profile import KYCStatus, PayoutMethod

# VPA: local-part@handle, e.g. "name@okhdfcbank". IFSC: 4 letters + 0 + 6 alnum.
_UPI_VPA_RE = re.compile(r"^[\w.\-]{2,}@[A-Za-z]{2,}$")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
# Indian bank account numbers are numeric (roughly 9-18 digits across banks).
_ACCOUNT_RE = re.compile(r"^\d{5,34}$")


class ProviderProfileUpdate(BaseModel):
    """Fields a provider can update on their own profile."""

    aadhaar: str | None = Field(None, min_length=12, max_length=12)
    pan: str | None = Field(None, min_length=10, max_length=10)
    upi_id: str | None = Field(None, max_length=100)
    bank_account: str | None = Field(None, max_length=100)
    service_area_lat: float | None = None
    service_area_lng: float | None = None
    service_area_radius_km: int | None = Field(None, ge=1, le=100)
    # Payout destination (separate from the KYC upi_id/bank_account above).
    # All optional so the onboarding payout step stays skippable.
    payout_vpa: str | None = Field(None, max_length=100)
    payout_account_number: str | None = Field(None, min_length=5, max_length=34)
    payout_ifsc: str | None = Field(None, max_length=11)
    # List of languages the provider speaks. Strings are trimmed and
    # de-duplicated (case-insensitive); an empty list clears the field.
    languages: list[str] | None = Field(None, max_length=20)
    # SVC-336: the business the provider trades as. Trimmed; blank is rejected
    # rather than stored, since onboarding requires a real name.
    business_name: str | None = Field(None, max_length=100)

    @field_validator("business_name")
    @classmethod
    def _clean_business_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("business_name must not be blank")
        return value

    @field_validator("payout_vpa")
    @classmethod
    def _validate_vpa(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not _UPI_VPA_RE.match(value):
            raise ValueError("payout_vpa must be a valid UPI VPA, e.g. name@bank")
        return value

    @field_validator("payout_ifsc")
    @classmethod
    def _validate_ifsc(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not _IFSC_RE.match(value):
            raise ValueError("payout_ifsc must match the IFSC format, e.g. HDFC0001234")
        return value

    @field_validator("payout_account_number")
    @classmethod
    def _validate_account_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not _ACCOUNT_RE.match(value):
            raise ValueError("payout_account_number must be 5-34 digits")
        return value

    @field_validator("languages")
    @classmethod
    def _clean_languages(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            lang = raw.strip()
            if not lang:
                continue
            if len(lang) > 50:
                raise ValueError("each language must be at most 50 characters")
            key = lang.casefold()
            if key not in seen:
                seen.add(key)
                cleaned.append(lang)
        return cleaned

    @model_validator(mode="after")
    def _validate_account_ifsc_pair(self) -> "ProviderProfileUpdate":
        if bool(self.payout_account_number) != bool(self.payout_ifsc):
            raise ValueError("payout_account_number and payout_ifsc must be provided together")
        # Saving a method makes it the active one (SVC-148), so sending both in one
        # request has no defined answer to "which is now active". Reject it rather
        # than pick one — the caller has to say what they mean.
        if self.payout_vpa and self.payout_account_number:
            raise ValueError(
                "Set one payout method at a time — send payout_vpa or "
                "payout_account_number + payout_ifsc, not both"
            )
        return self


class ProviderStatusUpdate(BaseModel):
    is_online: bool


class PayoutMethodSelect(BaseModel):
    """Switch payouts to a method the provider already has stored (SVC-148)."""

    method: PayoutMethod


class ProviderResponse(BaseModel):
    id: UUID
    user_id: UUID
    profile_photo_url: str | None
    # SVC-336: NULL for a provider who has not set them. The app shows a default
    # logo when business_image_url is NULL.
    business_name: str | None = None
    business_image_url: str | None = None
    languages: list[str] = []
    avg_rating: float
    total_completed_jobs: int
    kyc_status: KYCStatus
    is_online: bool
    service_area_radius_km: int
    # SVC-279: the centre, so a provider can see and edit the area they set.
    # Write-only until now — `ProviderProfileUpdate` accepted lat/lng but no
    # response returned them, which made "view your service area on a map"
    # impossible to build. Nullable because a provider who onboarded before
    # SVC-257 has no centre at all.
    service_area_lat: float | None = None
    service_area_lng: float | None = None
    # Derived: whether a payout destination is active. Never exposes the PII.
    payout_set: bool = False
    # Which method payouts go to, and a safe label for it — "••••8901" for a bank
    # account, or the UPI handle in full. Enough for a provider to recognise what
    # they saved without the API ever returning the underlying details.
    payout_method: PayoutMethod | None = None
    payout_display: str | None = None
    # Which methods have details on file, active or not (SVC-152) — so the frontend
    # can tell whether "switch back to your bank account" is possible instead of
    # firing the switch and reading the 400 as its answer. Names the filled slots,
    # never their contents; a method that is active is always listed here too.
    #
    # Deliberately no default, so it lands in the OpenAPI `required` set and generated
    # clients type it non-optional. The server always sends it — empty list when
    # nothing is stored — and a client that treats "absent" as "nothing stored" would
    # hide the switch from a provider who does have the other method on file.
    payout_methods_stored: list[PayoutMethod]

    model_config = {"from_attributes": True}


class NearbyProviderResponse(BaseModel):
    """Slim response for the 'nearby providers' discovery endpoint."""

    id: UUID
    user_id: UUID
    avg_rating: float
    total_completed_jobs: int
    is_online: bool
    distance_km: float | None = None  # populated by geo query

    model_config = {"from_attributes": True}


class ProviderServiceSubscribeRequest(BaseModel):
    service_id: UUID


class ProviderServiceSubscriptionResponse(BaseModel):
    id: UUID
    provider_id: UUID
    service_id: UUID

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# KYC
# ---------------------------------------------------------------------------


class KYCSubmissionResponse(BaseModel):
    kyc_status: KYCStatus
    kyc_submitted_at: datetime | None

    model_config = {"from_attributes": True}


class KYCRejectionRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class KYCReviewResponse(BaseModel):
    kyc_status: KYCStatus
    kyc_reviewed_at: datetime | None
    kyc_rejection_reason: str | None

    model_config = {"from_attributes": True}


class KYCDocsResponse(BaseModel):
    """Admin-only: full KYC payload including presigned doc URLs for review."""

    user_id: UUID
    name: str | None
    phone: str
    kyc_status: KYCStatus
    kyc_submitted_at: datetime | None
    kyc_reviewed_at: datetime | None
    kyc_rejection_reason: str | None
    # Presigned S3 URLs; nil if the provider hasn't uploaded yet.
    aadhaar_url: str | None
    selfie_url: str | None
    # SVC-307: full KYCVerdict payload from the AI verifier — extraction,
    # flags, outcome, confidence, and raw LLM response for audit. Null on
    # pre-SVC-307 submissions that were never scored, so the admin UI can
    # distinguish "AI said needs review" from "AI never looked".
    ai_verdict: dict | None = None


class KYCQueueRow(BaseModel):
    """One row in the admin KYC queue listing (SVC-298).

    Shape is a UserResponse plus the three ProviderProfile timestamps the review
    UI wants for aging + the rejected view. Kept separate from UserResponse
    because those are provider-only columns — surfacing them on every user list
    would be schema noise.
    """

    id: UUID
    phone: str
    name: str | None
    email: str | None
    kyc_submitted_at: datetime | None
    kyc_reviewed_at: datetime | None
    kyc_rejection_reason: str | None


# ---------------------------------------------------------------------------
# Earnings (SC-33)
# ---------------------------------------------------------------------------


class EarningsBookingItem(BaseModel):
    booking_id: UUID
    service_name: str
    completed_at: datetime
    # Base commission split + any consumer tip. Rendered as one line on
    # the earnings dashboard.
    payout_amount: float
    # Broken out separately so the app can show "₹800 + ₹50 tip" when
    # tips exist without doing the subtraction itself.
    tip_amount: float = 0.0

    # SVC-303: where this job's money is, so the provider can see why the
    # earned total and the transfer differ instead of inferring it.
    #
    # All three default, so an older client and the SVC-260 screen keep working
    # untouched — this is purely additive to the existing response.
    settlement_status: SettlementStatus | None = None
    # Cash is not a settlement state: such a booking never enters the payout
    # path at all, because the provider collected at the door. Surfaced
    # separately so the UI can label it as its own thing rather than as a
    # variant of "paid", which would imply a transfer that never happened.
    payment_method: PaymentMethod | None = None
    # The bank's own words on a failed payout, so the error block can say what
    # actually went wrong rather than apologising generically.
    settlement_failure_reason: str | None = None

    model_config = {"from_attributes": True}


class EarningsResponse(BaseModel):
    range: str  # "today" | "week" | "month"
    total_payout: float
    completed_count: int
    bookings: list[EarningsBookingItem]


class EarningsSummaryResponse(BaseModel):
    """All-time totals, with no per-booking breakdown (SVC-275).

    Deliberately two scalars. ``EarningsResponse`` carries one row per booking,
    which is fine over a window and unbounded over a lifetime — Provider Home
    would download every job a provider has ever completed to render a single
    number.
    """

    lifetime_payout: float
    completed_count: int


# ---------------------------------------------------------------------------
# Payouts (SVC-303)
# ---------------------------------------------------------------------------


class ProviderPayoutSummaryResponse(BaseModel):
    """What the next transfer will actually send, and what the last one did.

    Three money figures rather than one, because they genuinely differ and the
    difference is the thing this ticket exists to explain:

    * ``pending_amount`` is ``min(wallet_balance, booking_sum)`` — what the
      payout will really send.
    * ``gross_amount`` is what those jobs are worth before netting.
    * ``balance_owed`` is set only when the provider owes the platform more than
      it owes them, which happens after a run of cash work.

    A provider told "₹3,480 coming" who receives ₹2,900 has been misled by their
    own app, so the headline figure is the net one and the gap is named.
    """

    # Rupees throughout — this sits beside the earnings endpoints on screen,
    # which speak rupees. The payout path speaks paise; the service converts.
    pending_amount: float
    gross_amount: float
    balance_owed: float
    # Jobs queued for the next payout. A count, not a money figure — it comes
    # from ``_owed_filter`` while the amounts come from the wallet.
    pending_booking_count: int
    # Null, not zero, for a provider never paid: zero renders as "₹0 on —",
    # where null lets the UI say "no payouts yet", which is true.
    last_paid_at: datetime | None = None
    last_paid_amount: float | None = None
    # Whether a payout destination is on file. Lets the frontend render "we
    # can't send this yet" without a second request.
    payout_set: bool


class ProviderPayoutEntry(BaseModel):
    """One settlement cycle, as the provider sees it.

    Deliberately carries no ``payout_audit_id``, RazorpayX id, admin or error.
    A provider learns what happened to their money, never who moved it or
    through which processor.
    """

    id: UUID
    paid_at: datetime
    amount: float
    # The difference between what the provider expected and what arrived. A
    # smaller transfer with no explanation is the complaint this field prevents.
    cash_commission_deducted: float


class ProviderPayoutListResponse(BaseModel):
    entries: list[ProviderPayoutEntry]
    total: int
    page: int
    per_page: int
