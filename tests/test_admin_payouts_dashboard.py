"""SVC-426: the admin payouts dashboard reads and the ops payout trigger.

Roles for every route live in ``test_admin_console_auth.py::ROUTE_ROLES``. Other
files commit payout rows for real, so the per-SP list is read across all pages and
narrowed to this test's own SPs, and CSV tests use 2099 days.
"""

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import and_, select

from app.models.address import Address
from app.models.admin_action import AdminAction
from app.models.booking import (
    BookingStatus,
    PaymentMethod,
    PaymentStatus,
    SettlementStatus,
)
from app.models.booking_status_log import BookingStatusLog
from app.models.payout_audit import PayoutAudit
from app.models.professional import Professional
from app.models.provider_ledger_entry import (
    LedgerEntryType,
    LedgerReferenceType,
    ProviderLedgerEntry,
)
from app.models.provider_profile import KYCStatus, PayoutMethod, ProviderProfile
from app.models.user import AdminRole
from app.services.ledger_service import credit_booking_completed
from app.services.payout_dashboard_service import CSV_COLUMNS
from app.services.payout_service import PayoutService
from app.utils.encryption import encrypt
from tests.factories.booking_factory import BookingFactory
from tests.factories.user_factory import UserFactory
from tests.test_admin_console_auth import _admin, _session

A = "/api/v1/admin"
BASE = datetime(2099, 6, 1, 6, 0, tzinfo=UTC)


async def _headers(db, auth_headers, role: str = "admin") -> dict:
    admin = await UserFactory.create(db, role="admin")
    admin.admin_role = AdminRole(role)
    await db.flush()
    return auth_headers(str(admin.id), "admin")


async def _sp(db, *, business_name=None, payout=True, name=None, kyc=KYCStatus.VERIFIED):
    sp = await UserFactory.create(db, role="provider", name=name or f"SP {uuid.uuid4().hex[:6]}")
    db.add(
        ProviderProfile(
            user_id=sp.id,
            business_name=business_name,
            payout_method=PayoutMethod.UPI if payout else None,
            payout_vpa_encrypted=encrypt("sp@upi") if payout else None,
            kyc_status=kyc,
        )
    )
    await db.flush()
    return sp


async def _job(
    db,
    sp,
    *,
    settlement=SettlementStatus.PENDING,
    price=100.0,
    commission=20.0,
    payout=80.0,
    tip="0",
    discount="0",
    completed_at=BASE,
    method=PaymentMethod.ONLINE,
    payment=PaymentStatus.PAID,
    status=BookingStatus.COMPLETED,
    reason=None,
    payout_id=None,
    pro=None,
    log=True,
):
    consumer = await UserFactory.create(db)
    addr = Address(user_id=consumer.id, label="home", full_address="Addr")
    db.add(addr)
    await db.flush()
    b = await BookingFactory.create(
        db,
        consumer_id=consumer.id,
        provider_id=sp.id,
        address_id=addr.id,
        status=status,
        payment_status=payment,
        payment_method=method,
        service_amount=price,
        platform_commission=commission,
        provider_payout=payout,
    )
    b.tip_amount = Decimal(tip)
    b.discount_amount = Decimal(discount)
    b.settlement_status = settlement
    b.settlement_failure_reason = reason
    b.settlement_payout_id = payout_id
    b.professional_id = pro.id if pro else None
    if log:
        db.add(
            BookingStatusLog(
                booking_id=b.id,
                from_status="in_progress",
                to_status="completed",
                created_at=completed_at,
            )
        )
    await db.flush()
    return b


async def _all_rows(client, headers, **params) -> list[dict]:
    rows, page = [], 1
    while True:
        r = await client.get(
            f"{A}/payouts/providers",
            headers=headers,
            params={**params, "page": page, "per_page": 100},
        )
        assert r.status_code == 200, r.text
        got = r.json()["providers"]
        if not got:
            return rows
        rows += got
        page += 1


def _mine(rows, *sps) -> dict:
    ids = {str(sp.id) for sp in sps}
    return {row["provider_id"]: row for row in rows if row["provider_id"] in ids}


# --- scope -------------------------------------------------------------------------


