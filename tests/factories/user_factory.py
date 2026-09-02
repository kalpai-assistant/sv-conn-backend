"""
User factory for test fixtures.
Uses direct ORM inserts (no factory_boy dependency for simplicity).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole


def _random_phone() -> str:
    r"""A unique phone that also satisfies the API's own pattern.

    ``^\+91[6-9]\d{9}$`` — Indian mobile numbers start 6-9. The old
    uuid-digit-slice was unique but usually started with something else, so a
    generated user could exist in the database yet be rejected as request input,
    which is only noticed by tests that POST the phone they just created.
    """
    n = uuid.uuid4().int
    return f"+91{6 + n % 4}{n // 4 % 10**9:09d}"


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
            phone=phone or _random_phone(),
            name=name,
            role=UserRole(role),
            is_active=is_active,
        )
        db.add(user)
        await db.flush()
        return user
