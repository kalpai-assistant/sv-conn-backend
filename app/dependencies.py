"""
FastAPI shared dependencies: DB session, Redis, current user resolution, RBAC.
"""

from uuid import UUID

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.user import User
from app.utils.security import decode_jwt

settings = get_settings()
# auto_error=False: FastAPI's default answers a missing (or non-Bearer)
# Authorization header with 403, which is "you may not", not "who are you?".
# The app signs out on a 401 only, so a client calling with no token was told
# nothing it could act on and just retried. get_current_user raises the 401.
security = HTTPBearer(auto_error=False)

# RFC 6750: a 401 for a bearer-protected resource names the scheme.
_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

# Redis connection pool — initialised in lifespan, shared across requests
_redis_pool: AsyncRedis | None = None


async def get_redis() -> AsyncRedis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = AsyncRedis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_pool


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate JWT; return the active User object.

    SVC-47: also attaches the user to the current Sentry scope so events
    raised during the request are filterable by user_id in the Sentry
    UI. `send_default_pii=False` is set at init time, so only the id +
    role make it into Sentry — never the phone number or email.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers=_BEARER_CHALLENGE,
        )
    payload = decode_jwt(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers=_BEARER_CHALLENGE,
        )

    user = await db.get(User, UUID(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers=_BEARER_CHALLENGE,
        )
    _tag_sentry_user(str(user.id), user.role.value)
    return user


def _tag_sentry_user(user_id: str, role: str) -> None:
    """Bind user context to the current Sentry scope. Safe no-op when SDK
    isn't initialised (SENTRY_DSN empty)."""
    try:
        import sentry_sdk

        sentry_sdk.set_user({"id": user_id, "role": role})
    except Exception:
        # Never let Sentry issues break auth.
        pass


def require_role(*roles: str):
    """
    Dependency factory for role-based access control.

    Usage:
        @router.get("/admin/...", dependencies=[Depends(require_role("admin"))])
    """

    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role.value not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role: {', '.join(roles)}",
            )
        return user

    return checker


async def verify_ws_token(websocket: WebSocket) -> dict | None:
    """
    Verify JWT from WebSocket query param ?token=<jwt>.
    Closes the connection with code 4001 on failure.
    Returns the decoded payload on success.
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return None

    payload = decode_jwt(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid token")
        return None

    return payload
