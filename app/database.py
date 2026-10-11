import logging
from collections.abc import Callable

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Work that may only happen once the transaction is committed (SVC-348)
# ---------------------------------------------------------------------------
_AFTER_COMMIT = "after_commit_callbacks"


def call_after_commit(db: AsyncSession, fn: Callable[[], None]) -> None:
    """Run ``fn`` once ``db``'s transaction commits; drop it if it rolls back.

    For a side effect a service decides on mid-transaction but must not do
    until the change is real, such as enqueueing a push: a request that fails
    after this call must send nothing. ``fn`` runs inside the commit, so it
    must not touch the session; capture plain values, not ORM objects.
    """
    db.sync_session.info.setdefault(_AFTER_COMMIT, []).append(fn)


@event.listens_for(Session, "after_commit")
def _run_after_commit(session: Session) -> None:
    for fn in session.info.pop(_AFTER_COMMIT, []):
        try:
            fn()
        except Exception:
            # The commit has happened; a failed side effect can't undo it, and
            # raising here would turn a saved change into a 500.
            logger.exception("after_commit callback failed")


@event.listens_for(Session, "after_transaction_end")
def _drop_after_commit(session: Session, transaction) -> None:
    # The outermost transaction is over: after a commit the list is already
    # run and gone; after a rollback or a close it is dropped unrun. A
    # savepoint ending (``parent`` set) leaves it alone.
    if transaction.parent is None:
        session.info.pop(_AFTER_COMMIT, None)
