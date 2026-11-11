"""SVC-432: /admin/analytics/* math, the role gate, and the refresh task.

The views aggregate the whole database, and other test files commit rows for
real. So every test here puts its rows in 1999 (no other file does) and reads
them with a fixed clock (``NOW``), and the non-time-bucketed metrics (prices,
churn, cohorts, the gap matrix) use this test's own categories, services,
pincodes and SPs. Assertions are on this test's own ids or 1999 buckets, never
on global totals. Views are refreshed inside the test's transaction, so they see
its uncommitted rows, and the rollback at teardown takes the refresh back too.
"""

import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, text

from app.analytics import service as analytics_service
from app.analytics.service import AnalyticsService, refresh_views, week_starts
from app.analytics.views import CURRENT_VIEWS
from app.models.address import Address
from app.models.admin_action import AdminAction
from app.models.booking import BookingStatus, PaymentStatus
from app.models.booking_dispute import BookingDispute
from app.models.booking_status_log import BookingStatusLog
from app.models.category import Category
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from app.models.user import AdminRole
from tests.conftest import TEST_DATABASE_URL
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory

# Wednesday 1999-06-30, 12:00 IST.
NOW = datetime(1999, 6, 30, 6, 30, tzinfo=UTC)
TODAY = date(1999, 6, 30)
MONDAY = date(1999, 6, 28)


def _at(days_ago: float, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days_ago) + timedelta(hours=hours)


async def _consumer(db, signup_at: datetime | None = None):
    user = await UserFactory.create(db, role="consumer")
    user.created_at = signup_at or _at(300)
    await db.flush()
    return user


async def _sp(db, kyc=KYCStatus.VERIFIED, kyc_reviewed_at=None, signup_at=None):
    user = await UserFactory.create(db, role="provider")
    user.created_at = signup_at or _at(300)
    db.add(
        ProviderProfile(
            user_id=user.id,
            business_name=f"SP {uuid.uuid4().hex[:6]}",
            kyc_status=kyc,
            kyc_reviewed_at=kyc_reviewed_at,
        )
    )
    await db.flush()
    return user


async def _pro(db, sp, user=None, is_active=True):
    pro = Professional(
        service_provider_id=sp.id,
        user_id=(user or sp).id,
        display_name="Pro",
        is_active=is_active,
    )
    db.add(pro)
    await db.flush()
    return pro


async def _booking(
    db,
    consumer,
    created_at,
    status=BookingStatus.COMPLETED,
    provider=None,
    professional=None,
    pincode: str | None = "400001",
    **kw,
):
    addr = Address(user_id=consumer.id, label="home", full_address="Addr", pincode=pincode)
    db.add(addr)
    await db.flush()
    booking = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=provider.id if provider else None,
        address_id=addr.id,
        status=status,
        **kw,
    )
    booking.created_at = created_at
    if professional is not None:
        booking.professional_id = professional.id
    await db.flush()
    return booking


async def _accept_log(db, booking, at):
    db.add(
        BookingStatusLog(
            booking_id=booking.id, from_status="requested", to_status="accepted", created_at=at
        )
    )
    await db.flush()


async def _refund_dispute(db, booking, refund_status):
    db.add(
        BookingDispute(
            booking_id=booking.id,
            raised_by_user_id=booking.consumer_id,
            reason_code="QUALITY",
            description="x",
            status="RESOLVED" if refund_status == "FAILED" else "CLOSED",
            resolution_type="PARTIAL_REFUND",
            refund_amount=Decimal("50"),
            refund_channel="MANUAL" if refund_status is None else "RAZORPAY",
            refund_status=refund_status,
        )
    )
    await db.flush()


async def _category(db):
    cat = Category(name=f"Cat {uuid.uuid4()}", slug=f"cat-{uuid.uuid4()}", is_active=True)
    db.add(cat)
    await db.flush()
    return cat


async def _service(db, cat, name="Svc"):
    svc = Service(
        category_id=cat.id,
        name=f"{name} {uuid.uuid4().hex[:6]}",
        base_price=100,
        duration_minutes=60,
        is_active=True,
    )
    db.add(svc)
    await db.flush()
    return svc


def _pincode() -> str:
    return str(random.randint(900000, 999999))


async def _read(db, kind: str, weeks: int = 12):
    await refresh_views(db)
    svc = AnalyticsService(db)
    return await (svc.overview if kind == "overview" else svc.supply)(weeks=weeks, now=NOW)


