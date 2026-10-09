"""
SVC-326: an SP can stop taking jobs itself ("off tools") while its team keeps
working.

- ``PATCH``/``GET /providers/me/self-pro``, with the solo guard;
- broadcast eligibility: a switched-off SP is offered a job only while a team
  Pro could be assigned it (active, KYC-verified, online, not on a job), in
  the single-service and cart statements alike;
- the claim paths while switched off: the pick list flags the Pro-for-self,
  and ``/assign`` to it and a plain accept are 409 ``SELF_NOT_TAKING_JOBS``;
- bookings already on the Pro-for-self are untouched;
- removing the last other Pro switches the Pro-for-self back on.
"""

import pytest

from app.models.address import Address
from app.models.booking import BookingStatus
from app.models.provider_profile import KYCStatus
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.services.provider_matching import (
    eligible_providers_multi_stmt,
    is_eligible_provider_multi_stmt,
)
from app.services.provider_service import ProviderService
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_two_level_dispatch import (
    ACCEPT,
    ASSIGN,
    PICK,
    _as,
    _team_pro,
    _team_sp,
    pushes,  # noqa: F401  (fixture)
)

SELF_PRO = "/api/v1/providers/me/self-pro"
TEAM = "/api/v1/providers/me/team"


async def _switch(client, auth_headers, sp, on: bool):
    return await client.patch(
        SELF_PRO, json={"accepts_direct_jobs": on}, headers=_as(auth_headers, sp)
    )


async def _switched_off_sp(client, auth_headers, db, service, **member):
    """An eligible SP with one team Pro (online + verified unless overridden),
    switched off."""
    sp = await _eligible_provider(db, service)
    self_pro = await ProviderService(db).ensure_self_professional(sp.id)
    pro = await _team_pro(db, sp.id, **member)
    resp = await _switch(client, auth_headers, sp, False)
    assert resp.status_code == 200, resp.text
    await db.refresh(self_pro)
    return sp, self_pro, pro


# --- The switch ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_and_back_on(client, db_session, auth_headers):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)

    resp = await _switch(client, auth_headers, sp, False)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "professional_id": str(self_pro.id),
        "accepts_direct_jobs": False,
        "team_has_assignable_pro": True,
    }
    resp = await client.get(SELF_PRO, headers=_as(auth_headers, sp))
    assert resp.json()["accepts_direct_jobs"] is False

    team = (await client.get(TEAM, headers=_as(auth_headers, sp))).json()
    assert [(p["is_self"], p["accepts_direct_jobs"]) for p in team] == [
        (True, False),
        (False, True),
    ]

    resp = await _switch(client, auth_headers, sp, True)
    assert resp.status_code == 200
    assert resp.json()["accepts_direct_jobs"] is True
    await db_session.refresh(self_pro)
    assert self_pro.accepts_direct_jobs is True


@pytest.mark.asyncio
async def test_a_solo_sp_cannot_switch_off(client, db_session, auth_headers):
    """Nobody else to send: the SP would be unbroadcastable."""
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    # A removed Pro doesn't count either.
    await _team_pro(db_session, sp.id, active=False)

    resp = await _switch(client, auth_headers, sp, False)
    assert resp.status_code == 409
    assert resp.json()["code"] == "NO_TEAM_TO_DISPATCH"
    self_pro = await ProviderService(db_session).ensure_self_professional(sp.id)
    assert self_pro.accepts_direct_jobs is True
    # Switching on is always allowed (a no-op here).
    assert (await _switch(client, auth_headers, sp, True)).status_code == 200


@pytest.mark.asyncio
async def test_team_has_assignable_pro_reflects_the_team(client, db_session, auth_headers):
    """Off is allowed with an offline Pro; the response tells the app to warn."""
    service = await _service(db_session)
    sp, _, pro = await _switched_off_sp(client, auth_headers, db_session, service, online=False)
    resp = await client.get(SELF_PRO, headers=_as(auth_headers, sp))
    assert resp.json()["team_has_assignable_pro"] is False

    pro.is_online = True
    await db_session.flush()
    resp = await client.get(SELF_PRO, headers=_as(auth_headers, sp))
    assert resp.json()["team_has_assignable_pro"] is True


