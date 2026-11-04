"""Admin console login (SVC-422): email + password, then TOTP, then a session JWT."""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
from fastapi import status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import ADMIN_SESSION_BLACKLIST_PREFIX
from app.exceptions import AppException, RateLimitExceeded
from app.models.user import AdminRole, User, UserRole, admin_placeholder_phone
from app.utils.encryption import decrypt, encrypt
from app.utils.security import (
    ADMIN_CHALLENGE_TTL,
    ADMIN_SESSION_TTL,
    ADMIN_TOKEN_TYPE_CHALLENGE,
    admin_password_problem,
    create_admin_challenge_token,
    create_admin_session_token,
    decode_admin_token,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)

MAX_FAILED_LOGINS = 3
LOCKOUT = timedelta(hours=24)
TOTP_MAX_ATTEMPTS = 10
TOTP_WINDOW_SECONDS = 15 * 60
TOTP_ISSUER = "EazyServ Admin"
# verify() accepts the previous, current and next 30 s step, so a used code must
# stay refused for at least that long.
TOTP_VALID_WINDOW = 1
TOTP_USED_CODE_TTL_SECONDS = 120

# Compared against when there is no real hash to check, so an unknown email or a
# locked account takes as long as a wrong password.
_dummy_hash: str | None = None


def _get_dummy_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(uuid.uuid4().hex)
    return _dummy_hash


class InvalidAdminCredentials(AppException):
    """Unknown email, wrong password, locked and deactivated all raise this."""

    def __init__(self):
        super().__init__(
            "Invalid email or password",
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_CREDENTIALS",
        )


class InvalidChallenge(AppException):
    def __init__(self):
        super().__init__(
            "Login has expired, please sign in again",
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_CHALLENGE",
        )


class InvalidTOTPCode(AppException):
    def __init__(self):
        super().__init__(
            "Code is incorrect",
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="INVALID_TOTP_CODE",
        )


class TOTPAlreadyActive(AppException):
    def __init__(self):
        super().__init__(
            "Authenticator is already set up",
            status_code=status.HTTP_409_CONFLICT,
            code="TOTP_ALREADY_ACTIVE",
        )


class TOTPSetupRequired(AppException):
    def __init__(self):
        super().__init__(
            "Authenticator has not been set up",
            status_code=status.HTTP_409_CONFLICT,
            code="TOTP_SETUP_REQUIRED",
        )


def _can_sign_in(admin: User, now: datetime | None = None) -> bool:
    locked = admin.locked_until is not None and admin.locked_until > (now or datetime.now(UTC))
    return admin.is_active and admin.admin_role is not None and not locked


