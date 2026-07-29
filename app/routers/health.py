"""
Health probes (SVC-48).

Two endpoints, deliberately separate:

- ``/health`` — **liveness**. "Is the process alive?" Always 200 unless the
  process is dead. Used by orchestrators (ECS, Kubernetes) to decide when
  to restart a container.
- ``/healthz`` — **readiness**. "Am I ready to serve traffic?" Actually
  pings the DB and Redis. Returns 503 if any dependency is unreachable.
  Load balancers, uptime monitors (SC-48's BetterStack Uptime), and the
  Kubernetes readiness probe point here.

Each dependency check has a tight timeout so a hung DB can't hang the
probe itself.
"""
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_redis

router = APIRouter(tags=["Health"])
logger = logging.getLogger(__name__)

# Per-check budget. Kept tight so the readiness probe itself has a bounded
# worst case (~2 × timeout regardless of dependency behaviour). Longer than
# ~1s risks the LB timing out first; shorter than ~500ms produces false
# negatives under normal contention.
_CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/health")
async def liveness():
    """Liveness probe — the process is running.

    Never touches the DB / Redis. Used by container orchestrators to decide
    when to restart; a failed DB should NOT trigger a restart (that just
    thrashes the pod without fixing anything).
    """
    return {"status": "ok"}


@router.get("/healthz")
async def readiness(
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Readiness probe — the process can actually serve requests.

    Pings each dependency with a short timeout, returns a per-dependency
    result. If ANY dependency fails, response is HTTP 503 (Service
    Unavailable) — so load balancers can drain traffic from this pod without
    also killing it.
    """
    checks = {
        "database": await _check(_ping_database(db)),
        "redis": await _check(_ping_redis(redis)),
    }
    all_ok = all(v == "ok" for v in checks.values())
    if not all_ok:
        # 503 — LB and uptime monitor both understand this. Keep the body
        # detailed so ops can see which dependency failed.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("readiness probe failing: %s", checks)

    return {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Individual dependency checks
# ---------------------------------------------------------------------------


async def _ping_database(db: AsyncSession) -> None:
    """SELECT 1 — the standard "is the DB responsive" probe."""
    await db.execute(text("SELECT 1"))


async def _ping_redis(redis: Redis) -> None:
    """PING — Redis's own health probe."""
    pong = await redis.ping()
    if pong is not True:
        # Redis-py's async ping returns True on success. Anything else is a
        # protocol-level oddity — treat as failure so the probe fails loud.
        raise RuntimeError(f"redis ping returned {pong!r}")


async def _check(coro) -> str:
    """Run a check with a timeout, return 'ok' or 'error:<reason>'.

    Never raises — the readiness endpoint should always return a body even
    if every dependency is dead.
    """
    try:
        await asyncio.wait_for(coro, timeout=_CHECK_TIMEOUT_SECONDS)
        return "ok"
    except asyncio.TimeoutError:
        return "error:timeout"
    except Exception as exc:
        # First 100 chars is plenty — we're not logging stack traces in the
        # HTTP body, that's what Sentry (SC-46) is for.
        return f"error:{type(exc).__name__}:{str(exc)[:100]}"
