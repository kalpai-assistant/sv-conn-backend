"""
Per-Pro earnings for the Service Provider (SVC-323).

Covers GET /providers/me/earnings/by-pro, the ``professional_id`` filter and the
``professional`` field on GET /providers/me/earnings:
- Attribution by ``bookings.professional_id``: Pro-for-self, team Pros, and the
  "Not assigned" row for jobs that completed with no Pro
- The rows add up to the SP's /me/earnings (windows) and /me/earnings/summary
  (all) totals, for every range
- Removed Pros keep their history; zero-job Pros are listed with 0s
- Cash counts; refunded and cancelled don't
- IST calendar boundaries, with the clock frozen so the test never straddles one
- A booking with two COMPLETED log rows counts once (also on /me/earnings)
- The filter 404s on anyone else's Pro; another SP's bookings never leak in
- The statement count doesn't grow with team size
"""

import itertools
import os
import time
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.models.address import Address
from app.models.booking import BookingStatus, PaymentMethod, PaymentStatus
from app.models.booking_status_log import BookingStatusLog
from app.models.category import Category
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from app.services import provider_service
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

IST = timezone(timedelta(hours=5, minutes=30))
# Thursday 2026-10-08 10:00 IST. Week starts Monday 10-05, month on 10-01.
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=IST)
BASE = "/api/v1/providers/me/earnings"
RANGES = ("today", "week", "month")
# Team Pros' join dates, in call order (the Pro-for-self sorts first regardless).
JOINED = datetime(2026, 1, 1, tzinfo=UTC)
_join_order = itertools.count()


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin the clock the earnings windows read, so no test straddles a boundary."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(provider_service, "datetime", _Frozen)
    return NOW


def _ist(day: int, hour: int, minute: int, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=IST)


async def _service(db) -> Service:
    suffix = uuid.uuid4().hex[:8]
    category = Category(name=f"Repair {suffix}", slug=f"repair-{suffix}", is_active=True)
    db.add(category)
    await db.flush()
    service = Service(
        category_id=category.id,
        name=f"AC Service {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db.add(service)
    await db.flush()
    return service


async def _sp(db, name: str = "Owner"):
    """An SP with its Pro-for-self, as signup creates them."""
    user = await UserFactory.create(db, role="provider", name=name)
    db.add(ProviderProfile(user_id=user.id, kyc_status=KYCStatus.VERIFIED))
    await db.flush()
    self_pro = Professional(service_provider_id=user.id, user_id=user.id, display_name=name)
    db.add(self_pro)
    await db.flush()
    return user, self_pro


async def _pro(db, sp, name: str, *, active: bool = True) -> Professional:
    member = await UserFactory.create(db, role="provider", name=name)
    pro = Professional(
        service_provider_id=sp.id,
        user_id=member.id,
        display_name=name,
        profile_photo_url=f"https://cdn.example/{name}.jpg",
        is_active=active,
        # Pros join in their own transactions in real life. Here every row in
        # the test's one transaction would share now(), and the list's
        # join-date order would fall through to the random-UUID tiebreak.
        created_at=JOINED + timedelta(minutes=next(_join_order)),
    )
    db.add(pro)
    await db.flush()
    return pro


async def _job(
    db,
    sp,
    pro: Professional | None,
    *,
    payout: float,
    completed_at: datetime | None,
    status: BookingStatus = BookingStatus.COMPLETED,
    payment_status: PaymentStatus = PaymentStatus.PAID,
    payment_method: PaymentMethod = PaymentMethod.ONLINE,
    service: Service | None = None,
    extra_completed_at: datetime | None = None,
):
    consumer = await UserFactory.create(db, role="consumer")
    address = Address(user_id=consumer.id, label="home", full_address="x")
    db.add(address)
    await db.flush()
    service = service or await _service(db)
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=sp.id,
        service_id=service.id,
        address_id=address.id,
        status=status,
        payment_status=payment_status,
        payment_method=payment_method,
        provider_payout=payout,
    )
    booking.professional_id = pro.id if pro is not None else None
    for at in (completed_at, extra_completed_at):
        if at is None:
            continue
        log = BookingStatusLog(
            booking_id=booking.id,
            from_status=BookingStatus.IN_PROGRESS.value,
            to_status=BookingStatus.COMPLETED.value,
            changed_by=sp.id,
        )
        db.add(log)
        await db.flush()
        # server_default=func.now() ran on flush; rewrite to the instant wanted.
        log.created_at = at.astimezone(UTC)
    await db.flush()
    return booking


