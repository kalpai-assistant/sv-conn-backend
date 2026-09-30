"""
Provider business name + business image (SVC-336).

Covers:
- PATCH /providers/me business_name: round trip, trim, blank / too long rejected,
  omitted leaves it unchanged
- POST /providers/me/business-image: happy path under provider-photos/<user_id>/business-*,
  validation (unsupported MIME, oversized, empty), provider-role gating
- GET /providers/me returns both fields as null for a provider who set neither
"""

from unittest.mock import patch

import pytest

from app.models.provider_profile import KYCStatus, ProviderProfile
from tests.factories.user_factory import UserFactory


def _png_bytes(size: int = 1024) -> bytes:
    """Minimal PNG header + padding. Content is bogus but the MIME check passes."""
    header = b"\x89PNG\r\n\x1a\n"
    return header + b"\x00" * max(0, size - len(header))


async def _seed_provider(db, kyc_status: KYCStatus = KYCStatus.PENDING):
    user = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(user_id=user.id, kyc_status=kyc_status)
    db.add(profile)
    await db.flush()
    return user, profile


# ---------------------------------------------------------------------------
# Business name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_business_name_round_trip(client, auth_headers, db_session):
    user, profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"business_name": "Sparkle Cleaners"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["business_name"] == "Sparkle Cleaners"
    await db_session.refresh(profile)
    assert profile.business_name == "Sparkle Cleaners"


@pytest.mark.asyncio
async def test_business_name_is_trimmed(client, auth_headers, db_session):
    user, _profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"business_name": "  Sparkle Cleaners  "},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["business_name"] == "Sparkle Cleaners"


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_business_name_is_rejected(client, auth_headers, db_session, blank):
    """Onboarding requires a real name, so blank is refused rather than stored."""
    user, profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"business_name": blank},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 422
    await db_session.refresh(profile)
    assert profile.business_name is None


@pytest.mark.asyncio
async def test_business_name_over_100_characters_is_rejected(client, auth_headers, db_session):
    user, profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"business_name": "x" * 101},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 422
    await db_session.refresh(profile)
    assert profile.business_name is None


@pytest.mark.asyncio
async def test_business_name_of_exactly_100_characters_is_accepted(
    client, auth_headers, db_session
):
    user, _profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"business_name": "x" * 100},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["business_name"] == "x" * 100


@pytest.mark.asyncio
async def test_omitting_business_name_leaves_it_unchanged(client, auth_headers, db_session):
    """PATCH is partial: a request about something else must not clear the name."""
    user, profile = await _seed_provider(db_session)
    profile.business_name = "Sparkle Cleaners"
    await db_session.flush()

    response = await client.patch(
        "/api/v1/providers/me",
        json={"languages": ["Hindi"]},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["business_name"] == "Sparkle Cleaners"


@pytest.mark.asyncio
async def test_new_provider_has_neither_business_field(client, auth_headers, db_session):
    """Both are null until set; the app shows a default logo for a null image."""
    user, _profile = await _seed_provider(db_session)

    response = await client.get(
        "/api/v1/providers/me",
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["business_name"] is None
    assert body["business_image_url"] is None


# ---------------------------------------------------------------------------
# Business image
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_business_image_happy_path(client, auth_headers, db_session):
    """Valid PNG → 200, stored under provider-photos/<user_id>/business-*, URL persisted."""
    user, profile = await _seed_provider(db_session)
    url = "https://bucket.s3.amazonaws.com/provider-photos/business-x.png"

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        mock_upload.return_value = url
        response = await client.post(
            "/api/v1/providers/me/business-image",
            files={"image": ("logo.png", _png_bytes(2048), "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 200, response.text
    assert response.json()["business_image_url"] == url
    assert mock_upload.call_count == 1
    key = mock_upload.call_args.args[1]
    assert key.startswith(f"provider-photos/{user.id}/business-")
    assert key.endswith(".png")

    await db_session.refresh(profile)
    assert profile.business_image_url == url
    # The person's photo is a different field and must be untouched.
    assert profile.profile_photo_url is None


@pytest.mark.asyncio
async def test_upload_business_image_replaces_existing(client, auth_headers, db_session):
    user, profile = await _seed_provider(db_session)
    profile.business_image_url = "https://bucket.s3.amazonaws.com/old.png"
    await db_session.flush()

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        mock_upload.return_value = "https://bucket.s3.amazonaws.com/new.png"
        response = await client.post(
            "/api/v1/providers/me/business-image",
            files={"image": ("new.png", _png_bytes(1024), "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 200, response.text
    assert response.json()["business_image_url"] == "https://bucket.s3.amazonaws.com/new.png"


@pytest.mark.asyncio
async def test_upload_business_image_unsupported_mime(client, auth_headers, db_session):
    """application/pdf → 415, nothing uploaded."""
    user, _profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/business-image",
            files={"image": ("logo.pdf", b"%PDF-1.4\n%...", "application/pdf")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 415
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_business_image_oversized(client, auth_headers, db_session):
    """File over 5 MB → 413, nothing uploaded."""
    user, _profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/business-image",
            files={"image": ("big.png", _png_bytes(5 * 1024 * 1024 + 1), "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 413
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_business_image_empty_file(client, auth_headers, db_session):
    """Empty file → 400, nothing uploaded."""
    user, _profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/business-image",
            files={"image": ("empty.png", b"", "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 400
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_business_image_requires_provider_role(client, auth_headers, db_session):
    """A consumer caller is rejected by require_role('provider')."""
    consumer = await UserFactory.create(db_session, role="consumer")

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/business-image",
            files={"image": ("logo.png", _png_bytes(1024), "image/png")},
            headers=auth_headers(str(consumer.id), "consumer"),
        )

    assert response.status_code == 403
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_business_image_upload_failure_is_a_502(client, auth_headers, db_session):
    """An S3 error surfaces as 502 and leaves the stored URL alone."""
    user, profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        mock_upload.side_effect = RuntimeError("S3 down")
        response = await client.post(
            "/api/v1/providers/me/business-image",
            files={"image": ("logo.png", _png_bytes(1024), "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 502
    await db_session.refresh(profile)
    assert profile.business_image_url is None
