from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# Async engine — used by FastAPI request handlers
# ---------------------------------------------------------------------------
# `pool_pre_ping=True`: Neon's serverless Postgres suspends a compute after
# ~5 min idle. Our pool then holds sockets to a compute that's gone; the
# NEXT request pulls a "connection is closed" pool entry and 500s. Bit
# every path this session that was the first hit after a lull — the
# SVC-227.2 workflow retries, my ad-hoc migrate probes, and PR #124's
# release call. `pool_pre_ping` issues a lightweight SELECT 1 before
# checkout; a dead connection is silently replaced. Trades one round-trip
# per checkout for zero cold-start 500s.
#
# `pool_recycle=300` closes any pooled connection older than 5 min so we
# don't sit on a Neon-suspended socket in the first place. Belt on top
# of the pre-ping suspenders.
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=settings.DEBUG,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ---------------------------------------------------------------------------
# Sync engine — used exclusively by Celery tasks (workers run in threads)
# ---------------------------------------------------------------------------
_sync_url = settings.SYNC_DATABASE_URL or settings.DATABASE_URL.replace(
    "postgresql+asyncpg", "postgresql+psycopg2"
)

sync_engine = create_engine(
    _sync_url,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=settings.DEBUG,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    autocommit=False,
    class_=Session,
)


# ---------------------------------------------------------------------------
# ORM base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------
async def get_db() -> AsyncSession:  # type: ignore[override]
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
