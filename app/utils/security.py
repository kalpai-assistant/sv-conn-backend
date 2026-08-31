"""
Security helpers: JWT encoding/decoding + password hashing.
All JWT operations are centralised here — no other module touches jose directly.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

settings = get_settings()

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# Password helpers (used for admin accounts; OTP users skip this)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _build_token(data: dict[str, Any], expires_delta: timedelta) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(UTC) + expires_delta
    payload["iat"] = datetime.now(UTC)
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(data: dict[str, Any]) -> str:
    return _build_token(
        data,
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def create_refresh_token(data: dict[str, Any]) -> str:
    # SVC-241: every refresh token gets a `jti` (JWT ID) so logout can
    # blacklist it in Redis keyed on this stable identifier. Access tokens
    # deliberately don't need one — they're short-lived and the blacklist
    # cost per hop isn't worth the marginal safety window.
    payload = data.copy()
    payload.setdefault("jti", uuid.uuid4().hex)
    return _build_token(
        payload,
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )


def decode_jwt(token: str) -> dict[str, Any] | None:
    """Return decoded payload or None on any failure (expired, invalid, etc.)."""
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError:
        return None
