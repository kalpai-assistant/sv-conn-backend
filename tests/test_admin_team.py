"""
SVC-329 (2/2): admin team management.

- ``GET /admin/providers?q=``: search owners by phone / name / business name,
  wildcards escaped, one statement per page;
- ``GET /admin/providers/{id}/team`` (removed Pros included, job counts) and
  ``/invites`` (every status, no codes, stale flip saved);
- ``GET /admin/pros/{id}``;
- ``POST /admin/pros/{id}/force-remove`` and ``/reassign``: every refusal, KYC
  never copied (Option A), the concurrency lock, one audit row each;
- the deactivate retrofit writes its audit row;
- ``GET /admin/actions`` and its filters; every route is admin-only.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin_action import AdminAction
from app.models.booking import BookingStatus
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.provider_team_invite import InviteStatus, ProviderTeamInvite
from app.models.user import User
from app.services.admin_team_service import AdminTeamService
from app.services.provider_service import ProviderService
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_self_pro_toggle import _switched_off_sp
from tests.test_two_level_dispatch import _team_pro, _team_sp

A = "/api/v1/admin"


@pytest.fixture
async def admin(db_session):
    return await UserFactory.create(db_session, role="admin", name="Asha Admin")


@pytest.fixture
def as_admin(auth_headers, admin):
    return auth_headers(str(admin.id), "admin")


async def _owner(db, service=None, *, name=None, phone=None, business=None):
    service = service or await _service(db)
    sp = await _eligible_provider(db, service)
    if name or phone:
        if name:
            sp.name = name
        if phone:
            sp.phone = phone
    if business:
        profile = await db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == sp.id))
        profile.business_name = business
    await ProviderService(db).ensure_self_professional(sp.id)
    await db.flush()
    return sp


async def _job(db, service, sp, pro, status):
    _, job = await _booking(db, service=service, provider_id=sp.id, status=status)
    job.professional_id = pro.id
    await db.flush()
    return job


async def _actions(db, **where) -> list[AdminAction]:
    """Audit rows matching ``where``. Always filtered: endpoint commits are
    real in this harness, so other tests' rows are in the table too."""
    assert where
    stmt = select(AdminAction)
    for column, value in where.items():
        stmt = stmt.where(getattr(AdminAction, column) == value)
    return list((await db.execute(stmt)).scalars().all())


async def _actions_on(db, ids) -> list[AdminAction]:
    stmt = select(AdminAction).where(AdminAction.target_id.in_(list(ids)))
    return list((await db.execute(stmt)).scalars().all())


# --- Search -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_matches_phone_name_and_business(client, db_session, as_admin):
    # Unique per run: endpoint commits are real here, so other tests' owners
    # are in the table too.
    tag = uuid.uuid4().hex[:8]
    digits = f"98{uuid.uuid4().int % 10**8:08d}"
    service = await _service(db_session)
    sparkle = await _owner(
        db_session, service, name=f"Ravi {tag}", phone=f"+91{digits}", business=f"Sparkle{tag}"
    )
    other = await _owner(db_session, service, business=f"Fixit{tag}")
    # A consumer with a matching phone is not an owner.
    await UserFactory.create(db_session, role="consumer", name=f"Ravi {tag}")

    async def ids(q):
        resp = await client.get(f"{A}/providers", params={"q": q}, headers=as_admin)
        assert resp.status_code == 200, resp.text
        return {row["user_id"] for row in resp.json()}

    spaced = f"+91 {digits[:5]} {digits[5:]}"
    for q in (digits, f"91{digits}", spaced, f"({digits[:5]})-{digits[5:]}", f"RAVI {tag}"):
        assert await ids(q) == {str(sparkle.id)}, q
    assert await ids(f"sparkle{tag}") == {str(sparkle.id)}
    assert await ids(f"fixit{tag}") == {str(other.id)}
    assert await ids(tag) == {str(sparkle.id), str(other.id)}


