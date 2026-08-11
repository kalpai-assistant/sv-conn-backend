from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.user import (
    AddressResponse,
    CreateAddressRequest,
    UpdateFCMTokenRequest,
    UpdateProfileRequest,
    UserResponse,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserResponse)
async def update_me(
    request: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await UserService(db).update_profile(
        user_id=current_user.id,
        name=request.name,
        email=request.email,
    )


@router.patch("/me/fcm-token", status_code=status.HTTP_204_NO_CONTENT)
async def update_fcm_token(
    request: UpdateFCMTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register the caller's FCM token, or clear it by sending `null` on sign-out.

    The field is required, so an empty body is rejected — clearing must be explicit.
    """
    await UserService(db).update_fcm_token(current_user.id, request.fcm_token)


@router.get("/me/addresses", response_model=list[AddressResponse])
async def list_addresses(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await UserService(db).list_addresses(current_user.id)


@router.post("/me/addresses", response_model=AddressResponse, status_code=status.HTTP_201_CREATED)
async def add_address(
    request: CreateAddressRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await UserService(db).add_address(
        user_id=current_user.id,
        label=request.label,
        full_address=request.full_address,
        lat=request.lat,
        lng=request.lng,
        is_default=request.is_default,
    )


@router.delete("/me/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(
    address_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from uuid import UUID

    await UserService(db).delete_address(current_user.id, UUID(address_id))
