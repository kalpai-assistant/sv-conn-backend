"""
SVC-320 (Phase B, PR 1): SP team management, with SVC-327's invite KYC gate.

Invites (create, revoke, expiry, single use, rate limits), the 6-digit code
and its per-phone uniqueness, the mock-SMS knob, accept-invite by phone + code
(one SP at a time, reactivation), the team list and soft removal, and the
Pro-for-self every new provider now gets at signup.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.models.professional import Professional
from app.models.provider_profile import KYCStatus
from app.models.provider_team_invite import ProviderTeamInvite
from app.services.provider_service import ProviderService
from app.utils import sms_client
from tests.factories.user_factory import UserFactory

RANDBELOW = "app.services.team_service.secrets.randbelow"
INVITES = "/api/v1/providers/me/team/invites"
TEAM = "/api/v1/providers/me/team"
ACCEPT = "/api/v1/providers/me/accept-invite"


@pytest.fixture(autouse=True)
def mock_invite_sms(monkeypatch):
    """Mock mode unless a test opts in: no template ID, so no MSG91 call."""
    monkeypatch.setattr(sms_client.settings, "MSG91_INVITE_TEMPLATE_ID", "")


async def _sp(db, *, kyc=KYCStatus.VERIFIED, business_name="Sparkle Cleaners", name="Owner"):
    """A provider through the real creation path, so it has its Pro-for-self."""
    user = await UserFactory.create(db, role="provider", name=name)
    profile, _ = await ProviderService(db).get_or_create_profile(user.id)
    profile.kyc_status = kyc
    profile.business_name = business_name
    await db.flush()
    return user


async def _invite(client, auth_headers, sp, phone):
    return await client.post(
        INVITES, json={"invitee_phone": phone}, headers=auth_headers(str(sp.id), "provider")
    )


async def _accept(client, auth_headers, user, token):
    return await client.post(
        ACCEPT, json={"token": token}, headers=auth_headers(str(user.id), user.role.value)
    )


async def _pros_of(db, user_id) -> list[Professional]:
    rows = await db.execute(select(Professional).where(Professional.user_id == user_id))
    return list(rows.scalars().all())


# --- Pro-for-self at signup ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_provider_gets_a_pro_for_self(db_session):
    user = await UserFactory.create(db_session, role="provider", name="  Sunita ")
    _, is_new = await ProviderService(db_session).get_or_create_profile(user.id)
    assert is_new

    [pro] = await _pros_of(db_session, user.id)
    assert pro.service_provider_id == user.id
    assert pro.is_self and pro.is_active
    assert pro.display_name == "Sunita"

    # A second call finds the profile and adds nothing.
    _, is_new = await ProviderService(db_session).get_or_create_profile(user.id)
    assert not is_new
    assert len(await _pros_of(db_session, user.id)) == 1


@pytest.mark.asyncio
async def test_pro_for_self_display_name_never_falls_back_to_the_phone(db_session):
    user = await UserFactory.create(db_session, role="provider", name="   ")
    await ProviderService(db_session).get_or_create_profile(user.id)
    [pro] = await _pros_of(db_session, user.id)
    assert pro.display_name == "Professional"


@pytest.mark.asyncio
async def test_get_providers_me_creates_the_pro_for_self(client, db_session, auth_headers):
    user = await UserFactory.create(db_session, role="provider")
    resp = await client.get("/api/v1/providers/me", headers=auth_headers(str(user.id), "provider"))
    assert resp.status_code == 200
    assert len(await _pros_of(db_session, user.id)) == 1


# --- Creating invites ------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_invite_normalises_the_phone_and_logs_a_mock_sms(
    client, db_session, auth_headers, caplog
):
    sp = await _sp(db_session)
    caplog.set_level(logging.INFO, logger="app.utils.sms_client")

    with patch("app.utils.sms_client.httpx.AsyncClient") as http:
        resp = await _invite(client, auth_headers, sp, "98765 43210")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["invitee_phone"] == "+919876543210"
    assert body["status"] == "PENDING"
    assert body["token"].isdigit() and len(body["token"]) == 6
    # Fully static: STPL rejects any variable in the URL (SVC-328).
    assert body["invite_url"] == "https://app.eazyserv.com/invite"
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(UTC) <= timedelta(days=7)

    http.assert_not_called()
    [line] = [r.getMessage() for r in caplog.records if "[MOCK INVITE]" in r.getMessage()]
    assert "SP=Sparkle Cleaners" in line
    assert "phone=+919876543210" in line
    assert f"code={body['token']}" in line
    assert "url=https://app.eazyserv.com/invite" in line


@pytest.mark.parametrize("phone", ["12345", "+15551234567", "+915876543210"])
@pytest.mark.asyncio
async def test_create_invite_rejects_a_non_indian_mobile(client, db_session, auth_headers, phone):
    sp = await _sp(db_session)
    resp = await _invite(client, auth_headers, sp, phone)
    assert resp.status_code == 422


@pytest.mark.parametrize("kyc", [KYCStatus.PENDING, KYCStatus.SUBMITTED, KYCStatus.REJECTED])
@pytest.mark.asyncio
async def test_kyc_gate_403_before_any_token_or_sms(client, db_session, auth_headers, kyc):
    sp = await _sp(db_session, kyc=kyc)
    with patch("app.utils.sms_client.send_team_invite", new=AsyncMock()) as send:
        resp = await _invite(client, auth_headers, sp, "+919876543210")

    assert resp.status_code == 403
    assert resp.json()["code"] == "INVITER_PAYOUT_KYC_INCOMPLETE"
    send.assert_not_awaited()
    invites = await db_session.scalars(
        select(ProviderTeamInvite).where(ProviderTeamInvite.invited_by == sp.id)
    )
    assert invites.all() == []


@pytest.mark.asyncio
async def test_create_invite_is_provider_only(client, db_session, auth_headers):
    consumer = await UserFactory.create(db_session, role="consumer")
    resp = await client.post(
        INVITES,
        json={"invitee_phone": "+919876543210"},
        headers=auth_headers(str(consumer.id), "consumer"),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cannot_invite_yourself(client, db_session, auth_headers):
    sp = await _sp(db_session)
    resp = await _invite(client, auth_headers, sp, sp.phone)
    assert resp.status_code == 400
    assert resp.json()["code"] == "CANNOT_INVITE_SELF"


@pytest.mark.asyncio
async def test_a_second_live_invite_to_the_same_phone_is_409(client, db_session, auth_headers):
    sp = await _sp(db_session)
    assert (await _invite(client, auth_headers, sp, "+919876543210")).status_code == 201
    resp = await _invite(client, auth_headers, sp, "+919876543210")
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVITE_ALREADY_PENDING"

    # A different SP may invite the same phone.
    other = await _sp(db_session)
    assert (await _invite(client, auth_headers, other, "+919876543210")).status_code == 201


@pytest.mark.asyncio
async def test_an_expired_invite_does_not_block_a_new_one(client, db_session, auth_headers):
    sp = await _sp(db_session)
    first = (await _invite(client, auth_headers, sp, "+919876543210")).json()
    stale = await db_session.get(ProviderTeamInvite, uuid.UUID(first["id"]))
    stale.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    resp = await _invite(client, auth_headers, sp, "+919876543210")
    assert resp.status_code == 201
    await db_session.refresh(stale)
    assert stale.status == "EXPIRED"


@pytest.mark.asyncio
async def test_invite_creation_is_rate_limited_to_5_an_hour(client, db_session, auth_headers):
    sp = await _sp(db_session)
    for i in range(5):
        resp = await _invite(client, auth_headers, sp, f"+91987654321{i}")
        assert resp.status_code == 201, resp.text
    resp = await _invite(client, auth_headers, sp, "+919876543219")
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_real_send_uses_the_msg91_flow_api(client, db_session, auth_headers, monkeypatch):
    monkeypatch.setattr(sms_client.settings, "MSG91_INVITE_TEMPLATE_ID", "tpl_invite")
    monkeypatch.setattr(sms_client.settings, "MSG91_AUTH_KEY", "key")
    sp = await _sp(db_session)

    with patch("app.utils.sms_client.httpx.AsyncClient") as http:
        post = AsyncMock(return_value=httpx.Response(200, json={"type": "success"}))
        http.return_value.__aenter__.return_value.post = post
        resp = await _invite(client, auth_headers, sp, "+919876543210")

    assert resp.status_code == 201
    url = post.await_args.args[0]
    payload = post.await_args.kwargs["json"]
    assert url.endswith("/api/v5/flow")
    assert payload["template_id"] == "tpl_invite"
    assert payload["recipients"] == [
        {"mobiles": "919876543210", "var1": "Sparkle Cleaners", "var2": resp.json()["token"]}
    ]


@pytest.mark.asyncio
async def test_an_msg91_rejection_fails_the_request_with_502(
    client, db_session, auth_headers, monkeypatch
):
    """The send runs inside the request, so a rejection fails it and get_db rolls
    the invite back. (This harness never commits or rolls back, so the rollback
    itself can't be observed here; the 502 is what proves the error propagates.)
    """
    monkeypatch.setattr(sms_client.settings, "MSG91_INVITE_TEMPLATE_ID", "tpl_invite")
    sp = await _sp(db_session)
    with patch("app.utils.sms_client.httpx.AsyncClient") as http:
        http.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=httpx.Response(200, json={"type": "error", "message": "DLT"})
        )
        resp = await _invite(client, auth_headers, sp, "+919876543210")
    assert resp.status_code == 502


# --- Revoking ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_code_is_410_for_its_own_invitee(client, db_session, auth_headers):
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    invite = (await _invite(client, auth_headers, sp, invitee.phone)).json()

    resp = await client.post(
        f"{INVITES}/{invite['id']}/revoke", headers=auth_headers(str(sp.id), "provider")
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "REVOKED"

    again = await client.post(
        f"{INVITES}/{invite['id']}/revoke", headers=auth_headers(str(sp.id), "provider")
    )
    assert again.status_code == 409

    resp = await _accept(client, auth_headers, invitee, invite["token"])
    assert resp.status_code == 410
    assert resp.json()["code"] == "INVITE_NO_LONGER_VALID"


@pytest.mark.asyncio
async def test_cannot_revoke_another_sps_invite(client, db_session, auth_headers):
    sp, other = await _sp(db_session), await _sp(db_session)
    invite = (await _invite(client, auth_headers, sp, "+919876543210")).json()
    resp = await client.post(
        f"{INVITES}/{invite['id']}/revoke", headers=auth_headers(str(other.id), "provider")
    )
    assert resp.status_code == 404


# --- Accepting ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_consumer_accepts_and_joins_the_team(client, db_session, auth_headers):
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer", name="Rohan")
    # Without the +91: normalised to match users.phone.
    invite = (await _invite(client, auth_headers, sp, invitee.phone[3:])).json()

    resp = await _accept(client, auth_headers, invitee, invite["token"])
    assert resp.status_code == 200, resp.text
    pro = resp.json()
    assert pro["service_provider_id"] == str(sp.id)
    assert pro["user_id"] == str(invitee.id)
    assert pro["display_name"] == "Rohan"
    assert pro["is_active"] is True and pro["is_self"] is False
    assert pro["kyc_status"] == "pending"  # the Pro's own KYC; not inherited from the SP

    row = await db_session.get(ProviderTeamInvite, uuid.UUID(invite["id"]))
    assert row.status == "ACCEPTED"
    assert row.accepted_by_user_id == invitee.id
    assert row.accepted_at is not None

    # Single use. The code was the caller's own, so 410 rather than 404.
    resp = await _accept(client, auth_headers, invitee, invite["token"])
    assert resp.status_code == 410
    assert resp.json()["code"] == "INVITE_NO_LONGER_VALID"


@pytest.mark.asyncio
async def test_another_users_valid_code_is_the_same_404_as_a_wrong_one(
    client, db_session, auth_headers
):
    """No existence leak: a code texted to someone else looks exactly like a
    code that was never issued."""
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    stranger = await UserFactory.create(db_session, role="consumer")
    with patch(RANDBELOW, return_value=123456):
        invite = (await _invite(client, auth_headers, sp, invitee.phone)).json()

    theirs = await _accept(client, auth_headers, stranger, invite["token"])
    never_issued = await _accept(client, auth_headers, stranger, "654321")
    assert theirs.status_code == never_issued.status_code == 404
    assert theirs.json() == never_issued.json()
    assert theirs.json()["code"] == "INVITE_NOT_FOUND"
    assert await _pros_of(db_session, stranger.id) == []

    # The invite is untouched and still works for the person it was sent to.
    assert (await _accept(client, auth_headers, invitee, invite["token"])).status_code == 200


@pytest.mark.asyncio
async def test_accept_an_unknown_code_is_404(client, db_session, auth_headers):
    user = await UserFactory.create(db_session, role="consumer")
    resp = await _accept(client, auth_headers, user, "000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_wrong_code_is_404(client, db_session, auth_headers):
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    with patch(RANDBELOW, return_value=111111):
        await _invite(client, auth_headers, sp, invitee.phone)
    resp = await _accept(client, auth_headers, invitee, "111112")
    assert resp.status_code == 404
    assert resp.json()["code"] == "INVITE_NOT_FOUND"


@pytest.mark.parametrize("code", ["12345", "1234567", "12a456", "", "no-such-token"])
@pytest.mark.asyncio
async def test_a_malformed_code_is_422(client, db_session, auth_headers, code):
    user = await UserFactory.create(db_session, role="consumer")
    resp = await _accept(client, auth_headers, user, code)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_accept_is_rate_limited_to_10_an_hour_and_wrong_codes_count(
    client, db_session, auth_headers
):
    """Wrong and malformed codes both count: the limit is what makes a 6-digit
    code safe to guess against."""
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    with patch(RANDBELOW, return_value=999999):
        invite = (await _invite(client, auth_headers, sp, invitee.phone)).json()

    assert (await _accept(client, auth_headers, invitee, "abc")).status_code == 422
    for i in range(9):
        resp = await _accept(client, auth_headers, invitee, f"{i:06d}")
        assert resp.status_code == 404, resp.text
    # The 11th call is refused even with the right code.
    resp = await _accept(client, auth_headers, invitee, invite["token"])
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_accept_an_expired_invite_is_410(client, db_session, auth_headers):
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    invite = (await _invite(client, auth_headers, sp, invitee.phone)).json()
    row = await db_session.get(ProviderTeamInvite, uuid.UUID(invite["id"]))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    resp = await _accept(client, auth_headers, invitee, invite["token"])
    assert resp.status_code == 410
    assert "expired" in resp.json()["message"]


@pytest.mark.asyncio
async def test_an_admin_cannot_accept(client, db_session, auth_headers):
    sp = await _sp(db_session)
    admin = await UserFactory.create(db_session, role="admin")
    invite = (await _invite(client, auth_headers, sp, admin.phone)).json()
    resp = await _accept(client, auth_headers, admin, invite["token"])
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_an_active_pro_elsewhere_cannot_accept(client, db_session, auth_headers):
    sp_a, sp_b = await _sp(db_session), await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    first = (await _invite(client, auth_headers, sp_a, invitee.phone)).json()
    assert (await _accept(client, auth_headers, invitee, first["token"])).status_code == 200

    second = (await _invite(client, auth_headers, sp_b, invitee.phone)).json()
    resp = await _accept(client, auth_headers, invitee, second["token"])
    assert resp.status_code == 400
    assert resp.json()["code"] == "ALREADY_PRO_ELSEWHERE"
    assert {p.service_provider_id for p in await _pros_of(db_session, invitee.id)} == {sp_a.id}


@pytest.mark.asyncio
async def test_an_sp_cannot_join_another_team(client, db_session, auth_headers):
    """Their own Pro-for-self is an active row under another SP."""
    owner, other_sp = await _sp(db_session), await _sp(db_session)
    invite = (await _invite(client, auth_headers, owner, other_sp.phone)).json()
    resp = await _accept(client, auth_headers, other_sp, invite["token"])
    assert resp.status_code == 400
    assert resp.json()["code"] == "ALREADY_PRO_ELSEWHERE"


@pytest.mark.asyncio
async def test_a_pro_removed_elsewhere_can_join_a_new_team(client, db_session, auth_headers):
    sp_a, sp_b = await _sp(db_session), await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    first = (await _invite(client, auth_headers, sp_a, invitee.phone)).json()
    pro_a = (await _accept(client, auth_headers, invitee, first["token"])).json()
    resp = await client.delete(
        f"{TEAM}/{pro_a['id']}", headers=auth_headers(str(sp_a.id), "provider")
    )
    assert resp.status_code == 204

    second = (await _invite(client, auth_headers, sp_b, invitee.phone)).json()
    resp = await _accept(client, auth_headers, invitee, second["token"])
    assert resp.status_code == 200
    assert resp.json()["service_provider_id"] == str(sp_b.id)


@pytest.mark.asyncio
async def test_reinviting_a_removed_pro_reactivates_the_same_row(client, db_session, auth_headers):
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    first = (await _invite(client, auth_headers, sp, invitee.phone)).json()
    pro = (await _accept(client, auth_headers, invitee, first["token"])).json()
    await client.delete(f"{TEAM}/{pro['id']}", headers=auth_headers(str(sp.id), "provider"))

    second = (await _invite(client, auth_headers, sp, invitee.phone)).json()
    resp = await _accept(client, auth_headers, invitee, second["token"])
    assert resp.status_code == 200
    assert resp.json()["id"] == pro["id"]
    assert resp.json()["is_active"] is True
    assert len(await _pros_of(db_session, invitee.id)) == 1


@pytest.mark.asyncio
async def test_cannot_invite_someone_already_on_the_team(client, db_session, auth_headers):
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    invite = (await _invite(client, auth_headers, sp, invitee.phone)).json()
    await _accept(client, auth_headers, invitee, invite["token"])

    resp = await _invite(client, auth_headers, sp, invitee.phone)
    assert resp.status_code == 409
    assert resp.json()["code"] == "ALREADY_ON_TEAM"


# --- Team list + removal ----------------------------------------------------------


@pytest.mark.asyncio
async def test_team_lists_self_first_and_excludes_removed(client, db_session, auth_headers):
    sp = await _sp(db_session)
    headers = auth_headers(str(sp.id), "provider")
    members = []
    for _ in range(2):
        user = await UserFactory.create(db_session, role="consumer")
        invite = (await _invite(client, auth_headers, sp, user.phone)).json()
        members.append((await _accept(client, auth_headers, user, invite["token"])).json())
    first, second = members

    team = (await client.get(TEAM, headers=headers)).json()
    assert [m["is_self"] for m in team] == [True, False, False]
    assert team[0]["user_id"] == str(sp.id)

    await client.delete(f"{TEAM}/{first['id']}", headers=headers)
    team = (await client.get(TEAM, headers=headers)).json()
    assert [m["id"] for m in team[1:]] == [second["id"]]

    removed = await db_session.get(Professional, uuid.UUID(first["id"]))
    assert removed is not None, "soft delete keeps the row"
    assert removed.is_active is False and removed.is_online is False


@pytest.mark.asyncio
async def test_a_new_providers_team_is_just_themselves(client, db_session, auth_headers):
    user = await UserFactory.create(db_session, role="provider")
    team = (await client.get(TEAM, headers=auth_headers(str(user.id), "provider"))).json()
    assert len(team) == 1 and team[0]["is_self"] is True


@pytest.mark.asyncio
async def test_cannot_remove_your_own_pro_for_self(client, db_session, auth_headers):
    sp = await _sp(db_session)
    [self_pro] = await _pros_of(db_session, sp.id)
    resp = await client.delete(
        f"{TEAM}/{self_pro.id}", headers=auth_headers(str(sp.id), "provider")
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "CANNOT_REMOVE_SELF"
    await db_session.refresh(self_pro)
    assert self_pro.is_active is True


@pytest.mark.asyncio
async def test_cannot_remove_another_sps_pro(client, db_session, auth_headers):
    sp, other = await _sp(db_session), await _sp(db_session)
    [their_pro] = await _pros_of(db_session, other.id)
    resp = await client.delete(
        f"{TEAM}/{their_pro.id}", headers=auth_headers(str(sp.id), "provider")
    )
    assert resp.status_code == 404
    await db_session.refresh(their_pro)
    assert their_pro.is_active is True


@pytest.mark.asyncio
async def test_real_send_truncates_var1_to_the_dlt_limit(monkeypatch):
    """DLT caps every template variable at 30 characters."""
    monkeypatch.setattr(sms_client.settings, "MSG91_INVITE_TEMPLATE_ID", "tpl_invite")
    with patch("app.utils.sms_client.httpx.AsyncClient") as http:
        post = AsyncMock(return_value=httpx.Response(200, json={"type": "success"}))
        http.return_value.__aenter__.return_value.post = post
        await sms_client.send_team_invite("+919876543210", "A" * 100, "004217")
    [recipient] = post.await_args.kwargs["json"]["recipients"]
    assert recipient["var1"] == "A" * 30


@pytest.mark.asyncio
async def test_kyc_gate_does_not_create_an_sp_for_a_provider_without_a_profile(
    client, db_session, auth_headers
):
    user = await UserFactory.create(db_session, role="provider")
    resp = await _invite(client, auth_headers, user, "+919876543210")
    assert resp.status_code == 403
    assert resp.json()["code"] == "INVITER_PAYOUT_KYC_INCOMPLETE"
    assert await ProviderService(db_session).get_profile(user.id) is None
    assert await _pros_of(db_session, user.id) == []


# --- The 6-digit code ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_code_is_zero_padded_to_6_digits(client, db_session, auth_headers):
    sp = await _sp(db_session)
    with patch(RANDBELOW, return_value=42) as randbelow:
        resp = await _invite(client, auth_headers, sp, "+919876543210")
    assert resp.json()["token"] == "000042"
    randbelow.assert_called_with(1_000_000)


@pytest.mark.asyncio
async def test_a_code_collision_for_the_same_phone_draws_a_new_code(
    client, db_session, auth_headers
):
    """Live codes are unique per phone. A second SP inviting the same phone gets
    a fresh code when the first draw matches the live one."""
    sp_a, sp_b = await _sp(db_session), await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    with patch(RANDBELOW, return_value=111111):
        first = (await _invite(client, auth_headers, sp_a, invitee.phone)).json()
    with patch(RANDBELOW, side_effect=[111111, 222222]):
        resp = await _invite(client, auth_headers, sp_b, invitee.phone)

    assert resp.status_code == 201, resp.text
    assert (first["token"], resp.json()["token"]) == ("111111", "222222")
    # Each code reaches its own SP's team.
    pro = (await _accept(client, auth_headers, invitee, "222222")).json()
    assert pro["service_provider_id"] == str(sp_b.id)


@pytest.mark.asyncio
async def test_the_same_code_for_another_phone_is_no_collision(client, db_session, auth_headers):
    sp = await _sp(db_session)
    with patch(RANDBELOW, return_value=333333):
        a = await _invite(client, auth_headers, sp, "+919876543210")
        b = await _invite(client, auth_headers, sp, "+919876543211")
    assert a.json()["token"] == b.json()["token"] == "333333"


@pytest.mark.asyncio
async def test_endless_collisions_give_up_with_503(client, db_session, auth_headers):
    sp_a, sp_b = await _sp(db_session), await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    with patch(RANDBELOW, return_value=444444):
        await _invite(client, auth_headers, sp_a, invitee.phone)
        resp = await _invite(client, auth_headers, sp_b, invitee.phone)
    assert resp.status_code == 503
    assert resp.json()["code"] == "INVITE_CODE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_history_rows_with_the_same_code_do_not_block_a_new_invite(
    client, db_session, auth_headers
):
    """Accepted / revoked / expired rows keep their codes; only live codes are
    unique. A reused code reaches the live invite, not the old one."""
    sp = await _sp(db_session)
    invitee = await UserFactory.create(db_session, role="consumer")
    headers = auth_headers(str(sp.id), "provider")
    with patch(RANDBELOW, return_value=555555):
        old = (await _invite(client, auth_headers, sp, invitee.phone)).json()
        await client.post(f"{INVITES}/{old['id']}/revoke", headers=headers)
        new = await _invite(client, auth_headers, sp, invitee.phone)

    assert new.status_code == 201, new.text
    assert new.json()["token"] == "555555"
    resp = await _accept(client, auth_headers, invitee, "555555")
    assert resp.status_code == 200
    row = await db_session.get(ProviderTeamInvite, uuid.UUID(new.json()["id"]))
    assert row.status == "ACCEPTED"


# --- Listing invites (SVC-320 PR 2) ----------------------------------------------------


@pytest.mark.asyncio
async def test_list_invites_is_newest_first_with_every_status(client, db_session, auth_headers):
    sp = await _sp(db_session)
    headers = auth_headers(str(sp.id), "provider")
    phones = ["+919876543210", "+919876543211", "+919876543212"]
    made = [(await _invite(client, auth_headers, sp, phone)).json() for phone in phones]
    await client.post(f"{INVITES}/{made[1]['id']}/revoke", headers=headers)
    # One transaction means one now(): spread created_at so the order is real.
    for age, invite in enumerate(reversed(made)):
        row = await db_session.get(ProviderTeamInvite, uuid.UUID(invite["id"]))
        row.created_at = datetime.now(UTC) - timedelta(minutes=age)
    # And one PENDING invite that has quietly expired.
    stale = await db_session.get(ProviderTeamInvite, uuid.UUID(made[0]["id"]))
    stale.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    resp = await client.get(INVITES, headers=headers)

    assert resp.status_code == 200, resp.text
    assert [(i["invitee_phone"], i["status"]) for i in resp.json()] == [
        ("+919876543212", "PENDING"),
        ("+919876543211", "REVOKED"),
        ("+919876543210", "EXPIRED"),
    ]
    await db_session.refresh(stale)
    assert stale.status == "EXPIRED", "the lazy expiry is saved, not just shown"


@pytest.mark.asyncio
async def test_list_invites_shows_only_mine(client, db_session, auth_headers):
    sp, other = await _sp(db_session), await _sp(db_session)
    await _invite(client, auth_headers, other, "+919876543210")
    resp = await client.get(INVITES, headers=auth_headers(str(sp.id), "provider"))
    assert resp.json() == []
    consumer = await UserFactory.create(db_session, role="consumer")
    resp = await client.get(INVITES, headers=auth_headers(str(consumer.id), "consumer"))
    assert resp.status_code == 403
