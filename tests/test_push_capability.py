"""
POST /users/me/push-capability — measure who can actually receive push (SVC-255).

The endpoint stores nothing, so the *log line is the product*. These tests assert
its contents field by field: if the line silently loses `platform` or
`ios_standalone`, the CloudWatch query behind the ticket returns nothing useful
and no other test in the suite would notice.

Covers:
- 204 + a structured line for a native Android success
- the same 204 for a *failed* attempt, which is the point of the endpoint —
  Sentry already has the failures, what it lacks is the denominator
- the iOS shapes that decide the ticket: Safari not installed to the Home
  Screen, and Chrome on iOS (which cannot install at all)
- iOS-only fields defaulting to False so an older client build still reports
- an unknown platform rejected rather than logged as garbage, which would
  poison the counts the ticket is scoped from
- an explicit Enable tap flagged apart from the passive app-start attempt
- JWT gating
"""

import logging

import pytest

from tests.factories.user_factory import UserFactory

ENDPOINT = "/api/v1/users/me/push-capability"
LOGGER = "app.routers.users"

ANDROID_NATIVE = {
    "platform": "android",
    "is_native": True,
    "ios_standalone": False,
    "is_chrome_ios": False,
    "push_permission": "granted",
    "token_obtained": True,
    "interactive": False,
}


def _capability_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("push.capability")]


@pytest.mark.asyncio
async def test_reports_native_android(client, auth_headers, db_session, caplog):
    """The population we know works: one line, every field on it."""
    provider = await UserFactory.create(db_session, role="provider")
    caplog.set_level(logging.INFO, logger=LOGGER)

    response = await client.post(
        ENDPOINT, json=ANDROID_NATIVE, headers=auth_headers(str(provider.id))
    )

    assert response.status_code == 204, response.text
    lines = _capability_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert f"user={provider.id}" in line
    assert "role=provider" in line
    assert "platform=android" in line
    assert "native=True" in line
    assert "permission=granted" in line
    assert "token=True" in line
    assert "interactive=False" in line


@pytest.mark.asyncio
async def test_failed_attempt_is_still_recorded(client, auth_headers, db_session, caplog):
    """A denied permission reports too — otherwise there is no denominator.

    If this ever regresses to only logging successes, the measurement reproduces
    the exact blind spot it was built to close: failures counted with no idea how
    many attempts they came out of.
    """
    provider = await UserFactory.create(db_session, role="provider")
    caplog.set_level(logging.INFO, logger=LOGGER)

    response = await client.post(
        ENDPOINT,
        json={**ANDROID_NATIVE, "push_permission": "denied", "token_obtained": False},
        headers=auth_headers(str(provider.id)),
    )

    assert response.status_code == 204, response.text
    line = _capability_lines(caplog)[0]
    assert "permission=denied" in line
    assert "token=False" in line


@pytest.mark.asyncio
async def test_ios_safari_not_installed(client, auth_headers, db_session, caplog):
    """The shape the ticket turns on: iOS Safari with no Home Screen install."""
    provider = await UserFactory.create(db_session, role="provider")
    caplog.set_level(logging.INFO, logger=LOGGER)

    response = await client.post(
        ENDPOINT,
        json={
            "platform": "ios",
            "is_native": False,
            "ios_standalone": False,
            "is_chrome_ios": False,
            "push_permission": "unsupported",
            "token_obtained": False,
        },
        headers=auth_headers(str(provider.id)),
    )

    assert response.status_code == 204, response.text
    line = _capability_lines(caplog)[0]
    assert "platform=ios" in line
    assert "standalone=False" in line
    assert "permission=unsupported" in line


@pytest.mark.asyncio
async def test_chrome_on_ios_counted_separately(client, auth_headers, db_session, caplog):
    """Chrome on iOS cannot install to the Home Screen, so its remedy differs.

    Lumped in with Safari it would look like a user who simply hasn't installed
    yet, and inflate the "one tap from working" bucket.
    """
    provider = await UserFactory.create(db_session, role="provider")
    caplog.set_level(logging.INFO, logger=LOGGER)

    response = await client.post(
        ENDPOINT,
        json={
            "platform": "ios",
            "is_native": False,
            "ios_standalone": False,
            "is_chrome_ios": True,
            "push_permission": "unsupported",
            "token_obtained": False,
        },
        headers=auth_headers(str(provider.id)),
    )

    assert response.status_code == 204, response.text
    assert "chrome_ios=True" in _capability_lines(caplog)[0]


@pytest.mark.asyncio
async def test_ios_fields_default_to_false(client, auth_headers, db_session, caplog):
    """An app build that omits the iOS-only fields still reports rather than 422s."""
    provider = await UserFactory.create(db_session, role="provider")
    caplog.set_level(logging.INFO, logger=LOGGER)

    response = await client.post(
        ENDPOINT,
        json={
            "platform": "android",
            "is_native": True,
            "push_permission": "granted",
            "token_obtained": True,
        },
        headers=auth_headers(str(provider.id)),
    )

    assert response.status_code == 204, response.text
    line = _capability_lines(caplog)[0]
    assert "standalone=False" in line
    assert "chrome_ios=False" in line
    assert "interactive=False" in line


@pytest.mark.asyncio
async def test_unknown_platform_rejected(client, auth_headers, db_session, caplog):
    """Free-text platforms would poison the counts, so they 422 instead of logging."""
    provider = await UserFactory.create(db_session, role="provider")
    caplog.set_level(logging.INFO, logger=LOGGER)

    response = await client.post(
        ENDPOINT,
        json={**ANDROID_NATIVE, "platform": "windows-phone"},
        headers=auth_headers(str(provider.id)),
    )

    assert response.status_code == 422
    assert _capability_lines(caplog) == []


@pytest.mark.asyncio
async def test_requires_auth(client, caplog):
    """The line is keyed by user id, so an anonymous report has nothing to say."""
    caplog.set_level(logging.INFO, logger=LOGGER)

    response = await client.post(ENDPOINT, json=ANDROID_NATIVE)

    assert response.status_code in (401, 403)
    assert _capability_lines(caplog) == []


@pytest.mark.asyncio
async def test_interactive_attempt_flagged(client, auth_headers, db_session, caplog):
    """A tapped Enable is distinguishable from the passive app-start attempt.

    Without it, "denied" covers both a provider who never opened the prompt and
    one who opened it and said no — the first is a UX fix, the second isn't.
    """
    provider = await UserFactory.create(db_session, role="provider")
    caplog.set_level(logging.INFO, logger=LOGGER)

    response = await client.post(
        ENDPOINT,
        json={**ANDROID_NATIVE, "interactive": True, "push_permission": "denied"},
        headers=auth_headers(str(provider.id)),
    )

    assert response.status_code == 204, response.text
    assert "interactive=True" in _capability_lines(caplog)[0]