async def _get(client, auth_headers, sp, path: str, **params):
    resp = await client.get(path, params=params, headers=auth_headers(str(sp.id), "provider"))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(body) -> dict:
    return {row["display_name"]: row for row in body["pros"]}


async def _assert_reconciles(client, auth_headers, sp):
    """Per-Pro rows add up to the SP's own totals, for every range."""
    for range_ in (*RANGES, "all"):
        by_pro = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range=range_)
        assert by_pro["range"] == range_
        assert round(sum(r["payout"] for r in by_pro["pros"]), 2) == by_pro["total_payout"]
        assert sum(r["completed_count"] for r in by_pro["pros"]) == by_pro["completed_count"]
        if range_ == "all":
            summary = await _get(client, auth_headers, sp, f"{BASE}/summary")
            assert by_pro["total_payout"] == summary["lifetime_payout"], range_
            assert by_pro["completed_count"] == summary["completed_count"], range_
        else:
            window = await _get(client, auth_headers, sp, BASE, range=range_)
            assert by_pro["total_payout"] == window["total_payout"], range_
            assert by_pro["completed_count"] == window["completed_count"], range_


@pytest.mark.asyncio
async def test_attribution_self_team_removed_idle_and_not_assigned(
    client, auth_headers, db_session, frozen_now
):
    sp, self_pro = await _sp(db_session, "Tush")
    rohan = await _pro(db_session, sp, "Rohan")
    gone = await _pro(db_session, sp, "Gone", active=False)
    await _pro(db_session, sp, "Idle")
    today = _ist(8, 9, 0)
    await _job(db_session, sp, self_pro, payout=100, completed_at=today)
    await _job(db_session, sp, rohan, payout=200, completed_at=today)
    await _job(db_session, sp, rohan, payout=50, completed_at=_ist(6, 12, 0))
    # A removed Pro keeps the jobs they did before leaving.
    await _job(db_session, sp, gone, payout=300, completed_at=_ist(6, 12, 0))
    # Declined, then the SP drove it themselves: no Pro on the booking.
    await _job(db_session, sp, None, payout=40, completed_at=today)

    body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range="week")

    assert body["total_payout"] == 690.0
    assert body["completed_count"] == 5
    # Self first, then active, then removed; "Not assigned" last.
    assert [r["display_name"] for r in body["pros"]] == [
        "Tush",
        "Rohan",
        "Idle",
        "Gone",
        "Not assigned",
    ]
    rows = _rows(body)
    assert rows["Tush"]["is_self"] is True
    assert rows["Tush"]["professional_id"] == str(self_pro.id)
    assert (rows["Tush"]["payout"], rows["Tush"]["completed_count"]) == (100.0, 1)
    assert (rows["Rohan"]["payout"], rows["Rohan"]["completed_count"]) == (250.0, 2)
    assert rows["Rohan"]["is_self"] is False
    assert rows["Rohan"]["profile_photo_url"] == "https://cdn.example/Rohan.jpg"
    assert rows["Gone"]["is_active"] is False
    assert (rows["Gone"]["payout"], rows["Gone"]["completed_count"]) == (300.0, 1)
    assert (rows["Idle"]["payout"], rows["Idle"]["completed_count"]) == (0.0, 0)
    assert rows["Not assigned"]["professional_id"] is None
    assert (rows["Not assigned"]["payout"], rows["Not assigned"]["completed_count"]) == (40.0, 1)

    today_body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range="today")
    assert _rows(today_body)["Rohan"]["completed_count"] == 1
    assert _rows(today_body)["Gone"]["completed_count"] == 0

    await _assert_reconciles(client, auth_headers, sp)


