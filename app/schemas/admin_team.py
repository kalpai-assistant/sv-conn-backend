"""Admin team management (SVC-329): owners, their Pros, invites, force-remove,
reassign, and the admin audit log."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.booking import BookingStatus
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


class AdminServiceArea(BaseModel):
    """The SP's service area: a circle, since that's what is stored (a centre
    point and a radius), not a polygon."""

    lat: float
    lng: float
    radius_km: int


class AdminProviderService(BaseModel):
    """A catalog service the SP offers. ``is_active`` is the catalog row's, so
    a retired service can be shown as such."""

    id: UUID
    name: str
    is_active: bool


class AdminBookingVolume(BaseModel):
    """Bookings CREATED for this SP in the last 30 IST days, today included
    (not completed ones): ``created_at`` on or after IST midnight of ``since``.
    The same rule as ``GET /admin/bookings?provider_id=…&created_from=since``,
    so that link lists exactly these rows. ``by_status`` has every status."""

    since: date
    total: int
    by_status: dict[BookingStatus, int]


class AdminProviderDetail(AdminProviderRow):
    """``GET /admin/providers/{id}`` (SVC-427): the search row plus the SP
    page's aggregates. A superset of the row, so older callers are unaffected."""

    # Null when the SP never set a centre.
    service_area: AdminServiceArea | None
    avg_rating: float
    total_completed_jobs: int
    services: list[AdminProviderService]
    bookings_30d: AdminBookingVolume


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


class AdminProListRow(AdminTeamPro):
    """``GET /admin/pros`` (SVC-427): a Pro row and the owner it belongs to."""

    service_provider: AdminProOwner


class AdminProListResponse(BaseModel):
    items: list[AdminProListRow]
    total: int
    page: int
    per_page: int


class AdminProDetail(AdminProListRow):
    """``GET /admin/pros/{id}``: the Pro, their owner, and (SVC-427) their
    rating and completed jobs, by the SVC-396 trust-card rules: reviews and
    COMPLETED bookings carrying this ``professional_id``."""

    # Null when nobody has reviewed them; rounded to 1 decimal place.
    rating_avg: float | None
    rating_count: int
    completed_job_count: int


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
