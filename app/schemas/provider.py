import re

from pydantic import BaseModel, Field, field_validator, model_validator
from uuid import UUID
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

    @model_validator(mode="after")
    def _validate_account_ifsc_pair(self) -> "ProviderProfileUpdate":
        if bool(self.payout_account_number) != bool(self.payout_ifsc):
            raise ValueError(
                "payout_account_number and payout_ifsc must be provided together"
            )
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
    avg_rating: float
    total_completed_jobs: int
    kyc_status: KYCStatus
    is_online: bool
    service_area_radius_km: int
    # Derived: whether a payout destination is active. Never exposes the PII.
    payout_set: bool = False
    # Which method payouts go to, and a safe label for it — "••••8901" for a bank
    # account, or the UPI handle in full. Enough for a provider to recognise what
    # they saved without the API ever returning the underlying details.
    payout_method: PayoutMethod | None = None
    payout_display: str | None = None

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
