"""
SVC-329 (1/2): two live team bugs.

- Removing a Pro who holds an active job is refused (409 ``PRO_HAS_ACTIVE_JOB``),
  on the owner's ``DELETE /providers/me/team/{id}``, and a concurrent
  ``/assign`` can't slip a job onto a Pro being removed;
- a deactivated user never gets or takes a job: broadcast eligibility (all four
  statements), the SVC-326 assignable-team-Pro rule, the pick list, ``/assign``
  and a decline's "anyone else on the team?" all skip them;
- ``PATCH /admin/users/{id}/deactivate`` refuses while the person holds a job
  (409 ``USER_HAS_ACTIVE_JOBS`` with the booking ids), and otherwise takes them
  offline everywhere and off any team they're a Pro on.
"""

import asyncio

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.booking import Booking, BookingStatus
from app.models.professional import Professional
from app.models.provider_profile import ProviderProfile
from app.models.user import User
from app.services.booking_service import BookingService
from app.services.team_service import TeamService
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _booking, _eligible_provider, _service
from tests.test_self_pro_toggle import TEAM, _cart, _offered, _switched_off_sp
from tests.test_two_level_dispatch import (
    ACCEPT,
    ASSIGN,
    DECLINE,
    PICK,
    _as,
    _team_pro,
    _team_sp,
    pushes,  # noqa: F401  (fixture)
)

ACTIVE = [BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS]


async def _job(db, service, sp, pro, status):
    _, job = await _booking(db, service=service, provider_id=sp.id, status=status)
    job.professional_id = pro.id
    await db.flush()
    return job


async def _deactivate_user(db, user_id):
    user = await db.get(User, user_id)
    user.is_active = False
    await db.flush()


# --- Bug 1: removing a Pro on a job ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", ACTIVE, ids=lambda s: s.value)
async def test_removing_a_pro_on_an_active_job_is_409(client, db_session, auth_headers, job_status):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    await _job(db_session, service, sp, member, job_status)

    resp = await client.delete(f"{TEAM}/{member.id}", headers=_as(auth_headers, sp))

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "PRO_HAS_ACTIVE_JOB"
    await db_session.refresh(member)
    assert member.is_active is True


@pytest.mark.asyncio
async def test_a_finished_job_does_not_block_removal(client, db_session, auth_headers):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    for finished in (BookingStatus.COMPLETED, BookingStatus.CANCELLED):
        await _job(db_session, service, sp, member, finished)

    resp = await client.delete(f"{TEAM}/{member.id}", headers=_as(auth_headers, sp))

    assert resp.status_code == 200, resp.text
    await db_session.refresh(member)
    assert member.is_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize("assign_first", [True, False], ids=["assign-first", "remove-first"])
async def test_assign_and_remove_at_once_never_strand_a_job(db_session, assign_first):
    """Real concurrency on committed data. Whoever takes the Pro row first
    holds it for a moment, so the other genuinely waits. Assign first: the
    removal then sees the job (409). Remove first: the assign then sees a
    removed Pro (404). Never both."""
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict = {}
    try:
        async with sessions() as setup:
            service = await _service(setup)
            sp, _, member = await _team_sp(setup, service)
            _, booking = await _booking(setup, service=service)
            await setup.commit()
            ids = {"booking": booking.id, "sp": sp.id, "pro": member.id}

        first_holds_lock = asyncio.Event()

        async def assign():
            if not assign_first:
                await first_holds_lock.wait()
            async with sessions() as s:
                try:
                    await BookingService(s).assign(ids["booking"], ids["sp"], ids["pro"])
                    if assign_first:
                        first_holds_lock.set()
                        await asyncio.sleep(0.5)
                    await s.commit()
                    return "assigned"
                except Exception as exc:
                    first_holds_lock.set()
                    await s.rollback()
                    return exc

        async def remove():
            if assign_first:
                await first_holds_lock.wait()
            async with sessions() as s:
                try:
                    await TeamService(s).remove_pro(ids["sp"], ids["pro"])
                    if not assign_first:
                        first_holds_lock.set()
                        await asyncio.sleep(0.5)
                    await s.commit()
                    return "removed"
                except Exception as exc:
                    first_holds_lock.set()
                    await s.rollback()
                    return exc

        assigned, removed = await asyncio.wait_for(asyncio.gather(assign(), remove()), timeout=30)

        if assign_first:
            assert assigned == "assigned"
            assert removed.status_code == 409 and removed.code == "PRO_HAS_ACTIVE_JOB"
        else:
            assert removed == "removed"
            assert assigned.status_code == 404 and assigned.code == "PRO_NOT_FOUND"

        async with sessions() as check:
            final = await check.get(Booking, ids["booking"])
            pro = await check.get(Professional, ids["pro"])
            # The invariant: an active job never points at a removed Pro.
            assert not (final.professional_id == pro.id and not pro.is_active)
    finally:
        if ids:
            async with sessions() as cleanup:
                for table in ("booking_assignment_events", "booking_status_logs"):
                    await cleanup.execute(
                        text(f"DELETE FROM {table} WHERE booking_id = :b"),
                        {"b": str(ids["booking"])},
                    )
                await cleanup.execute(delete(Booking).where(Booking.id == ids["booking"]))
                await cleanup.commit()
        await engine.dispose()


