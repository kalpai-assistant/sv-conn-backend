"""SVC-436: the service-request quota and the spam ban.

Every test runs on real Postgres. The thresholds are boundary conditions, so each
gets a just-below / just-at pair. Time is the DB's ``now()``, which is the
transaction start. These tests read it once and place rows relative to it, so
the helper and the test agree on "now".
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.exceptions import ProviderNotFound
from app.models.category import Category
from app.models.provider_profile import ProviderProfile
from app.models.service_request import ServiceRequest, ServiceRequestStatus
from app.models.sp_request_ban import SpRequestBan
from app.models.user import User
from app.schemas.service_request import ServiceRequestResponse
from app.services.service_request_service import (
    REQUEST_QUOTA_EXCEEDED,
    SP_REQUEST_BANNED,
    ServiceRequestService,
)
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory

PENDING = ServiceRequestStatus.PENDING
APPROVED = ServiceRequestStatus.APPROVED
REJECTED = ServiceRequestStatus.REJECTED_WITH_REASON
RECOMMENDED = ServiceRequestStatus.REJECTED_WITH_RECOMMENDATION


async def _now(db: AsyncSession) -> datetime:
    return await db.scalar(select(func.now()))


async def _sp(db: AsyncSession) -> User:
    user = await UserFactory.create(db, role="provider")
    db.add(ProviderProfile(user_id=user.id))
    await db.flush()
    return user


async def _category(db: AsyncSession) -> Category:
    slug = f"svc436-{uuid.uuid4().hex[:10]}"
    category = Category(name=slug, slug=slug)
    db.add(category)
    await db.flush()
    return category


async def _requests(
    db: AsyncSession,
    sp: User,
    status: ServiceRequestStatus,
    n: int = 1,
    resolved_at: datetime | None = None,
) -> None:
    """``n`` requests in ``status``. A resolved one is resolved at ``resolved_at``
    (default: a minute before now, inside any window the test sets up)."""
    if status is not PENDING and resolved_at is None:
        resolved_at = await _now(db) - timedelta(minutes=1)
    category = await _category(db)
    for _ in range(n):
        db.add(
            ServiceRequest(
                sp_id=sp.id,
                proposed_name="Balcony grille cleaning",
                proposed_description="Clean and oil the balcony grille.",
                proposed_category_id=category.id,
                proposed_price=Decimal("499.00"),
                proposed_price_unit="PER_VISIT",
                status=status.value,
                resolved_at=None if status is PENDING else resolved_at,
            )
        )
    await db.flush()


async def _ban(
    db: AsyncSession,
    sp: User,
    banned_until: datetime,
    cleared_at: datetime | None = None,
) -> SpRequestBan:
    ban = SpRequestBan(
        sp_id=sp.id, banned_until=banned_until, reason="test ban", cleared_at=cleared_at
    )
    db.add(ban)
    await db.flush()
    return ban


async def _bans(db: AsyncSession, sp: User) -> list[SpRequestBan]:
    return list(
        (await db.execute(select(SpRequestBan).where(SpRequestBan.sp_id == sp.id))).scalars()
    )


# -- can_sp_submit_request ------------------------------------------------------


async def test_quota_allows_the_fifth_and_refuses_the_sixth(db_session):
    """FAILURE CAUGHT: an off-by-one in the pending quota (`>` for `>=`) lets an SP
    hold 6 PENDING requests, or refuses one at 4."""
    sp = await _sp(db_session)
    service = ServiceRequestService(db_session)

    await _requests(db_session, sp, PENDING, 4)
    assert await service.can_sp_submit_request(sp.id) == (True, None, None)

    await _requests(db_session, sp, PENDING, 1)
    assert await service.can_sp_submit_request(sp.id) == (False, REQUEST_QUOTA_EXCEEDED, None)


async def test_quota_counts_only_pending_requests(db_session):
    """FAILURE CAUGHT: the status filter dropped from the quota count, so an SP's
    resolved history fills the quota and they can never submit again."""
    sp = await _sp(db_session)
    await _requests(db_session, sp, PENDING, 4)
    await _requests(db_session, sp, APPROVED, 3)
    await _requests(db_session, sp, REJECTED, 1)

    assert (await ServiceRequestService(db_session).can_sp_submit_request(sp.id)).allowed


async def test_active_ban_refuses_with_banned_until_even_under_quota(db_session):
    """FAILURE CAUGHT: the quota checked before the ban, or the ban returned without
    ``banned_until``, which SA3's 423 body needs."""
    sp = await _sp(db_session)
    until = await _now(db_session) + timedelta(days=12)
    await _ban(db_session, sp, until)
    service = ServiceRequestService(db_session)

    assert await service.can_sp_submit_request(sp.id) == (False, SP_REQUEST_BANNED, until)

    # Over quota too: the ban still wins, since it's the reason that lasts longer.
    await _requests(db_session, sp, PENDING, 5)
    assert (await service.can_sp_submit_request(sp.id)).reason == SP_REQUEST_BANNED


