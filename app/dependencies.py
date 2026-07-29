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
security = HTTPBearer()

# Redis connection pool — initialised in lifespan, shared across requests
_redis_pool: AsyncRedis | None = None


async def get_redis() -> AsyncRedis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = AsyncRedis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_pool


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate JWT; return the active User object."""
    payload = decode_jwt(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user = await db.get(User, UUID(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


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