# --- Overview -----------------------------------------------------------------


async def test_gmv_windows_previous_is_null_until_history_covers_it(db_session):
    db = db_session
    c = await _consumer(db)
    await _booking(db, c, _at(0), service_amount=1000, platform_commission=200)
    await _booking(db, c, _at(10), service_amount=500, platform_commission=100)
    # Not completed: no GMV.
    await _booking(db, c, _at(0), status=BookingStatus.CANCELLED, service_amount=700)

    body = await _read(db, "overview")
    w7, w30, w90 = body.gmv
    assert (w7.days, w7.gmv, w7.completed) == (7, 1000.0, 1)
    assert (w30.gmv, w30.completed) == (1500.0, 2)
    # First booking is 10 days ago: the previous 7 days (13..7 ago) aren't covered.
    assert w7.previous_gmv is None and w7.change_pct is None
    assert w30.previous_gmv is None and w90.previous_gmv is None

    # History reaching back 200 days covers every previous window.
    await _booking(db, c, _at(200), service_amount=300)
    body = await _read(db, "overview")
    w7, w30, w90 = body.gmv
    assert (w7.previous_gmv, w7.change_pct) == (500.0, 1.0)
    # The previous 30 days had no completed booking: 0, not null, and no % change.
    assert (w30.previous_gmv, w30.change_pct) == (0.0, None)


async def test_weekly_take_rate_and_aov(db_session):
    db = db_session
    c = await _consumer(db)
    await _booking(db, c, _at(1), service_amount=1000, platform_commission=200)
    await _booking(db, c, _at(2), service_amount=500, platform_commission=50)
    await _booking(db, c, _at(10), service_amount=400, platform_commission=40)
    # Tips and discounts don't move GMV (service_amount, before discount).
    b = await _booking(db, c, _at(10), status=BookingStatus.EXPIRED, service_amount=900)
    b.tip_amount = Decimal("99")
    await db.flush()

    body = await _read(db, "overview")
    by_week = {w.week_start: w for w in body.weekly}
    assert len(body.weekly) == 12 and body.weekly[-1].week_start == MONDAY
    cur = by_week[MONDAY]
    assert (cur.gmv, cur.completed, cur.commission) == (1500.0, 2, 250.0)
    assert cur.take_rate == round(250 / 1500, 4)
    assert cur.aov == 750.0
    prev = by_week[MONDAY - timedelta(weeks=2)]  # 10 days ago = Sunday 06-20
    assert (prev.gmv, prev.take_rate, prev.aov) == (400.0, 0.1, 400.0)
    empty = by_week[MONDAY - timedelta(weeks=5)]
    assert (empty.gmv, empty.take_rate, empty.aov) == (0.0, None, None)


async def test_daily_bookings_by_status_and_refunded(db_session):
    db = db_session
    c = await _consumer(db)
    for st in BookingStatus:
        await _booking(db, c, _at(0), status=st)
    cancelled_refunded = await _booking(
        db, c, _at(0), status=BookingStatus.CANCELLED, payment_status=PaymentStatus.REFUNDED
    )
    manual = await _booking(db, c, _at(0))
    await _refund_dispute(db, manual, refund_status=None)  # manual refund: NULL status
    failed = await _booking(db, c, _at(0))
    await _refund_dispute(db, failed, refund_status="FAILED")
    assert cancelled_refunded

    body = await _read(db, "overview")
    assert body.daily[-1].day == TODAY
    assert body.daily[0].day == MONDAY - timedelta(weeks=11)
    today = body.daily[-1]
    assert today.model_dump(exclude={"day"}) == {
        "requested": 1,
        "accepted": 1,
        "en_route": 1,
        "in_progress": 1,
        "completed": 3,
        "cancelled": 2,
        "expired": 1,
        "refunded": 2,
    }
    assert body.daily[-2].completed == 0


