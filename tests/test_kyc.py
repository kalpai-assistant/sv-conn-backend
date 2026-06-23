"""
KYC submission + admin verify/reject tests (SC-31).

Covers:
- POST /providers/me/kyc happy path (file upload → kyc_status=SUBMITTED)
- POST /providers/me/kyc validation (oversized + unsupported MIME)
- POST /admin/providers/{id}/kyc/verify (admin only)
- POST /admin/providers/{id}/kyc/reject with reason (admin only)
- Non-admin caller → 403
- Verify on non-SUBMITTED profile → 403
"""
import io
from uuid import uuid4
from unittest.mock import patch

import pytest

from app.models.provider_profile import ProviderProfile, KYCStatus
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


@pytest.mark.asyncio
async def test_submit_kyc_happy_path(client, auth_headers, db_session):
    """Two valid PNG uploads → 201, status SUBMITTED, S3 upload called twice."""
    user, profile = await _seed_provider(db_session, kyc_status=KYCStatus.PENDING)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        mock_upload.return_value = "https://bucket.s3.amazonaws.com/dummy"
        response = await client.post(
            "/api/v1/providers/me/kyc",
            files={
                "aadhaar": ("aadhaar.png", _png_bytes(2048), "image/png"),
                "selfie": ("selfie.png", _png_bytes(1024), "image/png"),
            },
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 201, response.text
    assert response.json()["kyc_status"] == "submitted"
    assert response.json()["kyc_submitted_at"] is not None
    assert mock_upload.call_count == 2

    # Profile state was persisted with the doc keys.
    await db_session.refresh(profile)
    assert profile.kyc_status == KYCStatus.SUBMITTED
    assert profile.kyc_docs is not None
    assert "aadhaar_key" in profile.kyc_docs
    assert "selfie_key" in profile.kyc_docs


@pytest.mark.asyncio
async def test_submit_kyc_oversized_file(client, auth_headers, db_session):
    """File over 5 MB → 413, nothing uploaded."""
    user, _profile = await _seed_provider(db_session)

    too_big = _png_bytes(5 * 1024 * 1024 + 1)
    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/kyc",
            files={
                "aadhaar": ("aadhaar.png", too_big, "image/png"),
                "selfie": ("selfie.png", _png_bytes(1024), "image/png"),
            },
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 413
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_submit_kyc_unsupported_mime(client, auth_headers, db_session):
    """application/pdf upload → 415, nothing uploaded."""
    user, _profile = await _seed_provider(db_session)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/kyc",
            files={
                "aadhaar": ("aadhaar.pdf", b"%PDF-1.4\n%...", "application/pdf"),
                "selfie": ("selfie.png", _png_bytes(1024), "image/png"),
            },
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 415
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_submit_kyc_double_submit_rejected(client, auth_headers, db_session):
    """Re-submission while SUBMITTED → 403."""
    user, _profile = await _seed_provider(db_session, kyc_status=KYCStatus.SUBMITTED)

    with patch("app.services.provider_service.storage.upload_file") as mock_upload:
        response = await client.post(
            "/api/v1/providers/me/kyc",
            files={
                "aadhaar": ("aadhaar.png", _png_bytes(1024), "image/png"),
                "selfie": ("selfie.png", _png_bytes(1024), "image/png"),
            },
            headers=auth_headers(str(user.id), "provider"),
        )

    assert response.status_code == 403
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_admin_verify_kyc(client, auth_headers, db_session):
    """Admin verifies SUBMITTED KYC → 200, status flips to VERIFIED, reviewed_at set."""
    provider_user, profile = await _seed_provider(
        db_session, kyc_status=KYCStatus.SUBMITTED
    )
    admin_user = await UserFactory.create(db_session, role="admin")

    response = await client.post(
        f"/api/v1/admin/providers/{provider_user.id}/kyc/verify",
        headers=auth_headers(str(admin_user.id), "admin"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["kyc_status"] == "verified"
    assert response.json()["kyc_reviewed_at"] is not None
    assert response.json()["kyc_rejection_reason"] is None

    await db_session.refresh(profile)
    assert profile.kyc_status == KYCStatus.VERIFIED


@pytest.mark.asyncio
async def test_admin_verify_kyc_wrong_state(client, auth_headers, db_session):
    """Verifying a PENDING (not-yet-submitted) profile → 403."""
    provider_user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.PENDING)
    admin_user = await UserFactory.create(db_session, role="admin")

    response = await client.post(
        f"/api/v1/admin/providers/{provider_user.id}/kyc/verify",
        headers=auth_headers(str(admin_user.id), "admin"),
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_reject_kyc_with_reason(client, auth_headers, db_session):
    """Admin rejects SUBMITTED KYC with reason → 200, reason persisted."""
    provider_user, profile = await _seed_provider(
        db_session, kyc_status=KYCStatus.SUBMITTED
    )
    admin_user = await UserFactory.create(db_session, role="admin")

    response = await client.post(
        f"/api/v1/admin/providers/{provider_user.id}/kyc/reject",
        json={"reason": "Aadhaar photo is blurred — please re-upload"},
        headers=auth_headers(str(admin_user.id), "admin"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kyc_status"] == "rejected"
    assert body["kyc_rejection_reason"] == "Aadhaar photo is blurred — please re-upload"

    await db_session.refresh(profile)
    assert profile.kyc_status == KYCStatus.REJECTED
    assert profile.kyc_rejection_reason.startswith("Aadhaar photo")


@pytest.mark.asyncio
async def test_admin_reject_kyc_empty_reason(client, auth_headers, db_session):
    """Empty reason → 422 from Pydantic validation."""
    provider_user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.SUBMITTED)
    admin_user = await UserFactory.create(db_session, role="admin")

    response = await client.post(
        f"/api/v1/admin/providers/{provider_user.id}/kyc/reject",
        json={"reason": ""},
        headers=auth_headers(str(admin_user.id), "admin"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_non_admin_cannot_verify(client, auth_headers, db_session):
    """Provider tries to self-verify → 403."""
    provider_user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.SUBMITTED)
    consumer_user = await UserFactory.create(db_session, role="consumer")

    # As the provider themselves
    r1 = await client.post(
        f"/api/v1/admin/providers/{provider_user.id}/kyc/verify",
        headers=auth_headers(str(provider_user.id), "provider"),
    )
    # As a random consumer
    r2 = await client.post(
        f"/api/v1/admin/providers/{provider_user.id}/kyc/verify",
        headers=auth_headers(str(consumer_user.id), "consumer"),
    )

    assert r1.status_code == 403
    assert r2.status_code == 403


@pytest.mark.asyncio
async def test_admin_list_pending_kyc(client, auth_headers, db_session):
    """List endpoint returns only SUBMITTED profiles."""
    submitted_user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.SUBMITTED)
    verified_user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.VERIFIED)
    pending_user, _ = await _seed_provider(db_session, kyc_status=KYCStatus.PENDING)
    admin_user = await UserFactory.create(db_session, role="admin")

    response = await client.get(
        "/api/v1/admin/providers/kyc/pending",
        headers=auth_headers(str(admin_user.id), "admin"),
    )

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert str(submitted_user.id) in ids
    assert str(verified_user.id) not in ids
    assert str(pending_user.id) not in ids