@pytest.mark.asyncio
async def test_no_not_assigned_row_without_such_jobs(client, auth_headers, db_session, frozen_now):
    sp, self_pro = await _sp(db_session)
    await _job(db_session, sp, self_pro, payout=80, completed_at=_ist(8, 9, 0))

    body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range="today")

    assert [r["professional_id"] for r in body["pros"]] == [str(self_pro.id)]


@pytest.mark.asyncio
async def test_ist_boundaries_for_every_range(client, auth_headers, db_session, frozen_now):
    """Each job sits 30 min either side of a window's IST start. Payouts are powers
    of two, so each total names exactly which jobs it took."""
    sp, self_pro = await _sp(db_session)
    for payout, at in (
        (1, _ist(8, 0, 30)),  # today 00:30 IST (= 7th 19:00 UTC)
        (2, _ist(7, 23, 30)),  # yesterday: week, not today
        (4, _ist(5, 0, 30)),  # Monday 00:30: this week
        (8, _ist(4, 23, 30)),  # Sunday 23:30: last week, this month
        (16, _ist(1, 0, 30)),  # the 1st 00:30: this month
        (32, _ist(30, 23, 30, month=9)),  # Sep 30 23:30: last month
    ):
        await _job(db_session, sp, self_pro, payout=payout, completed_at=at)

    expected = {"today": 1.0, "week": 7.0, "month": 31.0, "all": 63.0}
    for range_, total in expected.items():
        body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range=range_)
        assert body["total_payout"] == total, range_
    await _assert_reconciles(client, auth_headers, sp)


@pytest.mark.asyncio
async def test_cash_counts_refunded_and_cancelled_dont(
    client, auth_headers, db_session, frozen_now
):
    sp, self_pro = await _sp(db_session)
    pro = await _pro(db_session, sp, "Rohan")
    at = _ist(8, 9, 0)
    await _job(
        db_session,
        sp,
        pro,
        payout=70,
        completed_at=at,
        payment_method=PaymentMethod.CASH,
        payment_status=PaymentStatus.PENDING,
    )
    await _job(
        db_session, sp, pro, payout=500, completed_at=at, payment_status=PaymentStatus.REFUNDED
    )
    await _job(db_session, sp, pro, payout=900, completed_at=None, status=BookingStatus.CANCELLED)

    for range_ in ("today", "all"):
        body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range=range_)
        assert (_rows(body)["Rohan"]["payout"], _rows(body)["Rohan"]["completed_count"]) == (
            70.0,
            1,
        ), range_
    await _assert_reconciles(client, auth_headers, sp)


@pytest.mark.asyncio
async def test_two_completed_logs_count_once(client, auth_headers, db_session, frozen_now):
    """Fails on the pre-SVC-323 /me/earnings, which joined every COMPLETED log row
    and so listed and summed this booking twice."""
    sp, self_pro = await _sp(db_session)
    first, second = _ist(8, 8, 0), _ist(8, 9, 30)
    booking = await _job(
        db_session, sp, self_pro, payout=80, completed_at=first, extra_completed_at=second
    )

    window = await _get(client, auth_headers, sp, BASE, range="today")
    assert window["completed_count"] == 1
    assert window["total_payout"] == 80.0
    assert [b["booking_id"] for b in window["bookings"]] == [str(booking.id)]
    # Bucketed (and shown) at its first completion.
    assert datetime.fromisoformat(window["bookings"][0]["completed_at"]) == first

    body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range="today")
    assert (body["total_payout"], body["completed_count"]) == (80.0, 1)
    await _assert_reconciles(client, auth_headers, sp)