async def test_consumer_cohort_retention_and_immature_cells(db_session):
    db = db_session
    signup = _at(100)
    week = signup.date() - timedelta(days=signup.date().weekday())

    c1 = await _consumer(db, signup)
    await _booking(db, c1, _at(95))
    await _booking(db, c1, _at(90), status=BookingStatus.CANCELLED)  # +5 days
    c2 = await _consumer(db, signup)
    await _booking(db, c2, _at(95))
    await _booking(db, c2, _at(75), status=BookingStatus.EXPIRED)  # +20 days
    c3 = await _consumer(db, signup)
    await _booking(db, c3, _at(20))  # no rebook; only the 7-day window has elapsed
    c4 = await _consumer(db, signup)
    await _booking(db, c4, _at(50), status=BookingStatus.EXPIRED)  # never completed: not in
    # This week's cohort: nothing has matured yet.
    c5 = await _consumer(db, _at(1))
    await _booking(db, c5, _at(1))

    body = await _read(db, "overview", weeks=20)
    by_week = {c.signup_week: c for c in body.cohorts}
    old = by_week[week]
    assert old.size == 3
    assert old.d7.model_dump() == {"matured": 3, "rebooked": 1, "rate": round(1 / 3, 4)}
    assert old.d30.model_dump() == {"matured": 2, "rebooked": 2, "rate": 1.0}
    assert old.d90.model_dump() == {"matured": 2, "rebooked": 2, "rate": 1.0}
    new = by_week[MONDAY]
    assert new.size == 1
    for cell in (new.d7, new.d30, new.d90):
        assert cell.model_dump() == {"matured": 0, "rebooked": 0, "rate": None}


# --- Supply -------------------------------------------------------------------


async def test_active_sps_and_pros(db_session):
    db = db_session
    c = await _consumer(db)
    sp1 = await _sp(db)
    self_pro = await _pro(db, sp1)
    team_user = await UserFactory.create(db, role="consumer")
    gone_pro = await _pro(db, sp1, user=team_user, is_active=False)
    sp2 = await _sp(db)
    unverified = await _sp(db, kyc=KYCStatus.SUBMITTED)

    await _booking(db, c, _at(2), provider=sp1, professional=self_pro)  # Monday
    await _booking(db, c, _at(1), provider=sp1, professional=self_pro)
    await _booking(db, c, _at(20), provider=sp2)
    await _booking(db, c, _at(1), provider=unverified)
    await _booking(db, c, _at(1), provider=sp1, professional=gone_pro)

    active = (await _read(db, "supply")).active
    assert (active.sps_7d, active.sps_30d, active.pros_7d, active.pros_30d) == (1, 2, 1, 1)
    by_week = {w.week_start: w for w in active.weekly}
    assert (by_week[MONDAY].active_sps, by_week[MONDAY].active_pros) == (1, 1)
    w = by_week[MONDAY - timedelta(weeks=3)]  # 1999-06-07, holds the 20-days-ago booking
    assert (w.active_sps, w.active_pros) == (1, 0)


async def test_sp_cohort_retention_and_immature_cohort(db_session):
    db = db_session
    c = await _consumer(db)
    kyc_week = date(1999, 2, 1)  # a Monday, ~150 days before NOW
    ka = datetime(1999, 2, 1, 4, 30, tzinfo=UTC)
    kb = datetime(1999, 2, 3, 4, 30, tzinfo=UTC)
    sa = await _sp(db, kyc_reviewed_at=ka)
    sb = await _sp(db, kyc_reviewed_at=kb)
    await _booking(db, c, ka + timedelta(days=10), provider=sa)  # day 0-30
    await _booking(db, c, ka + timedelta(days=70), provider=sa)  # day 60-90
    await _booking(db, c, kb + timedelta(days=40), provider=sb)  # day 30-60
    # KYC'd 40 days ago: only the first 30-day window has elapsed.
    kc = _at(40)
    await _sp(db, kyc_reviewed_at=kc)
    # Not verified: not in any cohort.
    await _sp(db, kyc=KYCStatus.REJECTED, kyc_reviewed_at=ka)

    body = await _read(db, "supply", weeks=22)
    by_week = {x.kyc_week: x for x in body.sp_cohorts}
    old = by_week[kyc_week]
    assert (old.size, old.d30, old.d60, old.d90) == (2, 0.5, 0.5, 0.5)
    young = by_week[kc.date() - timedelta(days=kc.date().weekday())]
    assert (young.size, young.d30, young.d60, young.d90) == (1, 0.0, None, None)