@pytest.mark.asyncio
async def test_search_escapes_like_wildcards(client, db_session, as_admin):
    """Unescaped, ``%_x`` would also match ``ab_x`` (any chars, any char, x)."""
    tag = uuid.uuid4().hex[:8]
    literal = await _owner(db_session, business=f"{tag}%_x")
    await _owner(db_session, business=f"{tag}ab_x")

    resp = await client.get(f"{A}/providers", params={"q": f"{tag}%_x"}, headers=as_admin)

    assert [r["user_id"] for r in resp.json()] == [str(literal.id)]


@pytest.mark.asyncio
async def test_search_row_counts_active_team_pros(client, db_session, as_admin):
    service = await _service(db_session)
    sp, _, _ = await _team_sp(db_session, service)
    await _team_pro(db_session, sp.id, name="Gone", active=False)
    await _team_pro(db_session, sp.id, name="Second")

    resp = await client.get(f"{A}/providers", params={"q": sp.phone}, headers=as_admin)

    [row] = resp.json()
    assert row["active_pro_count"] == 2  # Pro-for-self and the removed one not counted
    assert (row["user_is_active"], row["kyc_status"]) == (True, "verified")


@pytest.mark.asyncio
async def test_search_needs_two_characters(client, db_session, as_admin):
    resp = await client.get(f"{A}/providers", params={"q": "a"}, headers=as_admin)
    assert resp.status_code == 422


def _count_statements():
    executed: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    return executed, _record


@pytest.mark.asyncio
async def test_list_reads_do_not_grow_with_size(client, db_session, as_admin):
    """One statement per page for search and team, whatever the counts."""
    service = await _service(db_session)
    small, _, _ = await _team_sp(db_session, service)
    big, _, _ = await _team_sp(db_session, service)
    for i in range(15):
        pro = await _team_pro(db_session, big.id, name=f"Pro{i}")
        await _job(db_session, service, big, pro, BookingStatus.ACCEPTED)

    async def statements(url, **params):
        executed, record = _count_statements()
        event.listen(Engine, "before_cursor_execute", record)
        try:
            resp = await client.get(url, params=params, headers=as_admin)
        finally:
            event.remove(Engine, "before_cursor_execute", record)
        assert resp.status_code == 200, resp.text
        return len(executed)

    assert await statements(f"{A}/providers/{small.id}/team") == await statements(
        f"{A}/providers/{big.id}/team"
    )
    assert await statements(f"{A}/providers", per_page=1) == await statements(
        f"{A}/providers", per_page=200
    )


# --- Team, invites, Pro detail ------------------------------------------------------


@pytest.mark.asyncio
async def test_team_lists_removed_pros_and_job_counts(client, db_session, as_admin):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    gone = await _team_pro(db_session, sp.id, name="Gone", active=False)
    await _job(db_session, service, sp, member, BookingStatus.EN_ROUTE)
    await _job(db_session, service, sp, member, BookingStatus.COMPLETED)

    resp = await client.get(f"{A}/providers/{sp.id}/team", headers=as_admin)

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["id"] for r in rows] == [str(self_pro.id), str(member.id), str(gone.id)]
    by_id = {r["id"]: r for r in rows}
    assert by_id[str(member.id)]["active_job_count"] == 1
    assert by_id[str(gone.id)]["is_active"] is False
    assert by_id[str(self_pro.id)]["is_self"] is True
    member_user = await db_session.get(User, member.user_id)
    assert by_id[str(member.id)]["phone"] == member_user.phone


