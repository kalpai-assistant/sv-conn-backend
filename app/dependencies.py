"""
FastAPI shared dependencies: DB session, Redis, current user resolution, RBAC.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, HTTPException, Response, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.user import User, UserRole
from app.utils.security import (
    ADMIN_SESSION_TTL,
    ADMIN_TOKEN_TYPE_SESSION,
    create_admin_session_token,
    decode_access_token,
    decode_admin_token,
)

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
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers=_BEARER_CHALLENGE,
        )

    user = await db.get(User, UUID(payload["sub"]))
    # SVC-422: an admin only ever holds a console session, which never gets here.
    # The role check catches a phone token issued before the user became an admin.
    if not user or not user.is_active or user.role == UserRole.ADMIN:
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


# ---------------------------------------------------------------------------
# Admin console (SVC-422)
# ---------------------------------------------------------------------------

ADMIN_SESSION_BLACKLIST_PREFIX = "admin:session:revoked:"
ADMIN_TOKEN_HEADER = "X-Admin-Token"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers=_BEARER_CHALLENGE
    )


async def _load_admin_session(
    payload: dict, response: Response, db: AsyncSession, redis: AsyncRedis
) -> User:
    """The active admin behind a decoded session token. Role and is_active come
    from the DB on every request, never from the JWT.

    Past half its life the token is replaced: a fresh one with the same ``sid``
    goes out in ``X-Admin-Token``, so logout still ends the whole session. The
    old token stays valid until it expires, so parallel requests can't race.
    """
    sid = payload.get("sid")
    if not sid or await redis.exists(f"{ADMIN_SESSION_BLACKLIST_PREFIX}{sid}"):
        raise _unauthorized("Session has ended")

    admin = await db.get(User, UUID(payload["sub"]))
    if (
        admin is None
        or not admin.is_active
        or admin.role != UserRole.ADMIN
        or admin.admin_role is None
    ):
        raise _unauthorized("User not found or inactive")

    remaining = payload["exp"] - datetime.now(UTC).timestamp()
    if remaining < ADMIN_SESSION_TTL.total_seconds() / 2:
        response.headers[ADMIN_TOKEN_HEADER] = create_admin_session_token(admin.id, sid)
    return admin


async def authorize_admin_token(
    token: str,
    roles: tuple[str, ...],
    response: Response,
    db: AsyncSession,
    redis: AsyncRedis,
) -> User:
    """Check a bearer token against the roles a route allows; return the admin.

    Only a console session gets through. Any other token that decodes is a 403 for
    a consumer or provider and a 401 for an admin, whose old phone-login tokens no
    longer work (SVC-422). A challenge token decodes as neither, so a 401.
    """
    payload = decode_admin_token(token, ADMIN_TOKEN_TYPE_SESSION)
    if payload is not None:
        admin = await _load_admin_session(payload, response, db, redis)
        if admin.admin_role.value not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role: {', '.join(roles)}",
            )
        _tag_sentry_user(str(admin.id), admin.admin_role.value)
        return admin

    other = decode_access_token(token)
    if not other:
        raise _unauthorized("Invalid or expired token")
    user = await db.get(User, UUID(other["sub"]))
    if user is None or not user.is_active or user.role == UserRole.ADMIN:
        raise _unauthorized("User not found or inactive")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Requires role: {', '.join(roles)}",
    )


def require_roles(*roles: str):
    """Dependency factory for ``/admin/*`` routes: which admin roles may call this.

    Separate from ``require_role`` (provider routes use that) because it takes
    console sessions, which ``get_current_user`` refuses. Returns the admin.

        @router.post("/admin/...", dependencies=[Depends(require_roles("admin", "ops"))])
    """

    async def checker(
        response: Response,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        db: AsyncSession = Depends(get_db),
        redis: AsyncRedis = Depends(get_redis),
    ) -> User:
        if credentials is None:
            raise _unauthorized("Not authenticated")
        return await authorize_admin_token(credentials.credentials, roles, response, db, redis)

    return checker


async def get_current_admin(
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
    redis: AsyncRedis = Depends(get_redis),
) -> tuple[User, str]:
    """The console session calling ``/admin/auth/me`` or ``logout``, with its sid."""
    if credentials is None:
        raise _unauthorized("Not authenticated")
    payload = decode_admin_token(credentials.credentials, ADMIN_TOKEN_TYPE_SESSION)
    if payload is None:
        raise _unauthorized("Invalid or expired token")
    admin = await _load_admin_session(payload, response, db, redis)
    return admin, payload["sid"]


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

    payload = decode_access_token(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid token")
        return None

    return payload
