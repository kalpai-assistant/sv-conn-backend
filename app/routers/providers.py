from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.user import User, UserRole
from app.schemas.provider import (
    ProviderProfileUpdate, ProviderStatusUpdate,
    ProviderResponse, NearbyProviderResponse,
    ProviderServiceSubscribeRequest, ProviderServiceSubscriptionResponse,
    KYCSubmissionResponse,
)
from app.services.provider_service import ProviderService

router = APIRouter(prefix="/providers", tags=["Providers"])

# Limits for KYC document uploads. Aadhaar/selfie JPEGs are typically <2MB;
# 5MB leaves room for unoptimised camera output. content-type whitelist is the
# real safety net — without it the bucket fills with PDFs and HEIC files we
# can't render in the admin UI.
_KYC_MAX_BYTES = 5 * 1024 * 1024
_KYC_ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/webp": "webp",
}


async def _read_kyc_upload(upload: UploadFile, field_name: str) -> tuple[bytes, str, str]:
    """Validate + read a KYC upload. Returns (bytes, content_type, ext)."""
    if upload.content_type not in _KYC_ALLOWED_CONTENT_TYPES:
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
    if len(data) > _KYC_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{field_name}: file exceeds {_KYC_MAX_BYTES // (1024 * 1024)} MB limit",
        )
    return data, upload.content_type, _KYC_ALLOWED_CONTENT_TYPES[upload.content_type]


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
    )


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
    return await ProviderService(db).subscribe_to_service(
        current_user.id, request.service_id
    )


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
    aadhaar: UploadFile = File(..., description="Aadhaar document image"),
    selfie: UploadFile = File(..., description="Live selfie"),
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Upload KYC documents. Sets the profile's kyc_status to SUBMITTED."""
    aadhaar_bytes, aadhaar_ct, aadhaar_ext = await _read_kyc_upload(aadhaar, "aadhaar")
    selfie_bytes, selfie_ct, selfie_ext = await _read_kyc_upload(selfie, "selfie")

    profile = await ProviderService(db).submit_kyc(
        user_id=current_user.id,
        aadhaar_bytes=aadhaar_bytes,
        aadhaar_content_type=aadhaar_ct,
        aadhaar_ext=aadhaar_ext,
        selfie_bytes=selfie_bytes,
        selfie_content_type=selfie_ct,
        selfie_ext=selfie_ext,
    )
    return profile