# --- Bug 2a: a deactivated user never gets or takes a job --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True], ids=["single-service", "cart"])
async def test_a_deactivated_owner_is_not_offered_jobs(db_session, multi):
    """``_offered`` checks the broadcast set and the membership test together;
    single-service and cart cover all four eligibility statements."""
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    service_ids = await _cart(db_session, sp, service, multi)
    assert await _offered(db_session, service_ids, sp) is True

    # Online and still holding a push token: only is_active says no.
    await _deactivate_user(db_session, sp.id)

    assert await _offered(db_session, service_ids, sp) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("multi", [False, True], ids=["single-service", "cart"])
async def test_a_deactivated_team_pro_does_not_keep_a_switched_off_sp_offered(
    client, db_session, auth_headers, multi
):
    service = await _service(db_session)
    sp, _, pro = await _switched_off_sp(client, auth_headers, db_session, service)
    service_ids = await _cart(db_session, sp, service, multi)
    assert await _offered(db_session, service_ids, sp) is True

    # Still an active, online, verified Pro row; only the person is deactivated.
    await _deactivate_user(db_session, pro.user_id)

    assert await _offered(db_session, service_ids, sp) is False
    assert (await TeamService(db_session).get_self_pro(sp.id))[1] is False


@pytest.mark.asyncio
async def test_the_pick_list_leaves_out_a_deactivated_pro(
    client,
    db_session,
    auth_headers,
    pushes,  # noqa: F811
):
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    gone = await _team_pro(db_session, sp.id, name="Gone")
    await _deactivate_user(db_session, gone.user_id)
    _, booking = await _booking(db_session, service=service)

    resp = await client.patch(ACCEPT.format(booking.id), headers=_as(auth_headers, sp, **PICK))

    assert resp.status_code == 202, resp.text
    assert [p["id"] for p in resp.json()["pros"]] == [str(self_pro.id), str(member.id)]


@pytest.mark.asyncio
async def test_assigning_a_deactivated_pro_is_404(
    client,
    db_session,
    auth_headers,
    pushes,  # noqa: F811
):
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    await _deactivate_user(db_session, member.user_id)
    _, booking = await _booking(db_session, service=service)

    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "PRO_NOT_FOUND"
    await db_session.refresh(booking)
    assert booking.status is BookingStatus.REQUESTED


