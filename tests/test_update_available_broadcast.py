"""SVC-371 — the "Update available" release push reaches only the released platform.

``broadcast_update_available`` used to multicast to every consumer with a token.
Now it maps the release platform to a token platform (android→android,
ios→ios, pwa→web) and sends to matching tokens plus NULL ones (registered by a
client older than SVC-371, which keep their old behaviour until they
re-register). An unknown release platform sends nothing.

The task opens its own ``SyncSessionLocal``, so rows are committed and cleaned
up afterwards. ``sc_test`` may hold other suites' leftovers, so assertions only
look at this test's own tokens.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import delete

from app.models.user import User
from app.tasks.notification_tasks import broadcast_update_available
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory


@pytest.fixture
async def recipients(db_session):
    """One consumer per token platform, plus rows that must never be picked."""
    rows = {
        "android": ("consumer", "tok-android", "android"),
        "ios": ("consumer", "tok-ios", "ios"),
        "web": ("consumer", "tok-web", "web"),
        "null": ("consumer", "tok-null", None),
        # Providers are excluded whatever their platform (unchanged behaviour).
        "provider": ("provider", "tok-provider-android", "android"),
    }
    users = []
    tokens = {}
    for key, (role, token, platform) in rows.items():
        user = await UserFactory.create(db_session, role=role, fcm_token=f"{token}-{key}")
        user.fcm_platform = platform
        users.append(user)
        tokens[key] = user.fcm_token
    # A consumer with no token at all.
    users.append(await UserFactory.create(db_session, role="consumer", fcm_token=None))
    await db_session.commit()

    yield tokens

    await db_session.execute(delete(User).where(User.id.in_([u.id for u in users])))
    await db_session.commit()


def _run(platform: str) -> list[str] | None:
    """Run the task against sc_test; return the multicast's tokens, or None if not sent."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    )
    sent: list[list[str]] = []

    def _multicast(tokens, **_):
        sent.append(list(tokens))
        return {"success_count": len(tokens), "failure_count": 0}

    with (
        patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)),
        patch("app.tasks.notification_tasks.send_multicast", side_effect=_multicast),
    ):
        try:
            broadcast_update_available(version="9.9.9", platform=platform)
        finally:
            engine.dispose()
    assert len(sent) <= 1
    return sent[0] if sent else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("release", "expected"),
    [
        ("android", {"android", "null"}),
        ("pwa", {"web", "null"}),
        ("ios", {"ios", "null"}),
    ],
)
async def test_release_reaches_its_platform_and_null_tokens(recipients, release, expected):
    sent = _run(release)

    assert sent is not None
    ours = {key for key, token in recipients.items() if token in sent}
    assert ours == expected


@pytest.mark.asyncio
async def test_unknown_release_platform_sends_nothing(recipients):
    """Fail closed: a platform with no mapping must not fall back to everyone."""
    with patch("app.tasks.notification_tasks.send_multicast") as multicast:
        broadcast_update_available(version="9.9.9", platform="windows")

    multicast.assert_not_called()
