import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.user import (
    AddressResponse,
    CreateAddressRequest,
    PushCapabilityReport,
    UpdateFCMTokenRequest,
    UpdateProfileRequest,
    UserResponse,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["Users"])

logger = logging.getLogger(__name__)


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


@router.post("/me/push-capability", status_code=status.HTTP_204_NO_CONTENT)
async def report_push_capability(
    report: PushCapabilityReport,
    current_user: User = Depends(get_current_user),
):
    """Record what the caller's device can do about push (SVC-255). Logs only.

    SVC-255 is about providers who never receive notifications, and nobody can
    currently say how many that is. Sentry captures registration *failures*, but
    with no denominator — and no existing request carries the platform, so there
    is nothing to mine retroactively. The version check looks like it would help
    and does not: it reads a static `latest.json` and picks the platform branch
    on the client, so the server never sees which one.

    Deliberately stores nothing. One structured line, counted in CloudWatch
    Insights. The question is "should we invest in the iOS path at all", which
    needs answering once — a column on `users` would answer a different
    question ("which providers should we chase") and is only worth adding if
    the distribution says the problem is real.

    Fire-and-forget from the client's side: a failure here must never affect
    whether push itself registers.
    """
    logger.info(
        "push.capability user=%s role=%s platform=%s native=%s standalone=%s "
        "chrome_ios=%s permission=%s token=%s interactive=%s",
        current_user.id,
        current_user.role.value,
        report.platform,
        report.is_native,
        report.ios_standalone,
        report.is_chrome_ios,
        report.push_permission,
        report.token_obtained,
        report.interactive,
    )


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
