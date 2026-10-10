"""Admin team management (SVC-329): owners, their Pros, invites, force-remove,
reassign, and the admin audit log."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.provider_profile import KYCStatus

_REASON = Field(..., min_length=3, max_length=500, examples=["Left the business in September"])


class AdminProviderRow(BaseModel):
    """``GET /admin/providers``: one owner (a user with a provider profile)."""

    user_id: UUID
    name: str | None
    phone: str
    business_name: str | None
    kyc_status: KYCStatus
    is_online: bool
    user_is_active: bool
    # Active team Pros, the owner's own Pro-for-self not counted.
    active_pro_count: int
    created_at: datetime


class AdminTeamPro(BaseModel):
    """A Pro row as admin sees it: removed rows included (``is_active``)."""

    id: UUID
    service_provider_id: UUID
    user_id: UUID
    display_name: str
    phone: str
    user_is_active: bool
    profile_photo_url: str | None
    kyc_status: KYCStatus
    is_online: bool
    is_active: bool
    is_self: bool
    accepts_direct_jobs: bool
    # Bookings on this Pro in ACTIVE_JOB_STATUSES.
    active_job_count: int
    created_at: datetime


class AdminProOwner(BaseModel):
    user_id: UUID
    name: str | None
    phone: str
    business_name: str | None


class AdminProDetail(AdminTeamPro):
    """``GET /admin/pros/{id}``: the Pro and the owner they belong to."""

    service_provider: AdminProOwner


class AdminInviteRow(BaseModel):
    """An invite in the owner's history. No ``token``: a pending code is a live
    credential, and support doesn't need it (the SP sees it in My Team)."""

    id: UUID
    invitee_phone: str
    status: str
    expires_at: datetime
    created_at: datetime
    accepted_at: datetime | None
    accepted_by_user_id: UUID | None
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class ForceRemoveRequest(BaseModel):
    reason: str = _REASON


class ForceRemoveResponse(BaseModel):
    professional_id: UUID
    # True when this removed the owner's last other Pro while they had switched
    # off taking jobs themselves, so that was switched back on (SVC-326).
    self_reenabled: bool


class ReassignRequest(BaseModel):
    target_sp_user_id: UUID
    reason: str = _REASON


class ReassignResponse(BaseModel):
    old_professional_id: UUID
    # The person's row under the new owner: reactivated (keeping its own KYC)
    # or new (KYC PENDING).
    professional: AdminTeamPro
    reactivated: bool
    self_reenabled: bool


class DeactivateUserRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AdminActionRow(BaseModel):
    id: UUID
    admin_user_id: UUID
    admin_name: str | None
    action: str
    target_type: str
    target_id: UUID
    sp_user_id: UUID | None
    to_sp_user_id: UUID | None
    reason: str | None
    details: dict[str, Any]
    created_at: datetime
