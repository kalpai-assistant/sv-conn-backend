import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.database import Base, get_db
from app.dependencies import get_redis
from app.main import create_app
from app.utils.security import create_access_token

TEST_DATABASE_URL = "postgresql+asyncpg://sc_user:sc_password@localhost:5432/sc_test"


@pytest.fixture
async def db_session():
    import app.models  # noqa: F401

    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()

    await engine.dispose()


@pytest.fixture
async def redis_client():
    """Per-test Redis client, closed at teardown.

    Bypasses the `_redis_pool` module-global in `app.dependencies` — that
    global caches a client tied to the event loop of the first test that
    used it, causing "Event loop is closed" errors on subsequent tests
    (each test has its own loop). Each test getting its own client + a
    clean shutdown avoids the leak.
    """
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def client(db_session, redis_client):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    # Override the module-global `get_redis` so per-test dependencies see
    # the per-test client (see redis_client fixture for the "why").
    app.dependency_overrides[get_redis] = lambda: redis_client

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.fixture
def auth_headers():
    """Generate JWT authorization headers for a given user_id and role."""

    def _make(user_id: str, role: str = "consumer") -> dict:
        token = create_access_token({"sub": user_id, "role": role})
        return {"Authorization": f"Bearer {token}"}

    return _make
