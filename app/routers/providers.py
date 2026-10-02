from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.user import User
from app.schemas.booking import BookingOfferResponse
from app.schemas.provider import (
    EarningsResponse,
    EarningsSummaryResponse,
    KYCSubmissionResponse,
    NearbyProviderResponse,
    PayoutMethodSelect,
    ProviderPayoutListResponse,
    ProviderPayoutSummaryResponse,
    ProviderProfileUpdate,
    ProviderResponse,
    ProviderServiceSubscribeRequest,
    ProviderServiceSubscriptionResponse,
    ProviderStatusUpdate,
)
from app.schemas.team import (
    AcceptInviteRequest,
    ProfessionalResponse,
    TeamInviteCreate,
    TeamInviteResponse,
)
from app.services.booking_service import BookingService
from app.services.payout_service import PayoutService
from app.services.provider_service import ProviderService
from app.services.team_service import TeamService
from app.utils.rate_limiter import rate_limit

router = APIRouter(prefix="/providers", tags=["Providers"])

# Limits for image uploads (KYC docs + provider profile photo). Phone-camera
# JPEGs are typically <2MB; 5MB leaves room for unoptimised output. The
# content-type whitelist is the real safety net — without it the bucket fills
# with PDFs and formats we can't render in the admin/app UI.
_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/webp": "webp",
}


async def _read_image_upload(upload: UploadFile, field_name: str) -> tuple[bytes, str, str]:
    """Validate + read an image upload. Returns (bytes, content_type, ext)."""
    if upload.content_type not in _IMAGE_ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{field_name}: unsupported file type {upload.content_type!r}",
        )
    data = await upload.read()
    if len(data) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name}: empty file",
        )
    if len(data) > _IMAGE_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{field_name}: file exceeds {_IMAGE_MAX_BYTES // (1024 * 1024)} MB limit",
        )
    return data, upload.content_type, _IMAGE_ALLOWED_CONTENT_TYPES[upload.content_type]


