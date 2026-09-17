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


_UNSET = object()


class UserFactory:
    @staticmethod
    async def create(
        db: AsyncSession,
        role: str = "consumer",
        phone: str | None = None,
        name: str = "Test User",
        is_active: bool = True,
        fcm_token: str | None = _UNSET,  # type: ignore[assignment]
    ) -> User:
        """Create a User row.

        ``fcm_token`` defaults to a unique test token when ``role='provider'``
        (SVC-276 — the broadcast eligibility filter now requires one; without
        this default every existing provider fixture would go silently
        ineligible), and to ``None`` for every other role so that
        ``test_user_fcm_token`` continues to observe the real "no token
        registered yet" starting state. Pass ``fcm_token=None`` explicitly on a
        provider to model an unreachable one.
        """
        if fcm_token is _UNSET:
            fcm_token = f"fcm-test-{uuid.uuid4().hex[:16]}" if role == "provider" else None
        user = User(
            id=uuid.uuid4(),
            phone=phone or _random_phone(),
            name=name,
            role=UserRole(role),
            is_active=is_active,
            fcm_token=fcm_token,
        )
        db.add(user)
        await db.flush()
        return user
