from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_redis
from app.schemas.auth import (
    AdminVerifyOTPRequest,
    LogoutRequest,
    RefreshTokenRequest,
    SendOTPRequest,
    TokenResponse,
    VerifyOTPRequest,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/send-otp", status_code=status.HTTP_200_OK)
async def send_otp(
    request: SendOTPRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await AuthService(db, redis).send_otp(request.phone)
    return {"message": "OTP sent successfully"}


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(
    request: VerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    return await AuthService(db, redis).verify_otp(
        phone=request.phone, otp=request.otp, role=request.role
    )


# ---------------------------------------------------------------------------
# SVC-345: dedicated admin login endpoints
#
# Live under /auth/admin/* so they share the auth router's prefix without
# tripping AdminIPAllowlistMiddleware (which gates /api/v1/admin/*, not
# /api/v1/auth/admin/*). Admins must be able to log in from any IP; the
# actual admin API endpoints stay allowlist-gated.
# ---------------------------------------------------------------------------


@router.post("/admin/send-otp", status_code=status.HTTP_200_OK)
async def admin_send_otp(
    request: SendOTPRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await AuthService(db, redis).admin_send_otp(request.phone)
    return {"message": "OTP sent successfully"}


@router.post("/admin/verify-otp", response_model=TokenResponse)
async def admin_verify_otp(
    request: AdminVerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    return await AuthService(db, redis).admin_verify_otp(phone=request.phone, otp=request.otp)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    return await AuthService(db, redis).refresh_tokens(request.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: LogoutRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """SVC-241: revoke a refresh token so /auth/refresh with it returns 401.

    Silently succeeds on any token — even malformed / already-expired — so
    the client can call this once, drop its stored tokens, and never care
    whether the server had anything to blacklist.
    """
    await AuthService(db, redis).logout(request.refresh_token)
