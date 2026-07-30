"""SVC-148 — manage a payout method after onboarding: switch, set active, remove.

The load-bearing test here is ``test_switching_upi_to_bank_pays_the_bank`` — before
this ticket, switching UPI -> bank kept paying the old UPI handle, because
``create_payout`` takes whichever destination it is handed and prefers UPI. Saving a
method no longer erases the other one, so ``payout_method`` is the only thing that
decides where money goes.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.provider_profile import ProviderProfile, KYCStatus, PayoutMethod
from app.services.payout_service import PayoutService
from app.services.provider_service import ProviderService
from app.exceptions import InvalidPayoutMethod, PaymentError
from app.utils.encryption import encrypt
from tests.factories.user_factory import UserFactory

VPA = "provider@okhdfcbank"
ACCOUNT = "1234567890"
IFSC = "HDFC0001234"

# update_profile takes every profile field positionally-ish; these are the ones this
# module never sets, kept in one place so the payout arguments stay readable.
_UNSET = dict(
    aadhaar=None, pan=None, upi_id=None, bank_account=None,
    service_area_lat=None, service_area_lng=None, service_area_radius_km=None,
)


@pytest.fixture(autouse=True)
def stub_payout_audit_io():
    """Stub the audit-write helpers, as tests/test_payouts.py does.

    They commit through ``AsyncSessionLocal`` — the app database — rather than the
    test session, so against test data they hit a foreign-key violation on
    ``payout_audits.admin_user_id``. Any module that triggers a payout needs this.
    """
    with patch.object(
        PayoutService, "_record_attempting_audit",
        new_callable=AsyncMock, return_value=uuid.uuid4(),
    ), patch.object(PayoutService, "_resolve_audit", new_callable=AsyncMock):
        yield


async def _provider(db):
    user = await UserFactory.create(db, role="provider")
    db.add(ProviderProfile(user_id=user.id, kyc_status=KYCStatus.VERIFIED))
    await db.flush()
    return user


async def _set_upi(db, user):
    return await ProviderService(db).update_profile(
        user.id, **_UNSET, payout_vpa=VPA,
        payout_account_number=None, payout_ifsc=None,
    )


async def _set_bank(db, user):
    return await ProviderService(db).update_profile(
        user.id, **_UNSET, payout_vpa=None,
        payout_account_number=ACCOUNT, payout_ifsc=IFSC,
    )


# --- saving a method makes it active, and keeps the other one -----------------

@pytest.mark.asyncio
async def test_saving_a_method_makes_it_active(db_session):
    user = await _provider(db_session)

    profile = await _set_upi(db_session, user)
    assert profile.payout_method is PayoutMethod.UPI
    assert profile.payout_display == VPA
    assert profile.payout_set is True


@pytest.mark.asyncio
async def test_switching_keeps_the_old_method_stored(db_session):
    """Vipul's call: switching must not delete the previous destination.

    The provider can flip back to it later without re-entering it — removal is an
    explicit action only.
    """
    user = await _provider(db_session)
    await _set_bank(db_session, user)
    profile = await _set_upi(db_session, user)

    assert profile.payout_method is PayoutMethod.UPI
    # The bank details are still on file, untouched.
    assert profile.payout_account_number_encrypted is not None
    assert profile.payout_ifsc_encrypted is not None


# --- the money-critical part: which destination actually gets paid ------------

async def _payout_destination(db_session, user):
    """Run a payout and report the destination RazorpayX was actually given."""
    from app.models.booking import BookingStatus, SettlementStatus
    from tests.test_payouts import _booking_for

    consumer = await UserFactory.create(db_session, role="consumer")
    admin = await UserFactory.create(db_session, role="admin")
    await _booking_for(db_session, consumer, user)

    with patch(
        "app.services.payout_service.create_payout", new_callable=AsyncMock
    ) as mock_payout:
        mock_payout.return_value = {"id": "pout_dest", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(user.id, admin.id)

    return mock_payout.call_args.kwargs


@pytest.mark.asyncio
async def test_switching_upi_to_bank_pays_the_bank(db_session):
    """The regression this ticket exists for.

    ``create_payout`` prefers UPI when handed both, so passing both along meant a
    provider who moved from UPI to a bank account kept being paid to the UPI handle
    they had just replaced.
    """
    user = await _provider(db_session)
    await _set_upi(db_session, user)
    await _set_bank(db_session, user)

    sent = await _payout_destination(db_session, user)

    assert sent["account_number"] == ACCOUNT
    assert sent["ifsc"] == IFSC
    assert sent["vpa"] is None, "paid the old UPI handle instead of the new bank account"


@pytest.mark.asyncio
async def test_switching_bank_to_upi_pays_the_upi(db_session):
    """The other direction — worked by luck before, must now work by design."""
    user = await _provider(db_session)
    await _set_bank(db_session, user)
    await _set_upi(db_session, user)

    sent = await _payout_destination(db_session, user)

    assert sent["vpa"] == VPA
    assert sent["account_number"] is None
    assert sent["ifsc"] is None


@pytest.mark.asyncio
async def test_payout_refuses_when_values_stored_but_no_method_active(db_session):
    """Stored is not the same as active — a payout must not guess."""
    user = await _provider(db_session)
    profile = await _set_upi(db_session, user)
    profile.payout_method = None  # e.g. a row that predates the backfill
    await db_session.flush()

    with pytest.raises(PaymentError, match="no payout details"):
        await _payout_destination(db_session, user)


# --- set active --------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_active_switches_without_re_entering(db_session):
    user = await _provider(db_session)
    await _set_bank(db_session, user)
    await _set_upi(db_session, user)  # UPI now active, bank still stored

    profile = await ProviderService(db_session).set_active_payout_method(
        user.id, PayoutMethod.BANK
    )

    assert profile.payout_method is PayoutMethod.BANK
    # The label follows the active method, masked for a bank account.
    assert profile.payout_display == "••••••7890"

    sent = await _payout_destination(db_session, user)
    assert sent["account_number"] == ACCOUNT


@pytest.mark.asyncio
async def test_set_active_rejects_a_method_with_nothing_stored(db_session):
    """Activating a destination we cannot pay to would fail later, at payout time."""
    user = await _provider(db_session)
    await _set_upi(db_session, user)

    with pytest.raises(InvalidPayoutMethod, match="No bank account on file"):
        await ProviderService(db_session).set_active_payout_method(
            user.id, PayoutMethod.BANK
        )


# --- remove ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remove_clears_both_methods(db_session):
    """"Remove my payout details" must leave the platform holding nothing."""
    user = await _provider(db_session)
    await _set_bank(db_session, user)
    await _set_upi(db_session, user)

    profile = await ProviderService(db_session).remove_payout_method(user.id)

    assert profile.payout_method is None
    assert profile.payout_display is None
    assert profile.payout_set is False
    assert profile.payout_vpa_encrypted is None
    assert profile.payout_account_number_encrypted is None
    assert profile.payout_ifsc_encrypted is None


@pytest.mark.asyncio
async def test_can_add_again_after_removing(db_session):
    user = await _provider(db_session)
    await _set_upi(db_session, user)
    await ProviderService(db_session).remove_payout_method(user.id)

    profile = await _set_bank(db_session, user)

    assert profile.payout_method is PayoutMethod.BANK
    assert profile.payout_set is True


# --- endpoints ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_endpoints_switch_and_remove(client, db_session, auth_headers):
    user = await _provider(db_session)
    await _set_bank(db_session, user)
    await _set_upi(db_session, user)
    headers = auth_headers(str(user.id), "provider")

    resp = await client.patch(
        "/api/v1/providers/me/payout-method", json={"method": "bank"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["payout_method"] == "bank"
    assert resp.json()["payout_display"] == "••••••7890"

    resp = await client.delete("/api/v1/providers/me/payout-method", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["payout_set"] is False
    assert body["payout_method"] is None
    assert body["payout_display"] is None


@pytest.mark.asyncio
async def test_set_active_endpoint_rejects_unstored_method(
    client, db_session, auth_headers
):
    user = await _provider(db_session)
    await _set_upi(db_session, user)

    resp = await client.patch(
        "/api/v1/providers/me/payout-method",
        json={"method": "bank"},
        headers=auth_headers(str(user.id), "provider"),
    )

    assert resp.status_code == 400
    assert "No bank account on file" in resp.json()["message"]


@pytest.mark.asyncio
async def test_cannot_set_both_methods_in_one_request(client, db_session, auth_headers):
    """Saving a method makes it active, so "both at once" has no defined answer."""
    user = await _provider(db_session)

    resp = await client.patch(
        "/api/v1/providers/me",
        json={
            "payout_vpa": VPA,
            "payout_account_number": ACCOUNT,
            "payout_ifsc": IFSC,
        },
        headers=auth_headers(str(user.id), "provider"),
    )

    assert resp.status_code == 422
    assert "one payout method at a time" in resp.text


# --- the guarantee that actually matters -------------------------------------

def test_payout_fields_are_not_exposed_on_any_other_provider_schema():
    """No endpoint may leak one provider's payout details to another user.

    ``payout_display`` returns a VPA in full, which is only acceptable because
    ``ProviderResponse`` is served exclusively for ``/providers/me``. Anything that
    returns *other* providers must carry no payout fields at all — adding one to
    build a UI would otherwise publish handles (often phone-number based) to
    strangers.
    """
    from app.schemas.provider import NearbyProviderResponse, ProviderResponse

    payout_fields = {"payout_set", "payout_method", "payout_display"}
    assert payout_fields <= set(ProviderResponse.model_fields)
    assert not (payout_fields & set(NearbyProviderResponse.model_fields))


def test_provider_response_is_only_served_for_self():
    """ProviderResponse must stay on /me routes only — the basis for the above."""
    from app.main import create_app
    from app.schemas.provider import ProviderResponse

    app = create_app()
    for route in app.routes:
        if getattr(route, "response_model", None) is ProviderResponse:
            assert "/providers/me" in route.path, (
                f"{route.path} returns ProviderResponse but is not self-scoped — it "
                f"would expose payout_display to another user"
            )
