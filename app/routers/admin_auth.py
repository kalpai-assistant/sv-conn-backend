"""Admin console login (SVC-422). Behind the admin IP allowlist, like all of /admin."""

from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_admin, get_redis
from app.models.user import User
from app.schemas.admin_auth import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminSessionResponse,
    AdminUserResponse,
    TOTPSetupRequest,
    TOTPSetupResponse,
    TOTPVerifyRequest,
)
from app.services.admin_auth_service import AdminAuthService
from app.utils.rate_limiter import appended_client_ip, ip_rate_limit

router = APIRouter(prefix="/admin/auth", tags=["Admin Auth"])


@router.post(
    "/login",
    response_model=AdminLoginResponse,
    dependencies=[
        Depends(ip_rate_limit("admin_login", 5, 15 * 60, ip_resolver=appended_client_ip))
    ],
)
async def login(
    request: AdminLoginRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Password step. 401 ``INVALID_CREDENTIALS`` for an unknown email, wrong
    password, locked or deactivated account alike; 3 wrong passwords in a row
    lock the account for 24 hours. 429 past 5 attempts per IP per 15 minutes."""
    return await AdminAuthService(db, redis).login(request.email, request.password)


@router.post("/totp/setup", response_model=TOTPSetupResponse)
async def totp_setup(
    request: TOTPSetupRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """First login only: returns the otpauth URL for the app to draw as a QR.
    401 ``INVALID_CHALLENGE``; 409 ``TOTP_ALREADY_ACTIVE`` once activated."""
    return await AdminAuthService(db, redis).setup_totp(request.challenge_token)


@router.post("/totp/verify", response_model=AdminSessionResponse)
async def totp_verify(
    request: TOTPVerifyRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Code step: returns the 12-hour session JWT, and activates TOTP on first
    success. 401 ``INVALID_CHALLENGE`` / ``INVALID_TOTP_CODE``; 409
    ``TOTP_SETUP_REQUIRED``; 429 past 10 attempts per admin per 15 minutes."""
    return await AdminAuthService(db, redis).verify_totp(request.challenge_token, request.code)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    current: tuple[User, str] = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Ends this session: every token carrying its sid stops working."""
    _, sid = current
    await AdminAuthService(db, redis).logout(sid)


@router.get("/me", response_model=AdminUserResponse)
async def me(current: tuple[User, str] = Depends(get_current_admin)):
    admin, _ = current
    return admin
