"""
User factory for test fixtures.
Uses direct ORM inserts (no factory_boy dependency for simplicity).
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole


class UserFactory:
    @staticmethod
    async def create(
        db: AsyncSession,
        role: str = "consumer",
        phone: str | None = None,
        name: str = "Test User",
        is_active: bool = True,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            phone=phone or f"+91{str(uuid.uuid4().int)[:10]}",
            name=name,
            role=UserRole(role),
            is_active=is_active,
        )
        db.add(user)
        await db.flush()
        return user
