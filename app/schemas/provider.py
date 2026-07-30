from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.provider_profile import KYCStatus


class ProviderProfileUpdate(BaseModel):
    """Fields a provider can update on their own profile."""

    aadhaar: str | None = Field(None, min_length=12, max_length=12)
    pan: str | None = Field(None, min_length=10, max_length=10)
    upi_id: str | None = Field(None, max_length=100)
    bank_account: str | None = Field(None, max_length=100)
    service_area_lat: float | None = None
    service_area_lng: float | None = None
    service_area_radius_km: int | None = Field(None, ge=1, le=100)
    # List of languages the provider speaks. Strings are trimmed and
    # de-duplicated (case-insensitive); an empty list clears the field.
    languages: list[str] | None = Field(None, max_length=20)

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


class ProviderStatusUpdate(BaseModel):
    is_online: bool


class ProviderResponse(BaseModel):
    id: UUID
    user_id: UUID
    profile_photo_url: str | None
    languages: list[str] = []
    avg_rating: float
    total_completed_jobs: int
    kyc_status: KYCStatus
    is_online: bool
    service_area_radius_km: int

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


# ---------------------------------------------------------------------------
# Earnings (SC-33)
# ---------------------------------------------------------------------------


class EarningsBookingItem(BaseModel):
    booking_id: UUID
    service_name: str
    completed_at: datetime
    payout_amount: float

    model_config = {"from_attributes": True}


class EarningsResponse(BaseModel):
    range: str  # "today" | "week" | "month"
    total_payout: float
    completed_count: int
    bookings: list[EarningsBookingItem]