async def test_cleared_or_expired_ban_lets_the_sp_submit(db_session):
    """FAILURE CAUGHT: "active" loses ``cleared_at IS NULL`` (an admin clear does
    nothing) or ``banned_until > now`` (a ban never ends)."""
    now = await _now(db_session)
    cleared = await _sp(db_session)
    await _ban(db_session, cleared, now + timedelta(days=20), cleared_at=now - timedelta(hours=1))
    expired = await _sp(db_session)
    await _ban(db_session, expired, now - timedelta(seconds=1))
    service = ServiceRequestService(db_session)

    assert await service.can_sp_submit_request(cleared.id) == (True, None, None)
    assert await service.can_sp_submit_request(expired.id) == (True, None, None)


async def test_another_sps_ban_does_not_block(db_session):
    """FAILURE CAUGHT: the ban lookup loses its sp_id filter, and one SP's ban
    blocks every SP."""
    now = await _now(db_session)
    banned, other = await _sp(db_session), await _sp(db_session)
    await _ban(db_session, banned, now + timedelta(days=30))

    assert (await ServiceRequestService(db_session).can_sp_submit_request(other.id)).allowed


async def test_helpers_refuse_a_user_who_is_not_an_sp(db_session):
    """FAILURE CAUGHT: a user with no provider_profiles row silently comes back as
    "allowed" (and takes no lock), instead of surfacing the caller's bug."""
    consumer = await UserFactory.create(db_session)
    service = ServiceRequestService(db_session)

    with pytest.raises(ProviderNotFound) as exc:
        await service.can_sp_submit_request(consumer.id)
    assert (exc.value.status_code, exc.value.code) == (404, "PROVIDER_NOT_FOUND")
    with pytest.raises(ProviderNotFound):
        await service.trigger_spam_check_on_rejection(consumer.id)


# -- trigger_spam_check_on_rejection: thresholds --------------------------------


async def test_tenth_rejection_bans(db_session):
    """FAILURE CAUGHT: the absolute threshold off by one, so 9 rejections ban or
    10 don't. The many approvals keep the rate rule out of it (9/29, 10/30)."""
    sp = await _sp(db_session)
    service = ServiceRequestService(db_session)
    await _requests(db_session, sp, APPROVED, 20)
    await _requests(db_session, sp, REJECTED, 5)
    await _requests(db_session, sp, RECOMMENDED, 4)

    assert await service.trigger_spam_check_on_rejection(sp.id) is None
    assert await _bans(db_session, sp) == []

    await _requests(db_session, sp, RECOMMENDED, 1)
    ban = await service.trigger_spam_check_on_rejection(sp.id)
    assert ban is not None
    assert ban.reason == "Spam detection: 10 rejected requests"
    assert await _bans(db_session, sp) == [ban]


async def test_rate_bans_above_seventy_percent_only(db_session):
    """FAILURE CAUGHT: `>=` for `>`, or float rounding, so exactly 70% (7 of 10)
    bans; or 5 of 7 (71%) doesn't ban."""
    service = ServiceRequestService(db_session)

    at_seventy = await _sp(db_session)
    await _requests(db_session, at_seventy, REJECTED, 7)
    await _requests(db_session, at_seventy, APPROVED, 3)
    assert await service.trigger_spam_check_on_rejection(at_seventy.id) is None

    over = await _sp(db_session)
    await _requests(db_session, over, REJECTED, 3)
    await _requests(db_session, over, RECOMMENDED, 2)
    await _requests(db_session, over, APPROVED, 2)
    ban = await service.trigger_spam_check_on_rejection(over.id)
    assert ban is not None
    assert ban.reason == "Spam detection: 5 of 7 resolved requests rejected"


async def test_rate_needs_five_resolved_and_ignores_pending(db_session):
    """FAILURE CAUGHT: PENDING counted as resolved. Then 4 rejections plus 1
    pending reads as 4/5 = 80% and bans an SP who has only 4 results."""
    sp = await _sp(db_session)
    await _requests(db_session, sp, REJECTED, 4)
    await _requests(db_session, sp, PENDING, 1)

    assert await ServiceRequestService(db_session).trigger_spam_check_on_rejection(sp.id) is None


