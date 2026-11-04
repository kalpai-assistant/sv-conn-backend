"""
Security helpers: JWT encoding/decoding + password hashing.
All JWT operations are centralised here — no other module touches jose directly.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()

# bcrypt only reads 72 bytes, and bcrypt 5 raises rather than truncating.
_BCRYPT_MAX_BYTES = 72
ADMIN_PASSWORD_MIN_LENGTH = 12

# SVC-422: admin tokens carry an audience. ``decode_jwt`` passes none, and jose
# then refuses any token that has one, so every non-admin path (get_current_user,
# /auth/refresh, the WebSocket check) rejects admin tokens without a change.
ADMIN_TOKEN_AUDIENCE = "eazyserv-admin"
ADMIN_TOKEN_TYPE_SESSION = "admin"
ADMIN_TOKEN_TYPE_CHALLENGE = "admin_challenge"
ADMIN_SESSION_TTL = timedelta(hours=12)
ADMIN_CHALLENGE_TTL = timedelta(minutes=5)

# ---------------------------------------------------------------------------
# Password helpers (used for admin accounts; OTP users skip this)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Raises ValueError past 72 bytes; check ``admin_password_problem`` first."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    encoded = plain.encode()
    # A login body can hold anything; too long is a wrong password, not a 500.
    if len(encoded) > _BCRYPT_MAX_BYTES:
        return False
    return bcrypt.checkpw(encoded, hashed.encode())


def admin_password_problem(plain: str) -> str | None:
    """Why this can't be an admin password, or None if it can."""
    if len(plain) < ADMIN_PASSWORD_MIN_LENGTH:
        return f"must be at least {ADMIN_PASSWORD_MIN_LENGTH} characters"
    if len(plain.encode()) > _BCRYPT_MAX_BYTES:
        return f"must be at most {_BCRYPT_MAX_BYTES} bytes"
    return None


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


def decode_access_token(token: str) -> dict[str, Any] | None:
    """``decode_jwt`` for anything that grants access: a refresh token is refused.

    Both kinds are signed the same way, so without this check a 30-day refresh
    token also worked as a bearer token. Refresh tokens are only for
    ``/auth/refresh`` and ``/auth/logout``, which read them with ``decode_jwt``.
    """
    payload = decode_jwt(token)
    if payload is None or payload.get("type") == "refresh":
        return None
    return payload


# ---------------------------------------------------------------------------
# Admin console tokens (SVC-422)
# ---------------------------------------------------------------------------


def create_admin_session_token(
    admin_id: uuid.UUID, sid: str, expires_delta: timedelta = ADMIN_SESSION_TTL
) -> str:
    """Role is deliberately not a claim: it is read from the DB on every request."""
    return _build_token(
        {
            "sub": str(admin_id),
            "sid": sid,
            "typ": ADMIN_TOKEN_TYPE_SESSION,
            "aud": ADMIN_TOKEN_AUDIENCE,
        },
        expires_delta,
    )


def create_admin_challenge_token(
    admin_id: uuid.UUID, expires_delta: timedelta = ADMIN_CHALLENGE_TTL
) -> str:
    """Proves the password step passed. Only the TOTP endpoints accept it."""
    return _build_token(
        {
            "sub": str(admin_id),
            "jti": uuid.uuid4().hex,
            "typ": ADMIN_TOKEN_TYPE_CHALLENGE,
            "aud": ADMIN_TOKEN_AUDIENCE,
        },
        expires_delta,
    )


def decode_admin_token(token: str, expected_type: str) -> dict[str, Any] | None:
    """Return the payload of a valid admin token of this type, else None."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            audience=ADMIN_TOKEN_AUDIENCE,
        )
    except JWTError:
        return None
    return payload if payload.get("typ") == expected_type else None
