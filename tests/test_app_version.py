"""SVC-162 — GET /downloads/latest.json tests.

Public endpoint. Two things to pin:

1. **Happy path** — returns both platform rows in the declared shape,
   with the Cache-Control header the CDN needs.
   SVC-366 added ``ios`` as its own row, no longer read off ``pwa``.
2. **Missing-seed failure** — if the app_versions table is empty, we
   return 503 (deployment bug), not silently invent defaults. The whole
   point of storing this in the DB is to be authoritative — a fallback
   would mask the bug.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.database import get_db
from app.exceptions import register_exception_handlers
from app.models.app_version import AppPlatform, AppVersion
from app.routers.downloads import router


@pytest.fixture
def app(db_session):
    a = FastAPI()
    a.include_router(router, prefix="/api/v1")
    a.dependency_overrides[get_db] = lambda: db_session
    register_exception_handlers(a)
    return a


async def _get(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get("/api/v1/downloads/latest.json")


@pytest.mark.asyncio
async def test_latest_returns_every_platform_when_seeded(app, db_session):
    """The Alembic seeds insert android + pwa + ios at migration time.
    Endpoint returns all three. Uses the seed data as-is — no test setup needed."""
    # If the seed rows already exist (via the migration), they're here;
    # otherwise insert them so the test also passes on Base.metadata.create_all
    # (which doesn't run migrations, so no seed data).
    existing = {v.platform: v for v in (await _all_versions(db_session))}
    if AppPlatform.ANDROID not in existing:
        db_session.add(
            AppVersion(
                platform=AppPlatform.ANDROID,
                version="1.0.0-beta",
                min_supported_version="1.0.0-beta",
                url=None,
                release_notes="First beta",
            )
        )
    if AppPlatform.PWA not in existing:
        db_session.add(
            AppVersion(
                platform=AppPlatform.PWA,
                version="1.0.0-beta",
                min_supported_version="1.0.0-beta",
                url=None,
                release_notes="First beta",
            )
        )
    if AppPlatform.IOS not in existing:
        db_session.add(
            AppVersion(
                platform=AppPlatform.IOS,
                version="1.0.0-beta",
                min_supported_version="1.0.0-beta",
                url=None,
                release_notes=None,
            )
        )
    await db_session.flush()

    r = await _get(app)

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"android", "pwa", "ios"}
    for platform in ("android", "pwa", "ios"):
        assert body[platform]["version"] == "1.0.0-beta"
        assert body[platform]["min_supported_version"] == "1.0.0-beta"
    # CDN cache: 5 min. Matters — a burst of foreground events after a
    # network blip shouldn't hammer the DB.
    assert r.headers["cache-control"] == "public, max-age=300"


@pytest.mark.asyncio
async def test_latest_returns_503_when_seed_rows_missing(app, db_session):
    """Empty table → 503. Silent defaults would mask a deployment bug."""
    await db_session.execute(delete(AppVersion))
    await db_session.flush()

    r = await _get(app)

    assert r.status_code == 503, r.text
    assert "missing" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_latest_returns_503_when_only_one_platform_seeded(app, db_session):
    """Partial seed is also a deployment bug — same treatment as no seed."""
    await db_session.execute(delete(AppVersion))
    db_session.add(
        AppVersion(
            platform=AppPlatform.ANDROID,
            version="1.0.0-beta",
            min_supported_version="1.0.0-beta",
            url=None,
            release_notes=None,
        )
    )
    await db_session.flush()

    r = await _get(app)

    assert r.status_code == 503, r.text


@pytest.mark.asyncio
async def test_latest_reflects_updated_values(app, db_session):
    """When admin flips `min_supported_version` (via SQL for now),
    the next fetch reflects it. This is the 'emergency lever' path."""
    await db_session.execute(delete(AppVersion))
    db_session.add_all(
        [
            AppVersion(
                platform=AppPlatform.ANDROID,
                version="1.0.1",
                min_supported_version="1.0.1",  # bumped, forces upgrade
                url="https://s3/apk/latest.apk",
                release_notes="Critical fix: booking crash",
            ),
            AppVersion(
                platform=AppPlatform.PWA,
                version="1.0.1",
                min_supported_version="1.0.0",  # PWA allows the older one still
                url=None,
                release_notes="Critical fix: booking crash",
            ),
            AppVersion(
                platform=AppPlatform.IOS,
                version="1.0.0",
                min_supported_version="1.0.0",
                url=None,
                release_notes=None,
            ),
        ]
    )
    await db_session.flush()

    r = await _get(app)

    assert r.status_code == 200
    body = r.json()
    assert body["android"]["version"] == "1.0.1"
    assert body["android"]["min_supported_version"] == "1.0.1"
    assert body["android"]["url"] == "https://s3/apk/latest.apk"
    assert body["pwa"]["min_supported_version"] == "1.0.0"
    # SVC-366: iOS has its own row and doesn't follow the PWA bump.
    assert body["ios"]["version"] == "1.0.0"
    assert body["ios"]["url"] is None


@pytest.mark.asyncio
async def test_latest_returns_503_when_ios_row_missing(app, db_session):
    """SVC-366: ios is a required key now. A deploy without its seed row is
    the same deployment bug as a missing android or pwa row."""
    await db_session.execute(delete(AppVersion))
    db_session.add_all(
        [
            AppVersion(
                platform=p,
                version="1.0.0-beta",
                min_supported_version="1.0.0-beta",
                url=None,
                release_notes=None,
            )
            for p in (AppPlatform.ANDROID, AppPlatform.PWA)
        ]
    )
    await db_session.flush()

    r = await _get(app)

    assert r.status_code == 503, r.text


@pytest.mark.asyncio
async def test_admin_release_bumps_the_ios_row(db_session, monkeypatch):
    """SVC-366: ``POST /admin/app-versions/ios/release`` parses ``ios`` and
    bumps the ios row only. notify=false so no Celery broadcast fires."""
    from app.config import get_settings
    from app.routers.admin import router as admin_router

    monkeypatch.setattr(get_settings(), "INTERNAL_RELEASE_TOKEN", "test-release-token")
    await db_session.execute(delete(AppVersion))
    db_session.add_all(
        [
            AppVersion(
                platform=p,
                version="1.0.0-beta",
                min_supported_version="1.0.0-beta",
                url=None,
                release_notes=None,
            )
            for p in (AppPlatform.ANDROID, AppPlatform.PWA, AppPlatform.IOS)
        ]
    )
    await db_session.flush()

    a = FastAPI()
    a.include_router(admin_router, prefix="/api/v1")
    a.dependency_overrides[get_db] = lambda: db_session
    register_exception_handlers(a)
    async with AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as ac:
        r = await ac.post(
            "/api/v1/admin/app-versions/ios/release",
            json={"version": "1.0.1", "min_supported_version": "1.0.1", "notify": False},
            headers={"X-Internal-Release-Token": "test-release-token"},
        )

    assert r.status_code == 200, r.text
    assert r.json()["platform"] == "ios"
    rows = {v.platform: v for v in await _all_versions(db_session)}
    assert rows[AppPlatform.IOS].version == "1.0.1"
    assert rows[AppPlatform.IOS].min_supported_version == "1.0.1"
    assert rows[AppPlatform.PWA].version == "1.0.0-beta"
    assert rows[AppPlatform.ANDROID].version == "1.0.0-beta"


async def _all_versions(db_session):
    from sqlalchemy import select

    result = await db_session.execute(select(AppVersion))
    return list(result.scalars().all())