@pytest.mark.asyncio
async def test_team_and_invites_404_for_a_non_owner(client, db_session, as_admin):
    consumer = await UserFactory.create(db_session, role="consumer")
    for path in ("team", "invites"):
        resp = await client.get(f"{A}/providers/{consumer.id}/{path}", headers=as_admin)
        assert resp.status_code == 404, path
        assert resp.json()["code"] == "SP_NOT_FOUND"
    # And it didn't make them an SP as a side effect.
    assert (
        await db_session.scalar(
            select(ProviderProfile).where(ProviderProfile.user_id == consumer.id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_invites_show_every_status_without_codes(client, db_session, as_admin):
    sp = await _owner(db_session)
    now = datetime.now(UTC)
    for i, (status, expires) in enumerate(
        [
            (InviteStatus.PENDING, now + timedelta(days=3)),
            (InviteStatus.PENDING, now - timedelta(minutes=1)),  # stale
            (InviteStatus.REVOKED, now + timedelta(days=3)),
            (InviteStatus.ACCEPTED, now + timedelta(days=3)),
        ]
    ):
        db_session.add(
            ProviderTeamInvite(
                invited_by=sp.id,
                invitee_phone=f"+9198{uuid.uuid4().int % 10**8:08d}",
                token=f"{i:06d}",
                status=status.value,
                expires_at=expires,
            )
        )
    await db_session.flush()

    resp = await client.get(f"{A}/providers/{sp.id}/invites", headers=as_admin)

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert sorted(r["status"] for r in rows) == ["ACCEPTED", "EXPIRED", "PENDING", "REVOKED"]
    assert all("token" not in r and "invite_url" not in r for r in rows)


@pytest.mark.asyncio
async def test_pro_detail_names_the_owner(client, db_session, as_admin):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    profile = await db_session.scalar(
        select(ProviderProfile).where(ProviderProfile.user_id == sp.id)
    )
    profile.business_name = "Sparkle"
    await db_session.flush()

    resp = await client.get(f"{A}/pros/{member.id}", headers=as_admin)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(member.id)
    assert body["service_provider"] == {
        "user_id": str(sp.id),
        "name": sp.name,
        "phone": sp.phone,
        "business_name": "Sparkle",
    }
    resp = await client.get(f"{A}/pros/{uuid.uuid4()}", headers=as_admin)
    assert (resp.status_code, resp.json()["code"]) == (404, "PRO_NOT_FOUND")


# --- Force-remove -------------------------------------------------------------------


def _force_remove(client, headers, pro_id, reason="Owner unresponsive; left in Sept"):
    body = {} if reason is None else {"reason": reason}
    return client.post(f"{A}/pros/{pro_id}/force-remove", json=body, headers=headers)


@pytest.mark.asyncio
async def test_force_remove_soft_removes_and_audits(client, db_session, as_admin, admin):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)

    resp = await _force_remove(client, as_admin, member.id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"professional_id": str(member.id), "self_reenabled": False}
    await db_session.refresh(member)
    assert (member.is_active, member.is_online) == (False, False)
    [row] = await _actions(db_session, target_id=member.id)
    assert (row.action, row.target_type, row.admin_user_id, row.sp_user_id) == (
        "pro_force_removed",
        "professional",
        admin.id,
        sp.id,
    )
    assert row.reason == "Owner unresponsive; left in Sept"
    assert row.details == {
        "sp_user_id": str(sp.id),
        "user_id": str(member.user_id),
        "self_reenabled": False,
    }


@pytest.mark.asyncio
async def test_force_remove_reports_self_reenabled(client, db_session, auth_headers, as_admin):
    service = await _service(db_session)
    sp, self_pro, member = await _switched_off_sp(client, auth_headers, db_session, service)

    resp = await _force_remove(client, as_admin, member.id)

    assert resp.json()["self_reenabled"] is True
    await db_session.refresh(self_pro)
    assert self_pro.accepts_direct_jobs is True


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "", "ok"], ids=["missing", "empty", "too-short"])
async def test_force_remove_requires_a_reason(client, db_session, as_admin, reason):
    service = await _service(db_session)
    _, _, member = await _team_sp(db_session, service)
    resp = await _force_remove(client, as_admin, member.id, reason=reason)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_force_remove_refusals_write_nothing(client, db_session, as_admin):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    busy = await _team_pro(db_session, sp.id, name="Busy")
    await _job(db_session, service, sp, busy, BookingStatus.IN_PROGRESS)
    gone = await _team_pro(db_session, sp.id, name="Gone", active=False)

    cases = [
        (self_pro.id, 400, "CANNOT_REMOVE_SELF"),
        (busy.id, 409, "PRO_HAS_ACTIVE_JOB"),
        (gone.id, 404, "PRO_NOT_FOUND"),
        (uuid.uuid4(), 404, "PRO_NOT_FOUND"),
    ]
    for pro_id, code, error in cases:
        resp = await _force_remove(client, as_admin, pro_id)
        assert (resp.status_code, resp.json()["code"]) == (code, error), pro_id
    assert await _actions_on(db_session, [c[0] for c in cases]) == []