@pytest.mark.asyncio
async def test_first_completion_decides_the_window(client, auth_headers, db_session, frozen_now):
    """A second COMPLETED row today doesn't pull a job finished last month into today."""
    sp, self_pro = await _sp(db_session)
    await _job(
        db_session,
        sp,
        self_pro,
        payout=80,
        completed_at=_ist(30, 12, 0, month=9),
        extra_completed_at=_ist(8, 9, 0),
    )

    for range_ in RANGES:
        body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range=range_)
        assert body["completed_count"] == 0, range_
    body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range="all")
    assert body["completed_count"] == 1


@pytest.mark.asyncio
async def test_other_sp_never_leaks_in(client, auth_headers, db_session, frozen_now):
    sp, self_pro = await _sp(db_session, "Mine")
    other_sp, other_self = await _sp(db_session, "Theirs")
    other_pro = await _pro(db_session, other_sp, "TheirPro")
    at = _ist(8, 9, 0)
    await _job(db_session, sp, self_pro, payout=10, completed_at=at)
    await _job(db_session, other_sp, other_self, payout=1000, completed_at=at)
    await _job(db_session, other_sp, other_pro, payout=2000, completed_at=at)
    await _job(db_session, other_sp, None, payout=3000, completed_at=at)

    for range_ in (*RANGES, "all"):
        body = await _get(client, auth_headers, sp, f"{BASE}/by-pro", range=range_)
        assert (body["total_payout"], body["completed_count"]) == (10.0, 1), range_
        assert [r["display_name"] for r in body["pros"]] == ["Mine"]


@pytest.mark.asyncio
async def test_earnings_rows_name_the_pro(client, auth_headers, db_session, frozen_now):
    sp, self_pro = await _sp(db_session, "Tush")
    rohan = await _pro(db_session, sp, "Rohan")
    mine = await _job(db_session, sp, self_pro, payout=10, completed_at=_ist(8, 9, 0))
    theirs = await _job(db_session, sp, rohan, payout=20, completed_at=_ist(8, 8, 0))
    nobody = await _job(db_session, sp, None, payout=30, completed_at=_ist(8, 7, 0))

    body = await _get(client, auth_headers, sp, BASE, range="today")

    by_id = {b["booking_id"]: b["professional"] for b in body["bookings"]}
    assert by_id[str(mine.id)] == {
        "id": str(self_pro.id),
        "display_name": "Tush",
        "is_self": True,
    }
    assert by_id[str(theirs.id)] == {"id": str(rohan.id), "display_name": "Rohan", "is_self": False}
    assert by_id[str(nobody.id)] is None


@pytest.mark.asyncio
async def test_professional_id_filter(client, auth_headers, db_session, frozen_now):
    sp, self_pro = await _sp(db_session)
    rohan = await _pro(db_session, sp, "Rohan")
    gone = await _pro(db_session, sp, "Gone", active=False)
    at = _ist(8, 9, 0)
    await _job(db_session, sp, self_pro, payout=10, completed_at=at)
    r1 = await _job(db_session, sp, rohan, payout=20, completed_at=at)
    r2 = await _job(db_session, sp, rohan, payout=25, completed_at=_ist(8, 8, 0))
    g1 = await _job(db_session, sp, gone, payout=40, completed_at=at)
    await _job(db_session, sp, None, payout=30, completed_at=at)

    body = await _get(client, auth_headers, sp, BASE, range="today", professional_id=str(rohan.id))
    assert [b["booking_id"] for b in body["bookings"]] == [str(r1.id), str(r2.id)]
    assert (body["total_payout"], body["completed_count"]) == (45.0, 2)
    # The detail view agrees with the Pro's row in the split.
    split = _rows(await _get(client, auth_headers, sp, f"{BASE}/by-pro", range="today"))
    assert (split["Rohan"]["payout"], split["Rohan"]["completed_count"]) == (45.0, 2)

    # A removed Pro's history is still reachable.
    body = await _get(client, auth_headers, sp, BASE, range="today", professional_id=str(gone.id))
    assert [b["booking_id"] for b in body["bookings"]] == [str(g1.id)]

    # Without the param, unchanged: every job.
    body = await _get(client, auth_headers, sp, BASE, range="today")
    assert body["completed_count"] == 5


