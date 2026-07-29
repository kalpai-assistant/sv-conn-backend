import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
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
async def client(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session

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