# --- Reassign -----------------------------------------------------------------------


def _reassign(client, headers, pro_id, target_id, reason="Sparkle merged into Shine"):
    return client.post(
        f"{A}/pros/{pro_id}/reassign",
        json={"target_sp_user_id": str(target_id), "reason": reason},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_reassign_to_a_new_owner_starts_kyc_pending(client, db_session, as_admin, admin):
    service = await _service(db_session)
    a, _, member = await _team_sp(db_session, service)
    b = await _owner(db_session, service)
    assert member.kyc_status is KYCStatus.VERIFIED

    resp = await _reassign(client, as_admin, member.id, b.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["old_professional_id"], body["reactivated"]) == (str(member.id), False)
    new = body["professional"]
    assert (new["service_provider_id"], new["user_id"]) == (str(b.id), str(member.user_id))
    # Option A: KYC is never carried across owners.
    assert (new["kyc_status"], new["is_active"], new["is_online"]) == ("pending", True, False)
    await db_session.refresh(member)
    assert member.is_active is False

    [row] = await _actions(db_session, target_id=member.id)
    assert (row.action, row.sp_user_id, row.to_sp_user_id, row.admin_user_id) == (
        "pro_reassigned",
        a.id,
        b.id,
        admin.id,
    )
    assert row.details["to_professional_id"] == new["id"]
    assert row.details["kyc_status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("old_kyc", [KYCStatus.VERIFIED, KYCStatus.REJECTED])
async def test_reassign_back_reactivates_the_old_row_with_its_own_kyc(
    client, db_session, as_admin, old_kyc
):
    service = await _service(db_session)
    a, _, member = await _team_sp(db_session, service)
    b = await _owner(db_session, service)
    # The person used to be on B's team, then moved to A.
    old = Professional(
        service_provider_id=b.id,
        user_id=member.user_id,
        display_name="Rohan",
        is_active=False,
        kyc_status=old_kyc,
    )
    db_session.add(old)
    await db_session.flush()

    resp = await _reassign(client, as_admin, member.id, b.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reactivated"] is True
    assert body["professional"]["id"] == str(old.id)
    assert body["professional"]["kyc_status"] == old_kyc.value
    rows = (
        (
            await db_session.execute(
                select(Professional).where(Professional.user_id == member.user_id)
            )
        )
        .scalars()
        .all()
    )
    assert [r.id for r in rows if r.is_active] == [old.id]


@pytest.mark.asyncio
async def test_reassign_refusals_write_nothing(client, db_session, as_admin):
    service = await _service(db_session)
    a, a_self, member = await _team_sp(db_session, service)
    b = await _owner(db_session, service)
    unverified = await _owner(db_session, service)
    unverified_profile = await db_session.scalar(
        select(ProviderProfile).where(ProviderProfile.user_id == unverified.id)
    )
    unverified_profile.kyc_status = KYCStatus.SUBMITTED
    inactive_sp = await _owner(db_session, service)
    inactive_sp.is_active = False
    consumer = await UserFactory.create(db_session, role="consumer")
    busy = await _team_pro(db_session, a.id, name="Busy")
    await _job(db_session, service, a, busy, BookingStatus.ACCEPTED)
    gone = await _team_pro(db_session, a.id, name="Gone", active=False)
    deactivated = await _team_pro(db_session, a.id, name="Deactivated")
    (await db_session.get(User, deactivated.user_id)).is_active = False
    await db_session.flush()

    cases = [
        (a_self.id, b.id, 400, "CANNOT_REASSIGN_SELF"),
        (member.id, a.id, 400, "SAME_SERVICE_PROVIDER"),
        (member.id, consumer.id, 404, "TARGET_SP_NOT_FOUND"),
        (member.id, uuid.uuid4(), 404, "TARGET_SP_NOT_FOUND"),
        (member.id, inactive_sp.id, 409, "TARGET_SP_INACTIVE"),
        (member.id, unverified.id, 409, "TARGET_SP_KYC_INCOMPLETE"),
        (busy.id, b.id, 409, "PRO_HAS_ACTIVE_JOB"),
        (gone.id, b.id, 404, "PRO_NOT_FOUND"),
        (uuid.uuid4(), b.id, 404, "PRO_NOT_FOUND"),
        (deactivated.id, b.id, 409, "USER_INACTIVE"),
    ]
    for pro_id, target, code, error in cases:
        resp = await _reassign(client, as_admin, pro_id, target)
        assert (resp.status_code, resp.json().get("code")) == (code, error), error
    await db_session.refresh(member)
    assert member.is_active is True
    assert await _actions_on(db_session, [c[0] for c in cases]) == []


@pytest.mark.asyncio
async def test_reassign_refuses_a_person_already_active_under_the_target(
    client, db_session, as_admin
):
    """Can't happen through the app (one active row per person), so set up
    directly: the check is the backstop after the user-row lock."""
    service = await _service(db_session)
    _, _, member = await _team_sp(db_session, service)
    b = await _owner(db_session, service)
    db_session.add(
        Professional(service_provider_id=b.id, user_id=member.user_id, display_name="Twin")
    )
    await db_session.flush()

    resp = await _reassign(client, as_admin, member.id, b.id)

    assert (resp.status_code, resp.json()["code"]) == (409, "ALREADY_ON_TEAM")


@pytest.mark.asyncio
async def test_reassign_reason_is_required(client, db_session, as_admin):
    service = await _service(db_session)
    _, _, member = await _team_sp(db_session, service)
    b = await _owner(db_session, service)
    resp = await client.post(
        f"{A}/pros/{member.id}/reassign",
        json={"target_sp_user_id": str(b.id)},
        headers=as_admin,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_two_admins_reassigning_at_once_produce_one_move(db_session):
    """Real concurrency on committed data: the first holds the person's user
    row lock until the second has started. The second then finds the source
    row removed (404), so the person ends with exactly one active row."""
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict = {}
    try:
        async with sessions() as setup:
            service = await _service(setup)
            admin = await UserFactory.create(setup, role="admin")
            a, _, member = await _team_sp(setup, service)
            b = await _owner(setup, service)
            c = await _owner(setup, service)
            await setup.commit()
            ids = {"admin": admin.id, "pro": member.id, "user": member.user_id}
            ids["targets"] = (b.id, c.id)

        first_holds_lock = asyncio.Event()

        async def run(target, *, first):
            if not first:
                await first_holds_lock.wait()
            async with sessions() as s:
                try:
                    await AdminTeamService(s).reassign(ids["admin"], ids["pro"], target, "merger")
                    if first:
                        first_holds_lock.set()
                        await asyncio.sleep(0.5)
                    await s.commit()
                    return "moved"
                except Exception as exc:
                    first_holds_lock.set()
                    await s.rollback()
                    return exc

        b_id, c_id = ids["targets"]
        results = await asyncio.wait_for(
            asyncio.gather(run(b_id, first=True), run(c_id, first=False)), timeout=30
        )

        assert results[0] == "moved", results
        assert results[1].status_code == 404 and results[1].code == "PRO_NOT_FOUND"
        async with sessions() as check:
            active = (
                (
                    await check.execute(
                        select(Professional).where(
                            Professional.user_id == ids["user"], Professional.is_active.is_(True)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [p.service_provider_id for p in active] == [b_id]
            audits = await check.scalars(
                select(AdminAction).where(AdminAction.target_id == ids["pro"])
            )
            assert len(audits.all()) == 1
    finally:
        if ids:
            async with sessions() as cleanup:
                await cleanup.execute(
                    delete(AdminAction).where(AdminAction.target_id == ids["pro"])
                )
                await cleanup.execute(
                    text("DELETE FROM professionals WHERE user_id = :u AND id <> :p"),
                    {"u": str(ids["user"]), "p": str(ids["pro"])},
                )
                await cleanup.commit()
        await engine.dispose()


# --- Deactivate audit ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_deactivating_a_team_pro_audits_against_their_old_owner(
    client, db_session, as_admin, admin
):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)

    resp = await client.patch(
        f"{A}/users/{member.user_id}/deactivate",
        json={"reason": "Fraud report #12"},
        headers=as_admin,
    )

    assert resp.status_code == 204, resp.text
    [row] = await _actions(db_session, target_id=member.user_id)
    assert (row.action, row.target_type, row.admin_user_id, row.sp_user_id) == (
        "user_deactivated",
        "user",
        admin.id,
        sp.id,
    )
    assert row.reason == "Fraud report #12"
    assert row.details == {
        "removed_professional_ids": [str(member.id)],
        "self_reenabled_sp_ids": [],
    }


@pytest.mark.asyncio
async def test_deactivating_an_owner_with_no_body_audits_against_themselves(
    client, db_session, as_admin
):
    sp = await _owner(db_session)

    resp = await client.patch(f"{A}/users/{sp.id}/deactivate", headers=as_admin)

    assert resp.status_code == 204, resp.text
    [row] = await _actions(db_session, target_id=sp.id)
    assert (row.sp_user_id, row.reason) == (sp.id, None)


@pytest.mark.asyncio
async def test_a_refused_or_unknown_deactivation_writes_nothing(client, db_session, as_admin):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    await _job(db_session, service, sp, member, BookingStatus.ACCEPTED)

    assert (
        await client.patch(f"{A}/users/{member.user_id}/deactivate", headers=as_admin)
    ).status_code == 409
    unknown = uuid.uuid4()
    assert (
        await client.patch(f"{A}/users/{unknown}/deactivate", headers=as_admin)
    ).status_code == 204
    assert await _actions_on(db_session, [member.user_id, unknown]) == []


# --- Audit list ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actions_filter_by_target_and_by_either_owner(client, db_session, as_admin):
    service = await _service(db_session)
    a, _, moved = await _team_sp(db_session, service)
    removed = await _team_pro(db_session, a.id, name="Removed")
    b = await _owner(db_session, service)
    bystander, _, other = await _team_sp(db_session, service)

    assert (await _force_remove(client, as_admin, removed.id)).status_code == 200
    assert (await _reassign(client, as_admin, moved.id, b.id)).status_code == 200
    assert (await _force_remove(client, as_admin, other.id)).status_code == 200

    async def actions(**params):
        resp = await client.get(f"{A}/actions", params=params, headers=as_admin)
        assert resp.status_code == 200, resp.text
        return [(r["action"], r["target_id"]) for r in resp.json()]

    assert await actions(sp_user_id=str(a.id)) == [
        ("pro_reassigned", str(moved.id)),
        ("pro_force_removed", str(removed.id)),
    ]
    # The move shows in the receiving owner's history too.
    assert await actions(sp_user_id=str(b.id)) == [("pro_reassigned", str(moved.id))]
    assert await actions(target_type="professional", target_id=str(other.id)) == [
        ("pro_force_removed", str(other.id))
    ]
    # Unfiltered, newest first (other tests' rows may be in the table too).
    assert (await actions(per_page=1))[0] == ("pro_force_removed", str(other.id))
    assert await actions(sp_user_id=str(a.id), per_page=1, page=2) == [
        ("pro_force_removed", str(removed.id))
    ]

    [row] = (
        await client.get(f"{A}/actions", params={"sp_user_id": str(b.id)}, headers=as_admin)
    ).json()
    assert row["admin_name"] == "Asha Admin"
    assert row["to_sp_user_id"] == str(b.id)


# --- RBAC ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["consumer", "provider"])
async def test_every_admin_team_route_is_admin_only(client, db_session, auth_headers, role):
    user = await UserFactory.create(db_session, role=role)
    headers = auth_headers(str(user.id), role)
    some = uuid.uuid4()
    calls = [
        client.get(f"{A}/providers", headers=headers),
        client.get(f"{A}/providers/{some}/team", headers=headers),
        client.get(f"{A}/providers/{some}/invites", headers=headers),
        client.get(f"{A}/pros/{some}", headers=headers),
        client.post(f"{A}/pros/{some}/force-remove", json={"reason": "xyz"}, headers=headers),
        client.post(
            f"{A}/pros/{some}/reassign",
            json={"target_sp_user_id": str(some), "reason": "xyz"},
            headers=headers,
        ),
        client.get(f"{A}/actions", headers=headers),
    ]
    for call in calls:
        assert (await call).status_code == 403