@pytest.mark.asyncio
async def test_the_switch_is_provider_only(client, db_session, auth_headers):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    member_user_headers = auth_headers(str(member.user_id), "consumer")
    resp = await client.patch(
        SELF_PRO, json={"accepts_direct_jobs": False}, headers=member_user_headers
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cannot_assign_another_sps_pro(client, db_session, auth_headers, pushes):  # noqa: F811
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    _, other_self, _ = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(other_self.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "PRO_NOT_FOUND"


# --- Broadcast eligibility -----------------------------------------------------------


async def _cart(db, sp, service, multi: bool) -> list:
    """``[service]``, or a two-service cart the SP covers both of."""
    if not multi:
        return [service.id]
    second = await _service(db)
    db.add(ProviderServiceModel(provider_id=sp.id, service_id=second.id))
    await db.flush()
    return [service.id, second.id]


async def _offered(db, service_ids, sp) -> bool:
    """Both statements must agree: the broadcast set and the accept-time check."""
    _, booking = await _booking(db, service=None, provider_id=None)
    location = (await db.get(Address, booking.consumer_address_id)).location
    recipients = {
        u.id for u in (await db.scalars(eligible_providers_multi_stmt(service_ids, location))).all()
    }
    single = (
        await db.execute(is_eligible_provider_multi_stmt(service_ids, location, sp.id))
    ).scalar_one_or_none() is not None
    assert (sp.id in recipients) == single, "broadcast set and membership test disagree"
    return single


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True], ids=["single-service", "cart"])
@pytest.mark.parametrize(
    "unassignable",
    [
        pytest.param({"is_online": False}, id="offline"),
        pytest.param({"kyc_status": KYCStatus.PENDING}, id="unverified"),
        pytest.param({"is_active": False}, id="removed"),
    ],
)
async def test_a_switched_off_sp_with_no_assignable_pro_is_not_offered(
    client, db_session, auth_headers, multi, unassignable
):
    service = await _service(db_session)
    sp, _, pro = await _switched_off_sp(client, auth_headers, db_session, service)
    service_ids = await _cart(db_session, sp, service, multi)
    assert await _offered(db_session, service_ids, sp) is True

    # Make the only team Pro unassignable in one specific way. (Set directly:
    # removing through the API would switch the SP back on.)
    for column, value in unassignable.items():
        setattr(pro, column, value)
    await db_session.flush()

    assert await _offered(db_session, service_ids, sp) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True], ids=["single-service", "cart"])
async def test_a_switched_off_sp_whose_only_pro_is_busy_is_not_offered(
    client, db_session, auth_headers, multi
):
    service = await _service(db_session)
    sp, _, pro = await _switched_off_sp(client, auth_headers, db_session, service)
    service_ids = await _cart(db_session, sp, service, multi)
    assert await _offered(db_session, service_ids, sp) is True

    for status in (BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS):
        _, job = await _booking(db_session, service=service, provider_id=sp.id, status=status)
        job.professional_id = pro.id
        await db_session.flush()
        assert await _offered(db_session, service_ids, sp) is False, status
        job.status = BookingStatus.COMPLETED
        await db_session.flush()

    # Finished jobs don't make anyone busy.
    assert await _offered(db_session, service_ids, sp) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True], ids=["single-service", "cart"])
async def test_one_assignable_pro_keeps_a_switched_off_sp_offered(
    client, db_session, auth_headers, multi
):
    service = await _service(db_session)
    sp, _, _ = await _switched_off_sp(client, auth_headers, db_session, service, online=False)
    service_ids = await _cart(db_session, sp, service, multi)
    assert await _offered(db_session, service_ids, sp) is False

    await _team_pro(db_session, sp.id, name="Available")
    assert await _offered(db_session, service_ids, sp) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True], ids=["single-service", "cart"])
