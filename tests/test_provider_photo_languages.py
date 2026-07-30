"""
Provider profile photo upload + languages tests (SVC-145).

Covers:
- POST /providers/me/photo happy path (file upload → profile_photo_url set)
- POST /providers/me/photo validation (unsupported MIME, oversized, empty)
- PATCH /providers/me languages round-trip (trim + case-insensitive de-dup)
- Provider-role gating on both endpoints
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
# Profile photo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_photo_happy_path(client, auth_headers, db_session):
    """Valid PNG → 200, profile_photo_url persisted, S3 upload called once."""
    user, profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        mock_upload.return_value = "https://bucket.s3.amazonaws.com/provider-photos/x.png"
        response = await client.post(
            "/api/v1/providers/me/photo",
            files={"photo": ("me.png", _png_bytes(2048), "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 200, response.text
    assert response.json()["profile_photo_url"] == (
        "https://bucket.s3.amazonaws.com/provider-photos/x.png"
    )
    assert mock_upload.call_count == 1

    await db_session.refresh(profile)
    assert profile.profile_photo_url == (
        "https://bucket.s3.amazonaws.com/provider-photos/x.png"
    )


@pytest.mark.asyncio
async def test_upload_photo_replaces_existing(client, auth_headers, db_session):
    """Re-uploading overwrites the stored URL."""
    user, profile = await _seed_provider(db_session)
    profile.profile_photo_url = "https://bucket.s3.amazonaws.com/old.png"
    await db_session.flush()

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        mock_upload.return_value = "https://bucket.s3.amazonaws.com/new.png"
        response = await client.post(
            "/api/v1/providers/me/photo",
            files={"photo": ("new.png", _png_bytes(1024), "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 200, response.text
    assert response.json()["profile_photo_url"] == "https://bucket.s3.amazonaws.com/new.png"


@pytest.mark.asyncio
async def test_upload_photo_unsupported_mime(client, auth_headers, db_session):
    """application/pdf → 415, nothing uploaded."""
    user, _profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/photo",
            files={"photo": ("me.pdf", b"%PDF-1.4\n%...", "application/pdf")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 415
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_photo_oversized(client, auth_headers, db_session):
    """File over 5 MB → 413, nothing uploaded."""
    user, _profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/photo",
            files={"photo": ("big.png", _png_bytes(5 * 1024 * 1024 + 1), "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 413
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_photo_empty_file(client, auth_headers, db_session):
    """Empty file → 400, nothing uploaded."""
    user, _profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/photo",
            files={"photo": ("empty.png", b"", "image/png")},
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 400
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_photo_requires_provider_role(client, auth_headers, db_session):
    """A consumer caller is rejected by require_role('provider')."""
    consumer = await UserFactory.create(db_session, role="consumer")

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/photo",
            files={"photo": ("me.png", _png_bytes(1024), "image/png")},
            headers=auth_headers(str(consumer.id), "consumer"),
        )

    assert response.status_code == 403
    mock_upload.assert_not_called()


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_languages_round_trip(client, auth_headers, db_session):
    """PATCH /me with languages → persisted and echoed back."""
    user, profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"languages": ["Hindi", "English", "Marathi"]},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["languages"] == ["Hindi", "English", "Marathi"]

    await db_session.refresh(profile)
    assert profile.languages == ["Hindi", "English", "Marathi"]


@pytest.mark.asyncio
async def test_update_languages_dedup_and_trim(client, auth_headers, db_session):
    """Whitespace is trimmed and case-insensitive duplicates collapse (first wins)."""
    user, _profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"languages": ["Hindi", "  hindi  ", "English", "ENGLISH", ""]},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["languages"] == ["Hindi", "English"]


@pytest.mark.asyncio
async def test_update_languages_empty_list_clears(client, auth_headers, db_session):
    """An explicit empty list clears the field."""
    user, profile = await _seed_provider(db_session)
    profile.languages = ["Hindi"]
    await db_session.flush()

    response = await client.patch(
        "/api/v1/providers/me",
        json={"languages": []},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["languages"] == []

    await db_session.refresh(profile)
    assert profile.languages == []


@pytest.mark.asyncio
async def test_update_languages_omitted_leaves_unchanged(client, auth_headers, db_session):
    """Omitting languages (None) does not wipe an existing value."""
    user, profile = await _seed_provider(db_session)
    profile.languages = ["Hindi"]
    await db_session.flush()

    response = await client.patch(
        "/api/v1/providers/me",
        json={"service_area_radius_km": 15},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["languages"] == ["Hindi"]