def test_scope_filter_is_owed_filter_without_its_pending_clause():
    """The pending bucket is _owed_filter; the scope must be the rest of it."""
    from app.models.booking import Booking

    owed = and_(*PayoutService._owed_filter())
    rebuilt = and_(
        *PayoutService._payout_scope_filter(),
        Booking.settlement_status == SettlementStatus.PENDING,
    )
    assert str(owed.compile(compile_kwargs={"literal_binds": True})) == str(
        rebuilt.compile(compile_kwargs={"literal_binds": True})
    )


# --- GET /admin/payouts/providers --------------------------------------------------


async def test_buckets_sums_and_exclusions(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers, "support")
    sp = await _sp(db_session, business_name="Sharma Services")
    await _job(db_session, sp, payout=80.0)
    await _job(db_session, sp, payout=80.07, tip="10.50")
    await _job(db_session, sp, settlement=SettlementStatus.PROCESSING, payout=50.0)
    await _job(db_session, sp, settlement=SettlementStatus.COMPLETED, payout=200.0, tip="5")
    await _job(
        db_session, sp, settlement=SettlementStatus.FAILED, payout=40.0, reason="Invalid IFSC"
    )
    await _job(db_session, sp, settlement=SettlementStatus.REVERSED, payout=30.0)
    # Out of scope: cash, unpaid, not completed.
    await _job(db_session, sp, method=PaymentMethod.CASH, payout=999.0)
    await _job(db_session, sp, payment=PaymentStatus.PENDING, payout=999.0)
    await _job(db_session, sp, status=BookingStatus.IN_PROGRESS, payout=999.0, log=False)

    row = _mine(await _all_rows(client, headers), sp)[str(sp.id)]
    assert row["business_name"] == "Sharma Services"
    assert (row["phone"], row["is_active"], row["payout_set"], row["kyc_status"]) == (
        sp.phone,
        True,
        True,
        "verified",
    )
    assert (row["pending_count"], row["pending_paise"]) == (2, 8000 + 8007 + 1050)
    assert (row["processing_count"], row["processing_paise"]) == (1, 5000)
    assert (row["completed_count"], row["completed_paise"]) == (1, 20500)
    assert (row["failed_count"], row["failed_paise"]) == (1, 4000)
    assert (row["reversed_count"], row["reversed_paise"]) == (1, 3000)
    assert row["earned_count"] == 6
    assert row["earned_paise"] == 17057 + 5000 + 20500 + 4000 + 3000
    assert row["latest_failure_reason"] == "Invalid IFSC"