async def test_sp_churn_by_signup_month(db_session):
    db = db_session
    c = await _consumer(db)
    jan = datetime(1999, 1, 10, 6, 0, tzinfo=UTC)
    feb = datetime(1999, 2, 5, 6, 0, tzinfo=UTC)
    busy = await _sp(db, signup_at=jan)
    await _booking(db, c, _at(3), provider=busy)
    await _sp(db, signup_at=jan + timedelta(days=2))  # never booked: churned
    idle = await _sp(db, signup_at=feb)
    await _booking(db, c, _at(20), provider=idle)  # last booking 20 days ago: churned
    await _sp(db, kyc=KYCStatus.PENDING, signup_at=jan)  # unverified: not counted

    churn = {m.signup_month: m for m in (await _read(db, "supply")).churn}
    assert churn["1999-01"].model_dump() == {
        "signup_month": "1999-01",
        "verified": 2,
        "churned": 1,
        "rate": 0.5,
    }
    assert (churn["1999-02"].verified, churn["1999-02"].churned) == (1, 1)


async def test_supply_gap_matrix(db_session):
    db = db_session
    c = await _consumer(db)
    cat = await _category(db)
    x, y = await _service(db, cat, "X"), await _service(db, cat, "Y")
    p1, p2 = _pincode(), _pincode()

    # Expired and never accepted: unmet.
    await _booking(db, c, _at(3), status=BookingStatus.EXPIRED, pincode=p1, service_id=x.id)
    # Accepted, reopened, then expired: demand but not unmet.
    reopened = await _booking(
        db, c, _at(3), status=BookingStatus.EXPIRED, pincode=p1, service_id=x.id
    )
    await _accept_log(db, reopened, _at(3, hours=1))
    # Two items: one demand per service.
    await _booking(db, c, _at(5), pincode=p1, item_service_ids=[x.id, y.id])
    # No pincode: the Unknown row.
    await _booking(db, c, _at(4), status=BookingStatus.EXPIRED, pincode=None, service_id=y.id)
    # Outside the 12 weeks.
    await _booking(db, c, _at(150), status=BookingStatus.EXPIRED, pincode=p2, service_id=x.id)

    gap = (await _read(db, "supply")).supply_gap
    cells = {(cell.pincode, cell.service_id): (cell.demand, cell.unmet) for cell in gap.cells}
    assert cells[(p1, x.id)] == (3, 1)
    assert cells[(p1, y.id)] == (1, 0)
    assert cells[("Unknown", y.id)] == (1, 1)
    assert p2 not in gap.pincodes
    assert gap.pincodes.index(p1) < gap.pincodes.index("Unknown")
    assert {s.id for s in gap.services} >= {x.id, y.id}


async def test_supply_gap_caps_pincodes_but_keeps_unknown(db_session):
    db = db_session
    c = await _consumer(db)
    svc = await _service(db, await _category(db))
    big, small = _pincode(), _pincode()
    for _ in range(3):
        await _booking(db, c, _at(2), status=BookingStatus.EXPIRED, pincode=big, service_id=svc.id)
    await _booking(db, c, _at(2), status=BookingStatus.EXPIRED, pincode=small, service_id=svc.id)
    await _booking(db, c, _at(2), status=BookingStatus.EXPIRED, pincode=None, service_id=svc.id)

    with patch.object(analytics_service, "GAP_MAX_PINCODES", 1):
        gap = (await _read(db, "supply")).supply_gap
    assert gap.pincodes == [big, "Unknown"]
    assert gap.truncated is True and gap.total_pincodes == 3
    assert {cell.pincode for cell in gap.cells} == {big, "Unknown"}


async def test_price_distribution_per_category(db_session):
    db = db_session
    c = await _consumer(db)
    cat = await _category(db)
    svc = await _service(db, cat)
    for price in (100, 200, 300, 400, 500):
        await _booking(db, c, _at(5), service_id=svc.id, service_amount=price)
    # Not completed: left out.
    await _booking(
        db, c, _at(5), status=BookingStatus.CANCELLED, service_id=svc.id, service_amount=9999
    )

    prices = {p.category_id: p for p in (await _read(db, "supply")).prices}
    p = prices[cat.id]
    assert (p.count, p.min, p.p25, p.median, p.p75, p.max) == (5, 100, 200, 300, 400, 500)


