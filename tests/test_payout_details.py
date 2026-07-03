"""
Provider payout-details setter tests (SVC-147).

Covers PATCH /providers/me payout fields:
- set VPA -> stored encrypted, payout_set true
- set account + IFSC -> stored encrypted, payout_set true
- invalid UPI / IFSC -> 422
- account without IFSC (and vice versa) -> 422
- raw payout values are never returned; stored ciphertext != plaintext
"""
import pytest
from sqlalchemy import select

from app.models.provider_profile import ProviderProfile, KYCStatus
from app.utils.encryption import decrypt
from tests.factories.user_factory import UserFactory


async def _seed_provider(db, kyc_status: KYCStatus = KYCStatus.VERIFIED):
    user = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(user_id=user.id, kyc_status=kyc_status)
    db.add(profile)
    await db.flush()
    return user, profile


async def _get_profile(db, user_id):
    result = await db.execute(
        select(ProviderProfile).where(ProviderProfile.user_id == user_id)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_set_payout_vpa(client, auth_headers, db_session):
    """Valid VPA -> 200, payout_set true, stored encrypted (round-trips)."""
    user, profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_vpa": "provider@okhdfcbank"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["payout_set"] is True
    # Raw PII must never be returned.
    assert "payout_vpa" not in body
    assert "okhdfcbank" not in response.text

    await db_session.refresh(profile)
    assert profile.payout_vpa_encrypted is not None
    assert profile.payout_vpa_encrypted != "provider@okhdfcbank"  # encrypted at rest
    assert decrypt(profile.payout_vpa_encrypted) == "provider@okhdfcbank"


@pytest.mark.asyncio
async def test_set_payout_account_and_ifsc(client, auth_headers, db_session):
    """Valid account + IFSC -> 200, payout_set true, both stored encrypted."""
    user, profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_account_number": "1234567890", "payout_ifsc": "hdfc0001234"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["payout_set"] is True

    await db_session.refresh(profile)
    assert decrypt(profile.payout_account_number_encrypted) == "1234567890"
    # IFSC is upper-cased by the validator before storage.
    assert decrypt(profile.payout_ifsc_encrypted) == "HDFC0001234"


@pytest.mark.asyncio
async def test_invalid_vpa_rejected(client, auth_headers, db_session):
    """Malformed VPA -> 422, nothing stored."""
    user, profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_vpa": "not-a-vpa"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 422
    await db_session.refresh(profile)
    assert profile.payout_vpa_encrypted is None


@pytest.mark.asyncio
async def test_invalid_ifsc_rejected(client, auth_headers, db_session):
    """Malformed IFSC -> 422."""
    user, _profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_account_number": "1234567890", "payout_ifsc": "BADIFSC123"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_account_without_ifsc_rejected(client, auth_headers, db_session):
    """Account number without IFSC -> 422."""
    user, _profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_account_number": "1234567890"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_ifsc_without_account_rejected(client, auth_headers, db_session):
    """IFSC without account number -> 422."""
    user, _profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_ifsc": "HDFC0001234"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_payout_set_false_when_unset(client, auth_headers, db_session):
    """A profile with no payout details reports payout_set false."""
    user, _profile = await _seed_provider(db_session)

    response = await client.get(
        "/api/v1/providers/me",
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["payout_set"] is False


@pytest.mark.asyncio
async def test_non_digit_account_rejected(client, auth_headers, db_session):
    """Account number with non-digit characters -> 422, nothing stored."""
    user, profile = await _seed_provider(db_session)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_account_number": "12AB567890", "payout_ifsc": "HDFC0001234"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 422
    await db_session.refresh(profile)
    assert profile.payout_account_number_encrypted is None