@router.get("/me", response_model=ProviderResponse)
async def get_provider_profile(
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    profile, _ = await ProviderService(db).get_or_create_profile(current_user.id)
    return profile


@router.patch("/me", response_model=ProviderResponse)
async def update_provider_profile(
    request: ProviderProfileUpdate,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    return await ProviderService(db).update_profile(
        user_id=current_user.id,
        aadhaar=request.aadhaar,
        pan=request.pan,
        upi_id=request.upi_id,
        bank_account=request.bank_account,
        service_area_lat=request.service_area_lat,
        service_area_lng=request.service_area_lng,
        service_area_radius_km=request.service_area_radius_km,
        payout_vpa=request.payout_vpa,
        payout_account_number=request.payout_account_number,
        payout_ifsc=request.payout_ifsc,
        languages=request.languages,
        business_name=request.business_name,
    )


@router.post("/me/photo", response_model=ProviderResponse)
async def upload_profile_photo(
    photo: UploadFile = File(..., description="Provider profile photo"),
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Upload/replace the provider's profile photo. Returns the updated profile."""
    photo_bytes, content_type, ext = await _read_image_upload(photo, "photo")
    return await ProviderService(db).update_photo(
        user_id=current_user.id,
        photo_bytes=photo_bytes,
        content_type=content_type,
        ext=ext,
    )


@router.post("/me/business-image", response_model=ProviderResponse)
async def upload_business_image(
    image: UploadFile = File(..., description="Business image (logo)"),
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Upload/replace the provider's business image (SVC-336). Returns the updated profile."""
    image_bytes, content_type, ext = await _read_image_upload(image, "image")
    return await ProviderService(db).update_business_image(
        user_id=current_user.id,
        image_bytes=image_bytes,
        content_type=content_type,
        ext=ext,
    )


@router.patch("/me/payout-method", response_model=ProviderResponse)
async def set_active_payout_method(
    request: PayoutMethodSelect,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Switch payouts to a method already on file, without re-entering it.

    A provider who has both a UPI handle and a bank account stored can flip which
    one gets paid. Returns 400 if nothing is stored for the requested method.
    """
    return await ProviderService(db).set_active_payout_method(current_user.id, request.method)


@router.delete("/me/payout-method", response_model=ProviderResponse)
async def remove_payout_method(
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Erase all stored payout destinations.

    Deliberate, explicit action — saving a new method never removes the old one, so
    this is the only path that deletes payout details. Clears both methods, not just
    the active one: "remove my payout details" should leave the platform holding
    nothing. Afterwards ``payout_set`` is false and the provider cannot be paid until
    they add a method again.
    """
    return await ProviderService(db).remove_payout_method(current_user.id)


@router.patch("/me/status", response_model=ProviderResponse)
async def set_online_status(
    request: ProviderStatusUpdate,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    return await ProviderService(db).set_online_status(current_user.id, request.is_online)


@router.get("/nearby", response_model=list[NearbyProviderResponse])
async def find_nearby_providers(
    lat: float = Query(..., description="Consumer latitude"),
    lng: float = Query(..., description="Consumer longitude"),
    radius_km: float = Query(10.0, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await ProviderService(db).find_nearby(lat=lat, lng=lng, radius_km=radius_km)
    return [NearbyProviderResponse(**row) for row in rows]


@router.get(
    "/me/services",
    response_model=list[ProviderServiceSubscriptionResponse],
)
async def list_my_services(
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """List the services the current provider is subscribed to."""
    return await ProviderService(db).list_subscriptions(current_user.id)


@router.get("/me/offers", response_model=list[BookingOfferResponse])
async def list_my_offers(
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Open job offers for the current provider (SVC-254).

    The fallback delivery path. Until this existed, an FCM push was the only way
    an offer reached anyone — so a stale token, a denied notification permission
    or an iOS Safari tab meant the provider was silently deaf to paid work.

    The provider app polls this while online. Each entry is the same
    ``BookingOfferResponse`` the push route's card fetches, so a polled offer and
    a pushed one are indistinguishable to the UI. Soonest-expiring first.

    Also tops up: a provider who came online after a booking was dispatched has
    no offer row for it, and its push has already been sent. This is the only
    place they can be added, so the read writes the missing rows.
    """
    return await BookingService(db).list_open_offers(current_user.id)


@router.get("/me/payouts/summary", response_model=ProviderPayoutSummaryResponse)
async def get_my_payout_summary(
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """What the provider's next transfer will actually be (SVC-303).

    Distinct from `/me/earnings/summary`, which answers "what have I earned".
    This answers "what is coming to me", and the two differ by real money:

    * **cash commission** — on a cash job the provider collected the full amount
      at the door, so the platform's 20% is netted off their next online
      transfer;
    * **the T+1 settlement gate** — a completed job is only payable once
      Razorpay has deposited that money with us, since paying from platform
      float is not permitted.

    So `pending_amount` is the wallet-derived figure the payout will really
    send, `gross_amount` is what the queued jobs are worth before netting, and
    the frontend shows the difference rather than letting the provider discover
    it at the bank.

    Names no future payout date: payouts are admin-triggered and there is no
    schedule, so any date here would be invented.
    """
    return await PayoutService(db).get_provider_payout_summary(current_user.id)


@router.get("/me/payouts", response_model=ProviderPayoutListResponse)
async def list_my_payouts(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """The provider's own payout statement, newest first (SVC-303).

    Reads `settlement_statements` — the artifact built to be the snapshot a
    provider actually sees — rather than `payout_audits`, which records
    *attempts* (RazorpayX's status frozen at creation, never updated when the
    money lands) plus internal retries and nightly reconciliation repairs. A
    provider reading those raw would see several rows for one payment.

    Carries no admin id, gateway id or error text: a provider learns what
    happened to their money, not who moved it or through which processor.
    """
    return await PayoutService(db).list_my_payouts(current_user.id, page=page, per_page=per_page)


@router.get("/me/earnings/summary", response_model=EarningsSummaryResponse)
async def get_my_earnings_summary(
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """
    Lifetime earnings total for the signed-in provider (SVC-275).

    A separate route rather than a fourth `range` value on the endpoint below,
    because that one returns a row per booking: bounded over a window, unbounded
    over a lifetime. Provider Home shows a single figure and should not pay for
    every job the provider has ever done to render it.

    Same definition of "earned" as the windowed endpoint (completed, not
    refunded), shared via `ProviderService._earned_filter`. That is narrower than
    the payout path's `_owed_filter`, which also excludes cash jobs: a cash job is
    earned but not owed, since the provider already has the money.
    """
    return await ProviderService(db).get_earnings_summary(current_user.id)


@router.get("/me/earnings", response_model=EarningsResponse)
async def get_my_earnings(
    range: Literal["today", "week", "month"] = Query("week"),
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """
    Provider earnings summary + per-booking breakdown for the chosen window.

    Window is the IST calendar boundary (today = since 00:00 IST, week = since
    Monday 00:00 IST, month = since the 1st 00:00 IST). Refunded payments are
    excluded. Used by the provider Earnings dashboard (SC-27).
    """
    return await ProviderService(db).get_earnings(current_user.id, range)


@router.post(
    "/me/services",
    status_code=status.HTTP_201_CREATED,
    response_model=ProviderServiceSubscriptionResponse,
)
async def subscribe_to_service(
    request: ProviderServiceSubscribeRequest,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    return await ProviderService(db).subscribe_to_service(current_user.id, request.service_id)


@router.delete(
    "/me/services/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unsubscribe_from_service(
    service_id: UUID,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    await ProviderService(db).unsubscribe_from_service(current_user.id, service_id)


# ---------------------------------------------------------------------------
# KYC submission (SC-31)
# ---------------------------------------------------------------------------


@router.post(
    "/me/kyc",
    status_code=status.HTTP_201_CREATED,
    response_model=KYCSubmissionResponse,
)
async def submit_kyc(
    aadhaar_front: UploadFile = File(..., description="Aadhaar front image"),
    aadhaar_back: UploadFile = File(..., description="Aadhaar back image"),
    selfie: UploadFile = File(..., description="Live selfie"),
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Upload KYC documents. Sets the profile's kyc_status to SUBMITTED."""
    front_bytes, front_ct, front_ext = await _read_image_upload(aadhaar_front, "aadhaar_front")
    back_bytes, back_ct, back_ext = await _read_image_upload(aadhaar_back, "aadhaar_back")
    selfie_bytes, selfie_ct, selfie_ext = await _read_image_upload(selfie, "selfie")

    profile = await ProviderService(db).submit_kyc(
        user_id=current_user.id,
        aadhaar_front_bytes=front_bytes,
        aadhaar_front_content_type=front_ct,
        aadhaar_front_ext=front_ext,
        aadhaar_back_bytes=back_bytes,
        aadhaar_back_content_type=back_ct,
        aadhaar_back_ext=back_ext,
        selfie_bytes=selfie_bytes,
        selfie_content_type=selfie_ct,
        selfie_ext=selfie_ext,
    )
    return profile


# ---------------------------------------------------------------------------
# SP team management (SVC-320, Phase B PR 1)
# ---------------------------------------------------------------------------


@router.post(
    "/me/team/invites",
    status_code=status.HTTP_201_CREATED,
    response_model=TeamInviteResponse,
    dependencies=[Depends(rate_limit("team_invite_create", max_requests=5))],
)
async def create_team_invite(
    request: TeamInviteCreate,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Invite a phone number to join my team as a Pro, by SMS.

    403 ``INVITER_PAYOUT_KYC_INCOMPLETE`` until my KYC is verified (SVC-327):
    team earnings are paid to me. 5 per hour. The link is single-use and lasts 7
    days. Until the DLT template is approved (SVC-328) the SMS is only logged as
    ``[MOCK INVITE]``; the response carries the link either way.
    """
    return await TeamService(db).create_invite(current_user, request.invitee_phone)


@router.post("/me/team/invites/{invite_id}/revoke", response_model=TeamInviteResponse)
async def revoke_team_invite(
    invite_id: UUID,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a pending invite. 409 once it's accepted, revoked or expired."""
    return await TeamService(db).revoke_invite(current_user.id, invite_id)


@router.get("/me/team", response_model=list[ProfessionalResponse])
async def list_my_team(
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Active Pros under my SP, my own Pro-for-self (``is_self``) first."""
    return await TeamService(db).list_team(current_user.id)


@router.delete("/me/team/{pro_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_member(
    pro_id: UUID,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Remove a Pro from my team (soft: the row is kept for past bookings).

    400 ``CANNOT_REMOVE_SELF`` for my own Pro-for-self; going off-tools is SVC-326.
    """
    await TeamService(db).remove_pro(current_user.id, pro_id)


@router.post("/me/accept-invite", response_model=ProfessionalResponse)
async def accept_team_invite(
    request: AcceptInviteRequest,
    # Not provider-only: the app signs everyone up as a consumer, so most
    # invitees are consumers. The invite is bound to their phone instead.
    current_user: User = Depends(require_role("consumer", "provider")),
    db: AsyncSession = Depends(get_db),
):
    """Join the inviting SP's team. Returns my new (or reactivated) Pro row.

    403 if the invite was sent to another phone; 404 unknown token; 410 used,
    revoked or expired; 400 ``ALREADY_PRO_ELSEWHERE`` while I'm an active Pro
    under another SP (an SP's own Pro-for-self counts); 409 already on this team.
    """
    return await TeamService(db).accept_invite(current_user, request.token)