async def test_time_to_accept_uses_first_accept(db_session):
    db = db_session
    c = await _consumer(db)
    a = await _booking(db, c, _at(1), status=BookingStatus.ACCEPTED)
    await _accept_log(db, a, a.created_at + timedelta(seconds=60))
    # Reopened and accepted again: the first accept (120 s) counts.
    b = await _booking(db, c, _at(1), status=BookingStatus.COMPLETED)
    await _accept_log(db, b, b.created_at + timedelta(seconds=120))
    await _accept_log(db, b, b.created_at + timedelta(seconds=1000))
    # Never accepted: left out.
    await _booking(db, c, _at(1), status=BookingStatus.EXPIRED)

    tta = {w.week_start: w for w in (await _read(db, "supply")).time_to_accept}
    cur = tta[MONDAY]
    assert (cur.accepted, cur.p50_seconds, cur.p90_seconds) == (2, 90.0, 114.0)
    empty = tta[MONDAY - timedelta(weeks=4)]
    assert (empty.accepted, empty.p50_seconds, empty.p90_seconds) == (0, None, None)


def test_week_starts_are_ist_mondays():
    assert week_starts(TODAY, 3) == [date(1999, 6, 14), date(1999, 6, 21), MONDAY]


# --- API: role gate, as_of, no audit ------------------------------------------


async def _admin_headers(db, auth_headers, role):
    admin = await UserFactory.create(db, role="admin")
    admin.admin_role = AdminRole(role)
    await db.flush()
    return admin, auth_headers(str(admin.id), "admin")


@pytest.mark.parametrize("role", ["admin", "ops"])
@pytest.mark.parametrize("path", ["overview", "supply"])
async def test_admin_and_ops_read_without_audit_rows(client, db_session, auth_headers, role, path):
    admin, headers = await _admin_headers(db_session, auth_headers, role)
    await refresh_views(db_session)
    resp = await client.get(f"/api/v1/admin/analytics/{path}?weeks=4", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["as_of"]
    if path == "overview":
        assert len(body["weekly"]) == 4
        assert {w["days"] for w in body["gmv"]} == {7, 30, 90}
    else:
        assert len(body["time_to_accept"]) == 4
        assert set(body["supply_gap"]) == {
            "pincodes",
            "services",
            "cells",
            "truncated",
            "total_pincodes",
        }
    audited = await db_session.scalar(
        select(func.count()).select_from(AdminAction).where(AdminAction.admin_user_id == admin.id)
    )
    assert audited == 0


@pytest.mark.parametrize("path", ["overview", "supply"])
async def test_support_is_403(client, db_session, auth_headers, path):
    _, headers = await _admin_headers(db_session, auth_headers, "support")
    resp = await client.get(f"/api/v1/admin/analytics/{path}", headers=headers)
    assert resp.status_code == 403


@pytest.mark.parametrize("weeks", [0, 53])
async def test_weeks_out_of_range_is_422(client, db_session, auth_headers, weeks):
    _, headers = await _admin_headers(db_session, auth_headers, "admin")
    resp = await client.get(f"/api/v1/admin/analytics/overview?weeks={weeks}", headers=headers)
    assert resp.status_code == 422


# --- Refresh task -------------------------------------------------------------


async def test_refresh_task_refreshes_every_view(db_session):
    """The real task against the test DB (its SyncSessionLocal is the app DB
    otherwise). It commits, but a refresh only rewrites the views from committed
    data, so nothing leaks."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.tasks.analytics_tasks import refresh_analytics_views

    # Release the fixture's create_all connection so the refresh isn't blocked.
    await db_session.commit()

    engine = create_engine(TEST_DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    try:
        with engine.connect() as conn:
            before = conn.execute(text("SELECT refreshed_at FROM mv_analytics_meta")).scalar_one()
        with patch("app.database.SyncSessionLocal", sessionmaker(bind=engine)):
            refresh_analytics_views()
        with engine.connect() as conn:
            after = conn.execute(text("SELECT refreshed_at FROM mv_analytics_meta")).scalar_one()
            populated = conn.execute(
                text(
                    "SELECT count(*) FROM pg_matviews WHERE ispopulated AND matviewname = ANY(:n)"
                ),
                {"n": [v.name for v in CURRENT_VIEWS]},
            ).scalar_one()
    finally:
        engine.dispose()
    assert after > before
    assert populated == len(CURRENT_VIEWS)


def test_beat_refreshes_every_15_minutes():
    from app.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["refresh-analytics-views"]
    assert entry == {
        "task": "app.tasks.analytics_tasks.refresh_analytics_views",
        "schedule": 900.0,
    }