# -- trigger_spam_check_on_rejection: fresh start after an ended ban ------------


async def test_cleared_ban_starts_a_fresh_window(db_session):
    """FAILURE CAUGHT: rejections from before an admin's clear still count, so the
    SP's very next rejection re-bans them."""
    now = await _now(db_session)
    sp = await _sp(db_session)
    await _requests(db_session, sp, REJECTED, 10, resolved_at=now - timedelta(days=3))
    await _ban(db_session, sp, now + timedelta(days=27), cleared_at=now - timedelta(days=1))
    await _requests(db_session, sp, REJECTED, 2, resolved_at=now - timedelta(hours=1))

    assert await ServiceRequestService(db_session).trigger_spam_check_on_rejection(sp.id) is None


async def test_expired_ban_starts_a_fresh_window(db_session):
    """FAILURE CAUGHT: rejections from before a ban ran out still count, so the SP
    is re-banned on the first rejection after the ban expires."""
    now = await _now(db_session)
    sp = await _sp(db_session)
    await _requests(db_session, sp, REJECTED, 10, resolved_at=now - timedelta(days=40))
    await _ban(db_session, sp, now - timedelta(days=5))
    await _requests(db_session, sp, REJECTED, 2, resolved_at=now - timedelta(hours=1))

    assert await ServiceRequestService(db_session).trigger_spam_check_on_rejection(sp.id) is None


async def test_clearing_an_already_expired_ban_does_not_drop_rejections(db_session):
    """FAILURE CAUGHT: the window start taken as COALESCE(cleared_at, banned_until).
    A clear made after the ban had already expired then moves the window later,
    and the rejections between the real end and the clear are lost."""
    now = await _now(db_session)
    sp = await _sp(db_session)
    await _ban(
        db_session, sp, banned_until=now - timedelta(days=10), cleared_at=now - timedelta(days=1)
    )
    # Between the ban's real end and the late clear: these must count.
    await _requests(db_session, sp, REJECTED, 2, resolved_at=now - timedelta(days=5))
    await _requests(db_session, sp, REJECTED, 3, resolved_at=now - timedelta(hours=1))

    ban = await ServiceRequestService(db_session).trigger_spam_check_on_rejection(sp.id)
    assert ban is not None
    assert ban.reason == "Spam detection: 5 of 5 resolved requests rejected"


# -- trigger_spam_check_on_rejection: writing the ban ---------------------------


async def test_new_ban_runs_thirty_days_from_db_now(db_session):
    """FAILURE CAUGHT: the wrong ban length, or a Python-clock "now" that drifts
    from the DB time every other comparison uses."""
    now = await _now(db_session)
    sp = await _sp(db_session)
    await _requests(db_session, sp, REJECTED, 5)

    ban = await ServiceRequestService(db_session).trigger_spam_check_on_rejection(sp.id)

    assert ban.banned_until == now + timedelta(days=30)
    assert ban.cleared_at is None and ban.cleared_by is None


async def test_active_ban_is_extended_not_duplicated(db_session):
    """FAILURE CAUGHT: a second ban row inserted while one is active (two active
    bans, history muddled), or a longer existing ban cut short instead of kept at
    the later of the two dates."""
    now = await _now(db_session)
    service = ServiceRequestService(db_session)

    short = await _sp(db_session)
    existing = await _ban(db_session, short, now + timedelta(days=5))
    await _requests(db_session, short, REJECTED, 5)
    ban = await service.trigger_spam_check_on_rejection(short.id)
    assert ban.id == existing.id
    assert ban.banned_until == now + timedelta(days=30)
    assert len(await _bans(db_session, short)) == 1

    long = await _sp(db_session)
    existing = await _ban(db_session, long, now + timedelta(days=40))
    await _requests(db_session, long, REJECTED, 5)
    ban = await service.trigger_spam_check_on_rejection(long.id)
    assert ban.id == existing.id
    assert ban.banned_until == now + timedelta(days=40)
    assert len(await _bans(db_session, long)) == 1


async def test_ended_ban_is_kept_and_a_new_row_added(db_session):
    """FAILURE CAUGHT: an ended ban reused (overwritten) for the next one, which
    loses the "banned, then cleared" history the ticket keeps bans for."""
    now = await _now(db_session)
    sp = await _sp(db_session)
    old = await _ban(db_session, sp, now + timedelta(days=20), cleared_at=now - timedelta(days=2))
    await _requests(db_session, sp, REJECTED, 5)

    ban = await ServiceRequestService(db_session).trigger_spam_check_on_rejection(sp.id)

    assert ban.id != old.id
    assert {b.id for b in await _bans(db_session, sp)} == {old.id, ban.id}
    assert old.cleared_at == now - timedelta(days=2)