class AdminAuthService:
    def __init__(self, db: AsyncSession, redis: Redis):
        self.db = db
        self.redis = redis

    async def login(self, email: str, password: str) -> dict:
        """Password step. The row is locked so concurrent bad guesses can't each
        read the same count. Every refusal is committed before raising, because
        ``get_db`` rolls back on an exception and would undo the failure count."""
        now = datetime.now(UTC)
        result = await self.db.execute(
            select(User)
            .where(User.email == email.strip().lower(), User.role == UserRole.ADMIN)
            .with_for_update()
        )
        admin = result.scalar_one_or_none()

        # password_hash is NULL on an admin who predates SVC-422 until the script runs.
        if admin is None or admin.password_hash is None or not _can_sign_in(admin, now):
            await asyncio.to_thread(verify_password, password, _get_dummy_hash())
            raise InvalidAdminCredentials()

        if admin.locked_until is not None:
            # The lock ran out: three fresh attempts.
            admin.locked_until = None
            admin.failed_login_count = 0

        if not await asyncio.to_thread(verify_password, password, admin.password_hash):
            admin.failed_login_count += 1
            if admin.failed_login_count >= MAX_FAILED_LOGINS:
                admin.locked_until = now + LOCKOUT
                logger.warning("admin_auth.locked admin_id=%s", admin.id)
            await self.db.commit()
            raise InvalidAdminCredentials()

        admin.failed_login_count = 0
        setup_required = admin.totp_enabled_at is None
        challenge = create_admin_challenge_token(admin.id)
        await self.db.commit()
        return {
            "requires_totp": True,
            "totp_setup_required": setup_required,
            "challenge_token": challenge,
        }

    async def setup_totp(self, challenge_token: str) -> dict:
        """Start authenticator setup. A new secret on every call, so a secret
        someone saw earlier (with the password) can't outlive this setup."""
        admin, _ = await self._admin_from_challenge(challenge_token)
        if admin.totp_enabled_at is not None:
            raise TOTPAlreadyActive()
        secret = pyotp.random_base32()
        admin.totp_secret = encrypt(secret)
        url = pyotp.TOTP(secret).provisioning_uri(name=admin.email, issuer_name=TOTP_ISSUER)
        await self.db.commit()
        return {"otpauth_url": url}

    async def verify_totp(self, challenge_token: str, code: str) -> dict:
        """Code step: activates TOTP on first success and returns a session."""
        admin, payload = await self._admin_from_challenge(challenge_token)
        await self._count_totp_attempt(admin.id)

        if admin.totp_secret is None:
            raise TOTPSetupRequired()
        if not pyotp.TOTP(decrypt(admin.totp_secret)).verify(code, valid_window=TOTP_VALID_WINDOW):
            raise InvalidTOTPCode()

        # Both are single-use. SET NX is atomic, so of two parallel requests with
        # the same code or challenge exactly one gets through.
        used_key = f"admin:totp:used:{admin.id}:{code}"
        if not await self.redis.set(used_key, "1", nx=True, ex=TOTP_USED_CODE_TTL_SECONDS):
            raise InvalidTOTPCode()
        spent_key = f"admin:challenge:spent:{payload['jti']}"
        if not await self.redis.set(
            spent_key, "1", nx=True, ex=int(ADMIN_CHALLENGE_TTL.total_seconds())
        ):
            raise InvalidChallenge()

        now = datetime.now(UTC)
        if admin.totp_enabled_at is None:
            admin.totp_enabled_at = now
        admin.last_login_at = now
        await self.db.commit()
        return {
            "access_token": create_admin_session_token(admin.id, uuid.uuid4().hex),
            "token_type": "bearer",
            "expires_in": int(ADMIN_SESSION_TTL.total_seconds()),
            "admin": admin,
        }

    async def logout(self, sid: str) -> None:
        """Ends every token of this session. No token of it can outlive a full
        TTL from now, since the newest one was minted no later than now."""
        await self.redis.setex(
            f"{ADMIN_SESSION_BLACKLIST_PREFIX}{sid}", int(ADMIN_SESSION_TTL.total_seconds()), "1"
        )

    async def _admin_from_challenge(self, challenge_token: str) -> tuple[User, dict]:
        payload = decode_admin_token(challenge_token, ADMIN_TOKEN_TYPE_CHALLENGE)
        if payload is None or await self.redis.exists(f"admin:challenge:spent:{payload['jti']}"):
            raise InvalidChallenge()
        admin = await self.db.get(User, uuid.UUID(payload["sub"]), with_for_update=True)
        if admin is None or admin.role != UserRole.ADMIN or not _can_sign_in(admin):
            raise InvalidChallenge()
        return admin, payload

    async def _count_totp_attempt(self, admin_id: uuid.UUID) -> None:
        """Ticket: 10 per session per 15 min. Keyed on the admin, not the token,
        because logging in again would otherwise buy 10 more guesses."""
        key = f"rl:admin_totp:{admin_id}"
        current = await self.redis.incr(key)
        if current == 1:
            await self.redis.expire(key, TOTP_WINDOW_SECONDS)
        if current > TOTP_MAX_ATTEMPTS:
            ttl = await self.redis.ttl(key)
            retry_after = ttl if ttl > 0 else TOTP_WINDOW_SECONDS
            raise RateLimitExceeded(
                retry_after=retry_after,
                limit=TOTP_MAX_ATTEMPTS,
                reset_at=int(time.time()) + retry_after,
            )


async def provision_admin(
    db: AsyncSession, email: str, role: AdminRole, password: str, phone: str | None = None
) -> User:
    """Give someone a console login. Caller commits.

    With ``phone``, an existing admin at that number gets the login on its own row,
    so its history stays with it, and a new number becomes a new row. Without one,
    a new row gets a placeholder phone.

    Raises ValueError on a bad password, an email already in use, a phone belonging
    to a consumer or provider, or an admin who already has a login.
    """
    problem = admin_password_problem(password)
    if problem:
        raise ValueError(f"Password {problem}")
    email = email.strip().lower()
    if "@" not in email or len(email) > 255:
        raise ValueError(f"{email} is not an email address")
    taken = await db.execute(
        select(User.id).where(User.email == email, User.role == UserRole.ADMIN)
    )
    if taken.scalar_one_or_none() is not None:
        raise ValueError(f"{email} already has an admin login")

    user = None
    if phone is not None:
        result = await db.execute(select(User).where(User.phone == phone))
        user = result.scalar_one_or_none()
    if user is None:
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            phone=phone or admin_placeholder_phone(user_id),
            name=email[:100],
            role=UserRole.ADMIN,
        )
        db.add(user)
    elif user.role != UserRole.ADMIN:
        raise ValueError(f"{phone} is a {user.role.value} account, not an admin")
    elif user.password_hash is not None:
        raise ValueError(f"{phone} already has an admin login")

    user.email = email
    user.password_hash = hash_password(password)
    user.admin_role = role
    user.is_active = True
    await db.flush()
    return user
