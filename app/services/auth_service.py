"""
Auth service: OTP flow + JWT token management.

Single Responsibility: this service owns the full auth lifecycle.
It does NOT touch HTTP concerns — that belongs to the router.
"""
import uuid
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

from app.models.user import User, UserRole
from app.utils.otp import generate_otp, store_otp, verify_otp, is_rate_limited, otp_retry_after_seconds
from app.utils.security import create_access_token, create_refresh_token, decode_jwt
from app.utils.sms_client import get_sms_client
from app.exceptions import InvalidOTP, RateLimitExceeded
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class AuthService:
    """Handles OTP sending, verification, and token issuance."""

    def __init__(self, db: AsyncSession, redis: Redis):
        self.db = db
        self.redis = redis
        self._sms = get_sms_client()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def send_otp(self, phone: str) -> None:
        """Generate OTP, enforce rate limit, persist in Redis, send via SMS."""
        if await is_rate_limited(self.redis, phone):
            retry = await otp_retry_after_seconds(self.redis, phone)
            raise RateLimitExceeded(retry_after=retry)

        otp = generate_otp(settings.OTP_LENGTH)
        logger.info(f"\n{'='*40}\n🔑 [MOCK OTP] Use {otp} to login for {phone}\n{'='*40}\n")
        await store_otp(self.redis, phone, otp)
        try:
            await self._sms.send_otp(phone, otp)
        except Exception as e:
            logger.warning(f"SMS sending failed (expected if keys missing): {e}")

    async def verify_otp(self, phone: str, otp: str, role: str) -> dict:
        """
        Verify the OTP. On success, upsert the user and return JWT tokens.
        Returns a dict matching TokenResponse schema.
        """
        valid = await verify_otp(self.redis, phone, otp)
        if not valid:
            raise InvalidOTP("OTP is incorrect or has expired")

        user, is_new = await self._upsert_user(phone, UserRole(role))
        return self._build_token_response(user, is_new)

    async def refresh_tokens(self, refresh_token: str) -> dict:
        """Decode a refresh token and issue a fresh access + refresh pair."""
        payload = decode_jwt(refresh_token)
        if not payload or payload.get("type") != "refresh":
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        user = await self.db.get(User, uuid.UUID(payload["sub"]))
        if not user or not user.is_active:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )

        return self._build_token_response(user, is_new=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _upsert_user(self, phone: str, role: UserRole) -> tuple[User, bool]:
        """Get existing user or create a new one. Returns (user, is_new)."""
        result = await self.db.execute(select(User).where(User.phone == phone))
        user = result.scalar_one_or_none()

        is_new = user is None
        if is_new:
            user = User(phone=phone, role=role)
            self.db.add(user)
            await self.db.commit()
            await self.db.refresh(user)

        return user, is_new

    @staticmethod
    def _build_token_response(user: User, is_new: bool) -> dict:
        token_data = {"sub": str(user.id), "role": user.role.value}
        return {
            "access_token": create_access_token(token_data),
            "refresh_token": create_refresh_token({**token_data, "type": "refresh"}),
            "token_type": "bearer",
            "user_id": str(user.id),
            "role": user.role.value,
            "is_new_user": is_new,
        }