async def test_default_on_keeps_todays_behaviour(db_session, multi):
    """Every existing SP: on by default, offered whatever its team is doing,
    with or without a Pro-for-self row."""
    service = await _service(db_session)
    no_row = await _eligible_provider(db_session, service)
    with_team = await _eligible_provider(db_session, service)
    self_pro = await ProviderService(db_session).ensure_self_professional(with_team.id)
    assert self_pro.accepts_direct_jobs is True
    await _team_pro(db_session, with_team.id, online=False)

    for sp in (no_row, with_team):
        service_ids = await _cart(db_session, sp, service, multi)
        assert await _offered(db_session, service_ids, sp) is True


# --- Claim paths while switched off --------------------------------------------------


@pytest.mark.asyncio
async def test_the_pick_list_flags_the_switched_off_self(client, db_session, auth_headers, pushes):  # noqa: F811
    service = await _service(db_session)
    sp, self_pro, member = await _switched_off_sp(client, auth_headers, db_session, service)
    _, booking = await _booking(db_session, service=service)

    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp, **PICK))
    assert resp.status_code == 202, resp.text
    rows = {p["id"]: p["accepts_direct_jobs"] for p in resp.json()["pros"]}
    assert rows == {str(self_pro.id): False, str(member.id): True}


@pytest.mark.asyncio
async def test_assigning_the_switched_off_self_is_409(client, db_session, auth_headers, pushes):  # noqa: F811
    service = await _service(db_session)
    sp, self_pro, member = await _switched_off_sp(client, auth_headers, db_session, service)
    _, booking = await _booking(db_session, service=service)

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(self_pro.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "SELF_NOT_TAKING_JOBS"
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED and booking.provider_id is None
    assert pushes == []

    # The team Pro can still be assigned.
    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_plain_accept_while_switched_off_is_409_not_202(
    client,
    db_session,
    auth_headers,
    pushes,  # noqa: F811
):
    service = await _service(db_session)
    sp, _, _ = await _switched_off_sp(client, auth_headers, db_session, service)
    _, booking = await _booking(db_session, service=service)

    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 409
    assert resp.json()["code"] == "SELF_NOT_TAKING_JOBS"
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED and booking.provider_id is None
    assert pushes == []


@pytest.mark.asyncio
async def test_in_flight_self_bookings_are_untouched(client, db_session, auth_headers, pushes):  # noqa: F811
    """Held before switching off: a re-accept is still the idempotent 200, and
    the job still runs to completion."""
    service = await _service(db_session)
    sp, self_pro, _ = await _team_sp(db_session, service)
    _, booking = await _booking(db_session, service=service)
    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200

    assert (await _switch(client, auth_headers, sp, False)).status_code == 200

    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp))
    assert resp.status_code == 200, resp.text
    await db_session.refresh(booking)
    assert booking.professional_id == self_pro.id

    headers = _as(auth_headers, sp)
    base = f"/api/v1/bookings/{booking.id}"
    assert (await client.patch(f"{base}/en-route", headers=headers)).status_code == 200
    resp = await client.patch(f"{base}/start", json={"otp": booking.doorstep_otp}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert (await client.patch(f"{base}/complete", headers=headers)).status_code == 200


# --- The invariant: removing the last other Pro switches self back on ---------------


@pytest.mark.asyncio
async def test_removing_the_last_other_pro_switches_self_back_on(client, db_session, auth_headers):
    service = await _service(db_session)
    sp, self_pro, first = await _switched_off_sp(client, auth_headers, db_session, service)
    second = await _team_pro(db_session, sp.id, name="Second")

    resp = await client.delete(f"{TEAM}/{first.id}", headers=_as(auth_headers, sp))
    assert resp.status_code == 200
    assert resp.json() == {"self_reenabled": False}
    await db_session.refresh(self_pro)
    assert self_pro.accepts_direct_jobs is False

    resp = await client.delete(f"{TEAM}/{second.id}", headers=_as(auth_headers, sp))
    assert resp.status_code == 200
    assert resp.json() == {"self_reenabled": True}
    await db_session.refresh(self_pro)
    assert self_pro.accepts_direct_jobs is True
