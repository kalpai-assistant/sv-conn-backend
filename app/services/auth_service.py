"""
Auth service: OTP flow + JWT token management.

Single Responsibility: this service owns the full auth lifecycle.
It does NOT touch HTTP concerns — that belongs to the router.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import InvalidOTP, RateLimitExceeded, SMSDeliveryError
from app.models.user import User, UserRole
from app.utils.otp import (
    generate_otp,
    is_rate_limited,
    otp_retry_after_seconds,
    store_otp,
    verify_otp,
)
from app.utils.security import create_access_token, create_refresh_token, decode_jwt
from app.utils.sms_client import get_sms_client

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
        # SVC-52 E2E bypass — checked first, and deliberately touching no Redis.
        #
        # For a whitelisted phone the OTP *is* `settings.E2E_TEST_OTP`: a
        # constant both ends already know. Writing that constant to Redis and
        # reading it back proves nothing about the caller, only that Redis is
        # up — so the round trip is dropped and `verify_otp` compares against
        # the setting directly.
        #
        # Dropping it also makes the bypass work when Redis does not. On
        # 2026-08-24 the Upstash request quota was exhausted and this path
        # died at `is_rate_limited()` before ever reaching the bypass, which
        # is exactly the situation a dependency-free login route exists for.
        #
        # ⚠️ This does remove per-phone rate limiting for whitelisted numbers,
        # which the comment here previously promised. On a fixed test phone
        # with a fixed code the limiter guarded very little; the three gates
        # in `_is_e2e_test_phone` — never in production, OTP must be
        # configured, phone must be listed — are what actually contain this.
        if self._is_e2e_test_phone(phone):
            logger.info("auth.e2e_bypass phone=%s", phone)
            return

        if await is_rate_limited(self.redis, phone):
            retry = await otp_retry_after_seconds(self.redis, phone)
            raise RateLimitExceeded(retry_after=retry)

        otp = generate_otp(settings.OTP_LENGTH)
        logger.info(f"\n{'='*40}\n🔑 [MOCK OTP] Use {otp} to login for {phone}\n{'='*40}\n")
        await store_otp(self.redis, phone, otp)
        try:
            await self._sms.send_otp(phone, otp)
        except SMSDeliveryError:
            # Let the router surface 502 to the client — a silent 200
            # here (the old behavior) told the app "OTP sent" while the
            # SMS was actually dropped, so testers/users waited forever
            # for a code that would never arrive. The OTP is already in
            # Redis, so a caller that retries with the correct number
            # after ops fixes the provider still works.
            raise
        except Exception as e:
            # Anything unexpected (network blip, timeout, bug in the
            # provider client) also becomes a 502. Log so we can triage
            # from CloudWatch / Sentry.
            logger.exception("Unexpected SMS send failure for %s", phone)
            raise SMSDeliveryError(f"SMS provider error: {e}") from e

    @staticmethod
    def _is_e2e_test_phone(phone: str) -> bool:
        """True iff this phone is on the E2E bypass whitelist AND the bypass
        is fully configured AND we're not in production.

        Belt-and-suspenders: three independent checks, ALL of which must
        pass. Any one being wrong (empty whitelist, empty test OTP, or
        production env) disables the bypass entirely.
        """
        if settings.ENVIRONMENT == "production":
            return False
        if not settings.E2E_TEST_OTP:
            return False
        whitelist = {p.strip() for p in settings.E2E_TEST_PHONES.split(",") if p.strip()}
        return phone in whitelist

    async def verify_otp(self, phone: str, otp: str, role: str) -> dict:
        """
        Verify the OTP. On success, upsert the user and return JWT tokens.
        Returns a dict matching TokenResponse schema.
        """
        if self._is_e2e_test_phone(phone):
            # Counterpart to the bypass in `send_otp`: nothing was stored, so
            # compare against the configured constant. `compare_digest` rather
            # than `==` to keep it constant-time — the value is short and
            # fixed, so a timing oracle is worth denying even here.
            valid = secrets.compare_digest(otp, settings.E2E_TEST_OTP)
        else:
            valid = await verify_otp(self.redis, phone, otp)
        if not valid:
            raise InvalidOTP("OTP is incorrect or has expired")

        user, is_new = await self._upsert_user(phone, UserRole(role))
        return self._build_token_response(user, is_new)

    async def refresh_tokens(self, refresh_token: str) -> dict:
        """Decode a refresh token and issue a fresh access + refresh pair.

        SVC-241: also refuses when the token's `jti` is on the Redis blacklist
        (populated by ``logout``). Without this a stolen refresh token stays
        good for its full 30-day life.
        """
        payload = decode_jwt(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        # SVC-241: blacklist check. `jti` is set on every refresh token issued
        # after this ships; older tokens without a jti still work (grandfathered)
        # so a mid-deploy user isn't force-logged-out.
        jti = payload.get("jti")
        if jti and await self.redis.exists(f"jwt:blacklist:{jti}"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token has been revoked",
            )

        user = await self.db.get(User, uuid.UUID(payload["sub"]))
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )

        return self._build_token_response(user, is_new=False)

    async def logout(self, refresh_token: str) -> None:
        """SVC-241: blacklist the refresh token's `jti` in Redis with a TTL
        equal to the token's remaining lifetime. Subsequent calls to
        ``/auth/refresh`` with this token 401 immediately.

        Silently succeeds on a malformed / already-expired token — logout is
        an idempotent, user-driven signal, not a security check. The access
        token (short-lived, ~15 min) is deliberately not blacklisted; the
        cost of tracking every access-token jti buys 15 min of extra safety
        for a token the client already stopped using.
        """
        payload = decode_jwt(refresh_token)
        if not payload or payload.get("type") != "refresh":
            return
        jti = payload.get("jti")
        exp = payload.get("exp")
        if not jti or not isinstance(exp, int):
            return
        now = int(datetime.now(UTC).timestamp())
        ttl = max(1, exp - now)
        await self.redis.setex(f"jwt:blacklist:{jti}", ttl, "1")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _upsert_user(self, phone: str, role: UserRole) -> tuple[User, bool]:
        """Get existing user or create a new one. Returns (user, is_new).

        SVC-253 policy A: a phone is bound to a single role for its lifetime.
        Attempting to re-authenticate the same phone with a different role
        errors clearly instead of silently returning the original role — which
        used to bite when a consumer tried to onboard as a provider on the
        same number and got a JWT that never matched provider routes.
        """
        result = await self.db.execute(select(User).where(User.phone == phone))
        user = result.scalar_one_or_none()
        now = datetime.now(UTC)

        if user is not None:
            if user.role != role:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"This phone is already registered as {user.role.value}. "
                    f"Sign in with the same role or use a different phone.",
                )
            # SVC-240: stamp last_login_at on every successful sign-in.
            user.last_login_at = now
            await self.db.commit()
            return user, False

        user = User(phone=phone, role=role, last_login_at=now)
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user, True

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
