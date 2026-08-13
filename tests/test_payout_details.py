"""
Provider payout-details setter tests (SVC-147).

Covers PATCH /providers/me payout fields:
- set VPA -> stored encrypted, payout_set true
- set account + IFSC -> stored encrypted, payout_set true
- invalid UPI / IFSC -> 422
- account without IFSC (and vice versa) -> 422
- stored ciphertext != plaintext

On what the API returns (narrowed by SVC-148): a bank account number is never
returned in full — only its last 4 — while a VPA is returned as-is, because
``ProviderResponse`` is only ever served for ``/providers/me`` and every Indian
payment app shows a VPA in full. See ``tests/test_payout_method.py`` for the test
that no *other* endpoint exposes payout fields, which is the guarantee that matters.
"""

import pytest
from sqlalchemy import select

from app.models.provider_profile import KYCStatus, ProviderProfile
from app.utils.encryption import decrypt
from tests.factories.user_factory import UserFactory


async def _seed_provider(db, kyc_status: KYCStatus = KYCStatus.VERIFIED):
    user = await UserFactory.create(db, role="provider")
    profile = ProviderProfile(user_id=user.id, kyc_status=kyc_status)
    db.add(profile)
    await db.flush()
    return user, profile


async def _get_profile(db, user_id):
    result = await db.execute(select(ProviderProfile).where(ProviderProfile.user_id == user_id))
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
    # The request field itself is never echoed back.
    assert "payout_vpa" not in body

    # The VPA IS shown back, in full, via payout_display (SVC-148).
    #
    # This narrows the original assertion ("okhdfcbank" not in response.text), which
    # dated from when the response had no reason to carry payout details at all. The
    # rule it protected is that a provider's payout details never reach anyone else —
    # and ProviderResponse is only ever returned for /providers/me, so this is the
    # provider seeing a handle they typed in themselves. Masking it would also be
    # wrong for the market: GPay, PhonePe and Paytm all show a VPA in full.
    # Bank account numbers stay masked — see test_set_payout_account_and_ifsc.
    assert body["payout_method"] == "upi"
    assert body["payout_display"] == "provider@okhdfcbank"

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
    body = response.json()
    assert body["payout_set"] is True

    # Bank accounts ARE masked to the last 4 — the convention wherever someone is
    # shown their own account (net banking, GPay, the Razorpay/Stripe dashboards).
    # The full number must never appear in a response.
    assert body["payout_method"] == "bank"
    assert body["payout_display"] == "••••••7890"
    assert "1234567890" not in response.text

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


# ---------------------------------------------------------------------------
# SVC-189 (G16) — KYC gate on payout writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kyc",
    [KYCStatus.PENDING, KYCStatus.SUBMITTED, KYCStatus.REJECTED],
)
async def test_payout_vpa_refused_when_kyc_not_verified(client, auth_headers, db_session, kyc):
    """PATCH /providers/me refuses payout_vpa unless KYC is VERIFIED (SVC-189).

    Before this a provider could register, skip KYC entirely, and store a
    UPI destination — landing money in PENDING settlements an admin might
    then pay out without knowing KYC never happened.
    """
    user, profile = await _seed_provider(db_session, kyc_status=kyc)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_vpa": "provider@okhdfcbank"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 400, response.text
    # Message should say what to fix — the current status is more actionable
    # than a generic "not allowed".
    assert kyc.value in response.text.lower() or kyc.value in response.text
    await db_session.refresh(profile)
    assert profile.payout_vpa_encrypted is None
    assert profile.payout_method is None


@pytest.mark.asyncio
async def test_payout_bank_refused_when_kyc_not_verified(client, auth_headers, db_session):
    """Same gate applies to bank-account writes — one rule, both destinations."""
    user, profile = await _seed_provider(db_session, kyc_status=KYCStatus.SUBMITTED)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"payout_account_number": "1234567890", "payout_ifsc": "HDFC0001234"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 400, response.text
    await db_session.refresh(profile)
    assert profile.payout_account_number_encrypted is None
    assert profile.payout_ifsc_encrypted is None
    assert profile.payout_method is None


@pytest.mark.asyncio
async def test_non_payout_fields_still_writable_before_kyc(client, auth_headers, db_session):
    """The gate is scoped to *payout* fields; KYC-in-progress providers must
    still be able to update KYC docs, service area, languages, etc.

    Regression guard: an overly wide gate would strand providers half-way
    through onboarding, which is exactly the flow this ticket needs to
    stay open.
    """
    user, profile = await _seed_provider(db_session, kyc_status=KYCStatus.PENDING)

    response = await client.patch(
        "/api/v1/providers/me",
        json={"service_area_radius_km": 12, "languages": ["en", "hi"]},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(profile)
    assert profile.service_area_radius_km == 12
    assert profile.languages == ["en", "hi"]


@pytest.mark.asyncio
async def test_switch_active_payout_method_refused_when_kyc_not_verified(
    client, auth_headers, db_session
):
    """PATCH /providers/me/payout-method refuses when KYC isn't verified.

    Edge case: a profile that predates the SVC-189 write-side gate could hold
    stored details from before. Activating them defeats the point — the payout
    call itself will refuse (see the PayoutService test), but blocking here
    saves the admin from seeing a payable-looking provider that will 402 at
    payout time.
    """
    # Seed with both destinations stored (bypassing the API gate via direct
    # profile mutation — simulates pre-gate state), then drop KYC back.
    user, profile = await _seed_provider(db_session, kyc_status=KYCStatus.VERIFIED)
    from app.utils.encryption import encrypt as _enc

    profile.payout_vpa_encrypted = _enc("provider@upi")
    profile.payout_account_number_encrypted = _enc("1234567890")
    profile.payout_ifsc_encrypted = _enc("HDFC0001234")
    profile.payout_method = None
    profile.kyc_status = KYCStatus.REJECTED
    await db_session.flush()

    response = await client.patch(
        "/api/v1/providers/me/payout-method",
        json={"method": "upi"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 400, response.text
    await db_session.refresh(profile)
    assert profile.payout_method is None


@pytest.mark.asyncio
async def test_remove_payout_method_allowed_regardless_of_kyc(client, auth_headers, db_session):
    """DELETE /providers/me/payout-method is NOT gated: a provider can always
    withdraw their PII, KYC or no.

    The rule is symmetric — we won't accept new payout data until KYC is
    verified, but we won't hold old data hostage either.
    """
    user, profile = await _seed_provider(db_session, kyc_status=KYCStatus.VERIFIED)
    from app.utils.encryption import encrypt as _enc

    profile.payout_vpa_encrypted = _enc("provider@upi")
    profile.payout_method = None  # inactive but stored
    profile.kyc_status = KYCStatus.REJECTED
    await db_session.flush()

    response = await client.delete(
        "/api/v1/providers/me/payout-method",
        headers=auth_headers(str(user.id), "provider"),
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(profile)
    assert profile.payout_vpa_encrypted is None