@pytest.mark.asyncio
async def test_a_decline_reopens_when_the_only_other_pro_is_deactivated(
    client,
    db_session,
    auth_headers,
    pushes,  # noqa: F811
):
    """No Pro-for-self row (the fixture provider has none), so the deactivated
    teammate is the only "someone else". Waiting for a re-pick to them would
    strand the job; the booking goes back out instead."""
    service = await _service(db_session)
    sp = await _eligible_provider(db_session, service)
    member = await _team_pro(db_session, sp.id)
    gone = await _team_pro(db_session, sp.id, name="Gone")
    _, booking = await _booking(db_session, service=service)
    resp = await client.post(
        ASSIGN.format(booking.id),
        json={"professional_id": str(member.id)},
        headers=_as(auth_headers, sp),
    )
    assert resp.status_code == 200, resp.text
    await _deactivate_user(db_session, gone.user_id)

    member_user = await db_session.get(User, member.user_id)
    resp = await client.post(
        DECLINE.format(booking.id), headers=_as(auth_headers, member_user, role="consumer")
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "reopened"


# --- Bug 2b: admin deactivation ----------------------------------------------------


def _deactivate(client, auth_headers, admin, user_id):
    return client.patch(
        f"/api/v1/admin/users/{user_id}/deactivate",
        headers=auth_headers(str(admin.id), "admin"),
    )


@pytest.mark.asyncio
async def test_deactivating_an_owner_with_active_bookings_is_409(client, db_session, auth_headers):
    admin = await UserFactory.create(db_session, role="admin")
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    on_member = await _job(db_session, service, sp, member, BookingStatus.EN_ROUTE)
    # Held by the SP with no Pro yet (a decline awaiting re-pick) counts too.
    _, unpicked = await _booking(
        db_session, service=service, provider_id=sp.id, status=BookingStatus.ACCEPTED
    )
    await _job(db_session, service, sp, self_pro, BookingStatus.COMPLETED)

    resp = await _deactivate(client, auth_headers, admin, sp.id)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "USER_HAS_ACTIVE_JOBS"
    assert set(body["details"]["booking_ids"]) == {str(on_member.id), str(unpicked.id)}
    user = await db_session.get(User, sp.id)
    await db_session.refresh(user)
    assert user.is_active is True


@pytest.mark.asyncio
async def test_deactivating_a_pro_on_a_job_is_409(client, db_session, auth_headers):
    admin = await UserFactory.create(db_session, role="admin")
    service = await _service(db_session)
    sp, _, member = await _team_sp(db_session, service)
    job = await _job(db_session, service, sp, member, BookingStatus.IN_PROGRESS)

    resp = await _deactivate(client, auth_headers, admin, member.user_id)

    assert resp.status_code == 409, resp.text
    assert resp.json()["details"] == {"booking_ids": [str(job.id)]}
    await db_session.refresh(member)
    assert (member.is_active, member.is_online) == (True, True)


@pytest.mark.asyncio
async def test_deactivating_a_team_pro_takes_them_off_the_team(client, db_session, auth_headers):
    """Through remove_pro, so the other SP's SVC-326 invariant holds: their
    last other Pro going switches the owner back on."""
    admin = await UserFactory.create(db_session, role="admin")
    service = await _service(db_session)
    sp, self_pro, member = await _switched_off_sp(client, auth_headers, db_session, service)
    # A finished job doesn't block.
    await _job(db_session, service, sp, member, BookingStatus.COMPLETED)

    resp = await _deactivate(client, auth_headers, admin, member.user_id)

    assert resp.status_code == 204, resp.text
    for row in (member, self_pro):
        await db_session.refresh(row)
    assert (member.is_active, member.is_online) == (False, False)
    assert self_pro.accepts_direct_jobs is True
    user = await db_session.get(User, member.user_id)
    await db_session.refresh(user)
    assert user.is_active is False


@pytest.mark.asyncio
async def test_deactivating_an_owner_takes_them_offline_and_leaves_the_team(
    client, db_session, auth_headers
):
    admin = await UserFactory.create(db_session, role="admin")
    service = await _service(db_session)
    sp, self_pro, member = await _team_sp(db_session, service)
    self_pro.is_online = True
    await db_session.flush()

    resp = await _deactivate(client, auth_headers, admin, sp.id)

    assert resp.status_code == 204, resp.text
    profile = await db_session.scalar(
        select(ProviderProfile).where(ProviderProfile.user_id == sp.id)
    )
    for row in (profile, self_pro, member):
        await db_session.refresh(row)
    assert profile.is_online is False
    # Their own Pro-for-self stays (history); offline. Their team is untouched.
    assert (self_pro.is_active, self_pro.is_online) == (True, False)
    assert (member.is_active, member.is_online) == (True, True)