async def test_an_sp_with_only_cash_or_unpaid_jobs_is_not_listed(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    cash = await _sp(db_session)
    await _job(db_session, cash, method=PaymentMethod.CASH)
    unpaid = await _sp(db_session)
    await _job(db_session, unpaid, payment=PaymentStatus.PENDING)
    assert _mine(await _all_rows(client, headers), cash, unpaid) == {}


async def test_pending_matches_pending_settlements_and_payable_matches_estimate(
    client, db_session, auth_headers
):
    headers = await _headers(db_session, auth_headers, "ops")
    sp = await _sp(db_session, payout=False)
    b1 = await _job(db_session, sp, payout=400.0)
    b2 = await _job(db_session, sp, payout=250.25, tip="20")
    b1.settled_at = b2.settled_at = BASE
    for b in (b1, b2):
        await credit_booking_completed(
            db_session,
            provider_id=sp.id,
            booking_id=b.id,
            amount_paise=int((b.provider_payout + b.tip_amount) * 100),
        )
    # A cash job's commission nets against the wallet, not the booking sum.
    cash = await _job(db_session, sp, method=PaymentMethod.CASH)
    db_session.add(
        ProviderLedgerEntry(
            provider_id=sp.id,
            amount_paise=-2000,
            entry_type=LedgerEntryType.CASH_COMMISSION,
            reference_type=LedgerReferenceType.BOOKING,
            reference_id=cash.id,
        )
    )
    await db_session.flush()

    row = _mine(await _all_rows(client, headers), sp)[str(sp.id)]
    r = await client.get(f"{A}/providers/pending-settlements", headers=headers)
    [legacy] = [p for p in r.json()["providers"] if p["provider_id"] == str(sp.id)]
    estimate = await PayoutService(db_session).estimate_pending_payout(sp.id)

    assert (row["pending_count"], row["pending_paise"]) == (
        legacy["pending_booking_count"],
        legacy["pending_payout_paise"],
    )
    assert row["pending_count"] == estimate["booking_count"]
    assert row["payable_paise"] == estimate["amount_paise"] == 40000 + 27025 - 2000
    assert row["pending_paise"] == 67025
    assert row["payout_set"] is False


async def test_sorted_by_pending_then_id_and_filtered_by_status(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    big = await _sp(db_session)
    await _job(db_session, big, payout=900.0)
    small = await _sp(db_session)
    await _job(db_session, small, payout=100.0)
    failed_only = await _sp(db_session)
    await _job(db_session, failed_only, settlement=SettlementStatus.FAILED, payout=300.0)
    tie_a, tie_b = sorted([await _sp(db_session), await _sp(db_session)], key=lambda u: u.id)
    for sp in (tie_b, tie_a):
        await _job(db_session, sp, payout=555.0)

    rows = await _all_rows(client, headers)
    keys = [(-row["pending_paise"], uuid.UUID(row["provider_id"])) for row in rows]
    assert keys == sorted(keys)
    order = [
        r["provider_id"]
        for r in rows
        if r["provider_id"] in _mine(rows, big, small, tie_a, tie_b, failed_only)
    ]
    assert order == [str(u.id) for u in (big, tie_a, tie_b, small, failed_only)]

    failed = await _all_rows(client, headers, status="failed")
    assert set(_mine(failed, big, small, failed_only)) == {str(failed_only.id)}
    assert all(row["failed_count"] > 0 for row in failed)

    r = await client.get(f"{A}/payouts/providers", headers=headers, params={"status": "failed"})
    body = r.json()
    assert body["total"] == len(failed)
    assert (body["page"], body["per_page"]) == (1, 25)

    # Past the last page: no rows, but the true total.
    r = await client.get(
        f"{A}/payouts/providers", headers=headers, params={"status": "failed", "page": 9999}
    )
    assert r.json()["providers"] == []
    assert r.json()["total"] == len(failed)

    assert (
        await client.get(f"{A}/payouts/providers", headers=headers, params={"status": "x"})
    ).status_code == 422
    assert (
        await client.get(f"{A}/payouts/providers", headers=headers, params={"per_page": 101})
    ).status_code == 422


async def test_failure_reason_falls_back_to_a_failed_latest_attempt(
    client, db_session, auth_headers
):
    headers = await _headers(db_session, auth_headers)
    # Latest attempt failed, no failed booking: the attempt's error shows.
    kyc_hold = await _sp(db_session)
    await _job(db_session, kyc_hold)
    # A failed attempt, then a later good one: nothing shows.
    fixed = await _sp(db_session)
    await _job(db_session, fixed)
    # A failed booking beats a newer failed attempt.
    both = await _sp(db_session)
    await _job(
        db_session, both, settlement=SettlementStatus.REVERSED, reason="Beneficiary bank offline"
    )
    for sp, outcome, error, at in (
        (kyc_hold, "processed", None, BASE),
        (kyc_hold, "failed", "Provider KYC is not verified", BASE + timedelta(hours=1)),
        (fixed, "failed", "Old error", BASE),
        (fixed, "processed", None, BASE + timedelta(hours=1)),
        (both, "failed", "Attempt error", BASE + timedelta(days=1)),
    ):
        db_session.add(
            PayoutAudit(
                provider_id=sp.id,
                amount_paise=8000,
                booking_count=1,
                outcome=outcome,
                error=error,
                created_at=at,
            )
        )
    await db_session.flush()

    mine = _mine(await _all_rows(client, headers), kyc_hold, fixed, both)
    assert mine[str(kyc_hold.id)]["latest_failure_reason"] == "Provider KYC is not verified"
    assert mine[str(fixed.id)]["latest_failure_reason"] is None
    assert mine[str(both.id)]["latest_failure_reason"] == "Beneficiary bank offline"


# --- GET /admin/providers/{id}/payout-bookings ------------------------------------


async def test_payout_bookings_order_filter_and_split(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers, "support")
    sp = await _sp(db_session)
    rohan = Professional(
        service_provider_id=sp.id,
        user_id=(await UserFactory.create(db_session)).id,
        display_name="Rohan",
    )
    db_session.add(rohan)
    await db_session.flush()

    old = await _job(db_session, sp, completed_at=BASE, pro=rohan)
    new = await _job(
        db_session,
        sp,
        completed_at=BASE + timedelta(days=2),
        price=1499.0,
        commission=299.8,
        payout=1199.2,
        tip="50",
        discount="100",
        settlement=SettlementStatus.FAILED,
        reason="Account closed",
        payout_id="pout_X1",
    )
    mid = await _job(
        db_session, sp, completed_at=BASE + timedelta(days=1), settlement=SettlementStatus.COMPLETED
    )
    await _job(db_session, sp, method=PaymentMethod.CASH)
    other = await _sp(db_session)
    await _job(db_session, other)

    url = f"{A}/providers/{sp.id}/payout-bookings"
    body = (await client.get(url, headers=headers)).json()
    assert body["total"] == 3
    assert [b["booking_id"] for b in body["bookings"]] == [str(new.id), str(mid.id), str(old.id)]
    first = body["bookings"][0]
    assert first == {
        "booking_id": str(new.id),
        "completed_at": first["completed_at"],
        "service_amount_paise": 149900,
        "discount_amount_paise": 10000,
        "platform_commission_paise": 29980,
        "provider_payout_paise": 119920,
        "tip_amount_paise": 5000,
        "settlement_status": "failed",
        "settlement_payout_id": "pout_X1",
        "settlement_failure_reason": "Account closed",
        "professional_name": None,
    }
    assert datetime.fromisoformat(first["completed_at"]) == BASE + timedelta(days=2)
    assert body["bookings"][2]["professional_name"] == "Rohan"

    r = await client.get(url, headers=headers, params={"settlement_status": "completed"})
    assert [b["booking_id"] for b in r.json()["bookings"]] == [str(mid.id)]
    assert r.json()["total"] == 1

    r = await client.get(url, headers=headers, params={"per_page": 1, "page": 2})
    assert [b["booking_id"] for b in r.json()["bookings"]] == [str(mid.id)]
    assert r.json()["total"] == 3


# --- GET /admin/payouts/export.csv -------------------------------------------------

EXPORT = f"{A}/payouts/export.csv"


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


async def test_csv_header_values_and_ist_day_edges(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers, "ops")
    # 2099-04-01 IST is 2099-03-31T18:30Z .. 2099-04-01T18:30Z.
    start = datetime(2099, 3, 31, 18, 30, tzinfo=UTC)
    named = await _sp(db_session, business_name="Sharma Services")
    plain = await _sp(db_session, name="Ravi Kumar")
    first = await _job(
        db_session,
        named,
        completed_at=start,
        price=1499.0,
        commission=299.8,
        payout=1199.2,
        tip="50",
        settlement=SettlementStatus.FAILED,
    )
    last = await _job(db_session, plain, completed_at=start + timedelta(days=1, seconds=-1))
    # Just outside the day on each side, a cash job, and one with no completion log.
    await _job(db_session, named, completed_at=start - timedelta(seconds=1))
    await _job(db_session, named, completed_at=start + timedelta(days=1))
    await _job(db_session, named, completed_at=start, method=PaymentMethod.CASH)
    await _job(db_session, named, log=False)

    r = await client.get(EXPORT, headers=headers, params={"from": "2099-04-01", "to": "2099-04-01"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == (
        'attachment; filename="payouts_2099-04-01_2099-04-01.csv"'
    )
    rows = _parse(r.text)
    assert (
        tuple(rows[0])
        == CSV_COLUMNS
        == (
            "sp_id",
            "sp_name",
            "booking_id",
            "locked_price",
            "commission",
            "sp_payout",
            "booking_completed_at",
            "payout_status",
        )
    )
    assert rows[1:] == [
        [
            str(named.id),
            "Sharma Services",
            str(first.id),
            "1499.00",
            "299.80",
            "1249.20",
            "2099-04-01T00:00:00+05:30",
            "failed",
        ],
        [
            str(plain.id),
            "Ravi Kumar",
            str(last.id),
            "100.00",
            "20.00",
            "80.00",
            "2099-04-01T23:59:59+05:30",
            "pending",
        ],
    ]


async def test_csv_neutralises_formula_cells(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)
    evil = await _sp(db_session, business_name='=HYPERLINK("http://x","y")')
    at_name = await _sp(db_session, name="@SUM(1)")
    for sp in (evil, at_name):
        await _job(db_session, sp, completed_at=datetime(2099, 5, 10, 6, tzinfo=UTC))

    r = await client.get(EXPORT, headers=headers, params={"from": "2099-05-10", "to": "2099-05-10"})
    names = {row[1] for row in _parse(r.text)[1:]}
    assert names == {'\'=HYPERLINK("http://x","y")', "'@SUM(1)"}


async def test_csv_empty_range_is_header_only(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers, "support")
    r = await client.get(EXPORT, headers=headers, params={"from": "2098-01-01", "to": "2098-01-31"})
    assert r.status_code == 200
    assert _parse(r.text) == [list(CSV_COLUMNS)]


async def test_csv_range_validation(client, db_session, auth_headers):
    headers = await _headers(db_session, auth_headers)

    async def status(**params):
        return (await client.get(EXPORT, headers=headers, params=params)).status_code

    assert await status(**{"from": "2098-02-02", "to": "2098-02-01"}) == 422
    # 366 days inclusive is allowed (a leap year); 367 is not.
    assert await status(**{"from": "2096-01-01", "to": "2096-12-31"}) == 200
    assert await status(**{"from": "2097-01-01", "to": "2098-01-02"}) == 422
    assert await status(**{"from": "2098-01-01"}) == 422
    assert await status(**{"to": "2098-01-01"}) == 422
    assert await status(**{"from": "2098-13-01", "to": "2098-12-01"}) == 422


# --- Ops can pay out, and it is audited ---------------------------------------------


async def test_ops_trigger_writes_payout_enqueue_with_ops_as_actor(client, db_session):
    ops = await _admin(db_session, AdminRole.OPS)
    # No bookings: the handler commits, and a committed owed booking would show up in
    # other files' pending-settlements tests. An empty batch is still a 202 + a row.
    sp = await _sp(db_session)

    fake_task = type("T", (), {"id": "task_ops"})()
    with patch(
        "app.tasks.payment_tasks.execute_provider_payout.delay", return_value=fake_task
    ) as delay:
        r = await client.post(f"{A}/providers/{sp.id}/payout", headers=_session(ops))
    assert r.status_code == 202, r.text
    assert r.json()["booking_count"] == 0
    delay.assert_called_once_with(str(sp.id), str(ops.id))

    [row] = (
        (await db_session.execute(select(AdminAction).where(AdminAction.admin_user_id == ops.id)))
        .scalars()
        .all()
    )
    assert (row.action, row.target_type, row.target_id, row.sp_user_id) == (
        "payout.enqueue",
        "provider",
        str(sp.id),
        sp.id,
    )
    assert row.details == {"amount_paise": 0, "booking_count": 0}


async def test_ops_can_requeue_failed_but_not_reversed(client, db_session):
    ops = await _admin(db_session, AdminRole.OPS)
    sp = await _sp(db_session)
    # Unpaid, so the requeued booking (committed by the handler) is never owed.
    unpaid = PaymentStatus.PENDING
    failed = await _job(db_session, sp, settlement=SettlementStatus.FAILED, payment=unpaid)
    await _job(db_session, sp, settlement=SettlementStatus.REVERSED, payment=unpaid)

    r = await client.post(f"{A}/providers/{sp.id}/payout/requeue-failed", headers=_session(ops))
    assert r.status_code == 200, r.text
    await db_session.refresh(failed)
    assert failed.settlement_status is SettlementStatus.PENDING

    r = await client.post(f"{A}/providers/{sp.id}/payout/requeue-reversed", headers=_session(ops))
    assert r.status_code == 403
