from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.user import User, UserRole
from app.schemas.provider import (
    ProviderProfileUpdate, ProviderStatusUpdate,
    ProviderResponse, NearbyProviderResponse,
)
from app.services.provider_service import ProviderService

router = APIRouter(prefix="/providers", tags=["Providers"])


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