@pytest.mark.asyncio
async def test_professional_id_filter_404s_on_anyone_elses_pro(
    client, auth_headers, db_session, frozen_now
):
    sp, _ = await _sp(db_session)
    other_sp, other_self = await _sp(db_session, "Theirs")
    other_pro = await _pro(db_session, other_sp, "TheirPro")
    await _job(db_session, other_sp, other_pro, payout=99, completed_at=_ist(8, 9, 0))

    for pro_id in (other_pro.id, other_self.id, uuid.uuid4()):
        resp = await client.get(
            BASE,
            params={"range": "today", "professional_id": str(pro_id)},
            headers=auth_headers(str(sp.id), "provider"),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "PRO_NOT_FOUND"


@pytest.mark.asyncio
async def test_by_pro_rejects_consumer_and_bad_range(client, auth_headers, db_session):
    consumer = await UserFactory.create(db_session, role="consumer")
    resp = await client.get(f"{BASE}/by-pro", headers=auth_headers(str(consumer.id), "consumer"))
    assert resp.status_code == 403

    sp, _ = await _sp(db_session)
    resp = await client.get(
        f"{BASE}/by-pro",
        params={"range": "last30"},
        headers=auth_headers(str(sp.id), "provider"),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_by_pro_defaults_to_week(client, auth_headers, db_session, frozen_now):
    sp, _ = await _sp(db_session)
    body = await _get(client, auth_headers, sp, f"{BASE}/by-pro")
    assert body["range"] == "week"
    assert (body["total_payout"], body["completed_count"]) == (0.0, 0)


async def _statements_for(client, auth_headers, sp, range_: str) -> tuple[int, float]:
    executed: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        started = time.perf_counter()
        resp = await client.get(
            f"{BASE}/by-pro",
            params={"range": range_},
            headers=auth_headers(str(sp.id), "provider"),
        )
        elapsed = time.perf_counter() - started
    finally:
        event.remove(Engine, "before_cursor_execute", _record)
    assert resp.status_code == 200, resp.text
    return len(executed), elapsed


@pytest.mark.asyncio
async def test_statement_count_does_not_grow_with_team(
    client, auth_headers, db_session, frozen_now
):
    service = await _service(db_session)
    small_sp, small_self = await _sp(db_session, "Small")
    await _job(db_session, small_sp, small_self, payout=10, completed_at=_ist(8, 9, 0))

    big_sp, big_self = await _sp(db_session, "Big")
    pros = [big_self] + [await _pro(db_session, big_sp, f"Pro{i}") for i in range(49)]
    for i, pro in enumerate(pros):
        await _job(
            db_session, big_sp, pro, payout=10 + i, completed_at=_ist(8, 9, 0), service=service
        )

    for range_ in ("week", "all"):
        small_count, _ = await _statements_for(client, auth_headers, small_sp, range_)
        big_count, elapsed = await _statements_for(client, auth_headers, big_sp, range_)
        assert big_count == small_count, range_
        # Wall-clock is noisy on shared CI runners; the statement count above is
        # the deterministic guard. Locally, hold the ticket's 500 ms bar.
        if not os.environ.get("CI"):
            assert elapsed < 0.5, f"{range_}: {elapsed:.3f}s for 50 Pros"

    body = await _get(client, auth_headers, big_sp, f"{BASE}/by-pro", range="week")
    assert len(body["pros"]) == 50
    assert body["completed_count"] == 50
    await _assert_reconciles(client, auth_headers, big_sp)
