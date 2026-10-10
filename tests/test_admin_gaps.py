"""
SVC-329 (3/3): gaps found building the admin frontend.

- ``PRO_HAS_ACTIVE_JOB`` says which booking(s) block the removal
  (``details.booking_ids``), on the owner's DELETE, admin force-remove and
  reassign alike;
- ``GET /admin/providers/{id}``: one owner, the same row the search returns;
- a Pro-for-self shows its owner's KYC (display only: the self row itself is
  never written), in My Team, the admin team and admin Pro detail.
"""

import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.models.booking import BookingStatus
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _service
from tests.test_two_level_dispatch import _as, _team_pro, _team_sp

A = "/api/v1/admin"
TEAM = "/api/v1/providers/me/team"


@pytest.fixture
async def as_admin(db_session, auth_headers):
    admin = await UserFactory.create(db_session, role="admin")
    return auth_headers(str(admin.id), "admin")


async def _job(db, service, sp, pro, status):
    _, job = await _booking(db, service=service, provider_id=sp.id, status=status)
    job.professional_id = pro.id
    await db.flush()
    return job


async def _profile(db, sp_id) -> ProviderProfile:
    return await db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == sp_id))


# --- FIX 1: which job blocks a removal ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["owner-delete", "admin-force-remove", "admin-reassign"])
async def test_pro_has_active_job_names_the_blocking_bookings(
    client, db_session, auth_headers, as_admin, path
):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    first = await _job(db_session, service, sp, member, BookingStatus.ACCEPTED)
    second = await _job(db_session, service, sp, member, BookingStatus.IN_PROGRESS)
    await _job(db_session, service, sp, member, BookingStatus.COMPLETED)
    target, _, _ = await _team_sp(db_session, service)

    if path == "owner-delete":
        resp = await client.delete(f"{TEAM}/{member.id}", headers=_as(auth_headers, sp))
    elif path == "admin-force-remove":
        resp = await client.post(
            f"{A}/pros/{member.id}/force-remove", json={"reason": "left"}, headers=as_admin
        )
    else:
        resp = await client.post(
            f"{A}/pros/{member.id}/reassign",
            json={"target_sp_user_id": str(target.id), "reason": "merger"},
            headers=as_admin,
        )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "PRO_HAS_ACTIVE_JOB"
    assert body["message"] == "Rohan is on a job. Reassign or finish it before removing them."
    # Finished jobs aren't listed. (Order is created_at, then id; both rows
    # share one transaction's created_at here, so compare as a set.)
    assert list(body["details"]) == ["booking_ids"]
    assert sorted(body["details"]["booking_ids"]) == sorted([str(first.id), str(second.id)])


# --- FIX 2: one owner ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_detail_is_the_search_row(client, db_session, as_admin):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    await _team_pro(db_session, sp.id, name="Second")
    (await _profile(db_session, sp.id)).business_name = f"Shine{uuid.uuid4().hex[:8]}"
    await db_session.flush()

    detail = await client.get(f"{A}/providers/{sp.id}", headers=as_admin)
    search = await client.get(f"{A}/providers", params={"q": sp.phone}, headers=as_admin)

    assert detail.status_code == 200, detail.text
    assert [detail.json()] == search.json()
    assert detail.json()["active_pro_count"] == 2


@pytest.mark.asyncio
async def test_owner_detail_404s_for_unknown_and_non_owners(client, db_session, as_admin):
    consumer = await UserFactory.create(db_session, role="consumer")
    for user_id in (uuid.uuid4(), consumer.id):
        resp = await client.get(f"{A}/providers/{user_id}", headers=as_admin)
        assert (resp.status_code, resp.json()["code"]) == (404, "SP_NOT_FOUND")
    assert await _profile(db_session, consumer.id) is None


@pytest.mark.asyncio
async def test_owner_detail_is_admin_only(client, db_session, auth_headers):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    resp = await client.get(f"{A}/providers/{sp.id}", headers=_as(auth_headers, sp))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_the_literal_provider_routes_still_resolve(client, db_session, as_admin):
    """``/providers/{sp_user_id}`` must not swallow the existing literal paths."""
    for path in ("kyc/pending", "pending-settlements"):
        resp = await client.get(f"{A}/providers/{path}", headers=as_admin)
        assert resp.status_code == 200, (path, resp.text)


# --- FIX 3: a Pro-for-self shows its owner's KYC ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_kyc", [KYCStatus.VERIFIED, KYCStatus.REJECTED])
async def test_the_self_row_shows_the_owners_kyc_everywhere(
    client, db_session, auth_headers, as_admin, owner_kyc
):
    """The self row is created PENDING and verifying the owner writes only the
    profile: the state of every owner created after the SVC-339 backfill."""
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    member.kyc_status = KYCStatus.PENDING
    (await _profile(db_session, sp.id)).kyc_status = owner_kyc
    await db_session.flush()
    assert self_pro.kyc_status is KYCStatus.PENDING

    mine = (await client.get(TEAM, headers=_as(auth_headers, sp))).json()
    admin_team = (await client.get(f"{A}/providers/{sp.id}/team", headers=as_admin)).json()
    detail = (await client.get(f"{A}/pros/{self_pro.id}", headers=as_admin)).json()

    for rows in (mine, admin_team):
        kyc = {r["id"]: r["kyc_status"] for r in rows}
        assert kyc[str(self_pro.id)] == owner_kyc.value
        # A team Pro keeps their own KYC.
        assert kyc[str(member.id)] == "pending"
    assert detail["kyc_status"] == owner_kyc.value

    # Display only: the self row itself is untouched.
    await db_session.flush()
    await db_session.refresh(self_pro)
    assert self_pro.kyc_status is KYCStatus.PENDING


@pytest.mark.asyncio
async def test_my_team_statement_count_does_not_grow_with_the_team(
    client, db_session, auth_headers
):
    """``/me/team`` runs on every team accept (the pick step); the owner's KYC
    must not cost a read per row."""
    service = await _service(db_session)
    small, _, _ = await _team_sp(db_session, service)
    big, _, _ = await _team_sp(db_session, service)
    for i in range(10):
        await _team_pro(db_session, big.id, name=f"Pro{i}")

    async def statements(sp):
        executed: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            executed.append(statement)

        event.listen(Engine, "before_cursor_execute", record)
        try:
            resp = await client.get(TEAM, headers=_as(auth_headers, sp))
        finally:
            event.remove(Engine, "before_cursor_execute", record)
        assert resp.status_code == 200, resp.text
        return len(executed), len(resp.json())

    small_count, small_rows = await statements(small)
    big_count, big_rows = await statements(big)
    assert (small_rows, big_rows) == (2, 12)
    assert small_count == big_count


@pytest.mark.asyncio
async def test_pros_me_still_shows_the_owners_kyc(client, db_session, auth_headers):
    """``/pros/me`` moved onto the shared helper; same answer as before."""
    service = await _service(db_session)
    sp, self_pro, _ = await _team_sp(db_session, service)
    assert self_pro.kyc_status is KYCStatus.PENDING

    resp = await client.get("/api/v1/pros/me", headers=_as(auth_headers, sp))

    assert resp.status_code == 200, resp.text
    assert resp.json()["kyc_status"] == "verified"
    rows = (
        (await db_session.execute(select(Professional).where(Professional.id == self_pro.id)))
        .scalars()
        .all()
    )
    assert rows[0].kyc_status is KYCStatus.PENDING