# -- schema + DB constraints ---------------------------------------------------


async def test_db_checks_reject_unknown_status_and_price_unit(db_session):
    """FAILURE CAUGHT: the CHECKs missing from the model (create_all builds the
    test schema) or built against the wrong column name, which the DB would
    reject at CREATE time or never enforce."""
    sp = await _sp(db_session)
    category = await _category(db_session)

    for bad in ({"status": "REJECTED"}, {"proposed_price_unit": "PER_DAY"}):
        fields = {
            "sp_id": sp.id,
            "proposed_name": "x",
            "proposed_description": "x",
            "proposed_category_id": category.id,
            "proposed_price": Decimal("1.00"),
            "proposed_price_unit": "PER_ITEM",
            **bad,
        }
        async with db_session.begin_nested():
            db_session.add(ServiceRequest(**fields))
            with pytest.raises(IntegrityError, match="ck_service_requests_"):
                await db_session.flush()


async def test_response_sends_price_as_a_number(db_session):
    """FAILURE CAUGHT: Decimal in the response schema, which serialises as a JSON
    string and breaks typed clients (repo rule: money out is a number)."""
    sp = await _sp(db_session)
    await _requests(db_session, sp, PENDING)
    row = await db_session.scalar(select(ServiceRequest).where(ServiceRequest.sp_id == sp.id))

    body = ServiceRequestResponse.model_validate(row).model_dump(mode="json")

    assert body["proposed_price"] == 499.0
    assert body["status"] == "PENDING"


# -- the lock, for real --------------------------------------------------------


async def test_concurrent_submits_cannot_both_pass_the_quota(db_session):
    """FAILURE CAUGHT: the FOR UPDATE on provider_profiles removed. Two submits
    both read 4 PENDING, both pass, and the SP ends with 6.

    Runs on committed data with two of its own sessions, since a second
    connection can't see db_session's open transaction. Overlap is forced: A
    holds its transaction open, after inserting, until B has started. A plain
    gather runs the two end to end and passes with the lock removed.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    sp_id = category_id = None
    try:
        async with sessions() as setup:
            sp = await _sp(setup)
            await _requests(setup, sp, PENDING, 4)
            sp_id = sp.id
            category_id = await setup.scalar(
                select(ServiceRequest.proposed_category_id).where(ServiceRequest.sp_id == sp_id)
            )
            await setup.commit()

        a_inserted = asyncio.Event()
        b_started = asyncio.Event()

        async def submit(s: AsyncSession):
            check = await ServiceRequestService(s).can_sp_submit_request(sp_id)
            if check.allowed:
                s.add(
                    ServiceRequest(
                        sp_id=sp_id,
                        proposed_name="race",
                        proposed_description="race",
                        proposed_category_id=category_id,
                        proposed_price=Decimal("1.00"),
                        proposed_price_unit="PER_ITEM",
                    )
                )
                await s.flush()
            return check

        async def first():
            async with sessions() as s:
                check = await submit(s)
                a_inserted.set()
                await b_started.wait()
                # Give B time to reach the lock (and block on it).
                await asyncio.sleep(0.5)
                await s.commit()
                return check

        async def second():
            await a_inserted.wait()
            async with sessions() as s:
                b_started.set()
                check = await submit(s)
                await s.commit()
                return check

        a, b = await asyncio.wait_for(asyncio.gather(first(), second()), timeout=30)

        assert a == (True, None, None)
        assert b == (False, REQUEST_QUOTA_EXCEEDED, None)
        async with sessions() as check:
            pending = await check.scalar(
                select(func.count())
                .select_from(ServiceRequest)
                .where(ServiceRequest.sp_id == sp_id, ServiceRequest.status == "PENDING")
            )
        assert pending == 5
    finally:
        if sp_id is not None:
            async with sessions() as cleanup:
                await cleanup.execute(delete(ServiceRequest).where(ServiceRequest.sp_id == sp_id))
                await cleanup.execute(
                    delete(ProviderProfile).where(ProviderProfile.user_id == sp_id)
                )
                await cleanup.execute(delete(Category).where(Category.id == category_id))
                await cleanup.execute(delete(User).where(User.id == sp_id))
                await cleanup.commit()
        await engine.dispose()
