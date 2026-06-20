from datetime import datetime
from pydantic import BaseModel, Field
from uuid import UUID
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


class ProviderStatusUpdate(BaseModel):
    is_online: bool


class ProviderResponse(BaseModel):
    id: UUID
    user_id: UUID
    profile_photo_url: str | None
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
