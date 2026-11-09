"""
SVC-423: the admin audit log.

- Every listed write route writes exactly one row, and a new admin write route
  can't be added without a case here.
- Blocked fields can't be listed or saved. Rows are seeded with canary values, so
  a leak fails on the value as well as the key.
- A rolled-back action leaves no row. The ``client`` fixture skips ``get_db``'s
  commit and rollback, so those tests use ``real_tx_client``.
- Payout writes its row before enqueuing; the CI release token writes none.
- A bad IP or User-Agent never fails the action.
- GET /admin/audit, /audit/filters, and /admin/actions limited to the team actions.

Endpoint commits are real in this harness, so every assertion is scoped to this
test's own admin or target.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_redis
from app.exceptions import PayoutRefused
from app.main import create_app
from app.models.admin_action import (
    ADMIN_ACTION_TARGET_TYPES,
    ADMIN_ACTIONS,
    TEAM_ACTIONS,
    AdminAction,
)
from app.models.app_version import AppPlatform, AppVersion
from app.models.booking import Booking, BookingStatus, PaymentStatus, SettlementStatus
from app.models.category import Category
from app.models.coupon import Coupon
from app.models.pricing_config import PricingConfig
from app.models.professional import Professional
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from app.models.user import AdminRole, User
from app.services.admin_audit import (
    AUDIT_FIELDS,
    AuditActor,
    audit_actor,
    audit_admin_action,
    is_blocked_field,
    snapshot,
)
from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.test_accept_contract import _eligible_provider, _service
from tests.test_admin_console_auth import _admin, _session
from tests.test_admin_coupons import _coupon as _seed_coupon
from tests.test_admin_force_cancel import _seed_booking
from tests.test_admin_pricing import category as _pricing_category
from tests.test_admin_pricing import seed_config
from tests.test_two_level_dispatch import _team_pro, _team_sp

V1 = "/api/v1"
A = f"{V1}/admin"
IP = "203.0.113.9"
UA = "audit-test/1.0"
CANARY = "CANARY-SECRET"
OTP_CANARY = "C4NY"

# The ticket's "must never appear" list, per table.
BLOCKED_BY_MODEL = {
    User: ("password_hash", "totp_secret", "fcm_token"),
    ProviderProfile: (
        "razorpayx_contact_id",
        "razorpayx_fund_account_id",
        "kyc_docs",
        "kyc_ai_verdict",
    ),
    Professional: ("kyc_docs", "kyc_ai_verdict"),
    Booking: ("doorstep_otp",),
}
RECORD_MODELS = {
    "user": User,
    "provider_profile": ProviderProfile,
    "professional": Professional,
    "booking": Booking,
    "app_version": AppVersion,
    "service": Service,
    "category": Category,
    "pricing_config": PricingConfig,
    "coupon": Coupon,
}


def _headers(admin, **extra) -> dict:
    return {**_session(admin), "X-Forwarded-For": IP, "User-Agent": UA, **extra}


async def _rows(db, **where) -> list[AdminAction]:
    assert where
    stmt = select(AdminAction)
    for column, value in where.items():
        stmt = stmt.where(getattr(AdminAction, column) == value)
    return list((await db.execute(stmt)).scalars().all())


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def _assert_nothing_blocked(row: AdminAction) -> None:
    for payload in (row.before_json, row.after_json, row.details):
        assert not [k for k in _keys(payload) if is_blocked_field(k)], payload
        dumped = json.dumps(payload)
        assert CANARY not in dumped
        assert OTP_CANARY not in dumped


async def _plant_profile_canaries(db, user_id) -> None:
    profile = await db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == user_id))
    for column in ProviderProfile.__table__.columns.keys():
        if column.endswith("_encrypted"):
            setattr(profile, column, CANARY)
    profile.razorpayx_contact_id = CANARY
    profile.razorpayx_fund_account_id = CANARY
    profile.kyc_docs = {"aadhaar_front": CANARY}
    profile.kyc_ai_verdict = {"notes": CANARY}
    user = await db.get(User, user_id)
    user.fcm_token = f"{CANARY}-{uuid.uuid4().hex[:8]}"
    user.password_hash = CANARY
    user.totp_secret = CANARY
    await db.flush()


# ---------------------------------------------------------------------------
# One case per write route
# ---------------------------------------------------------------------------


@dataclass
class Case:
    url: str
    action: str
    target_type: str
    # None: the id comes back in the response (a create).
    target_id: object = None
    body: dict | None = None
    status: int = 200
    sp_user_id: uuid.UUID | None = None
    to_sp_user_id: uuid.UUID | None = None
    reason: str | None = None
    # Subsets of the saved JSON. None means SQL NULL.
    before: dict | None = None
    after: dict | None = None
    details: dict | None = None


async def _deactivate(db) -> Case:
    sp, _, member = await _team_sp(db, await _service(db))
    return Case(
        url=f"{A}/users/{member.user_id}/deactivate",
        body={"reason": "Fraud report"},
        status=204,
        action="user_deactivated",
        target_type="user",
        target_id=member.user_id,
        sp_user_id=sp.id,
        reason="Fraud report",
        before={"is_active": True},
        after={"is_active": False},
        details={"removed_professional_ids": [str(member.id)]},
    )


async def _force_remove(db) -> Case:
    sp, _, member = await _team_sp(db, await _service(db))
    member.kyc_docs = {"aadhaar_front": CANARY}
    member.kyc_ai_verdict = {"notes": CANARY}
    await db.flush()
    return Case(
        url=f"{A}/pros/{member.id}/force-remove",
        body={"reason": "Owner unresponsive"},
        action="pro_force_removed",
        target_type="professional",
        target_id=member.id,
        sp_user_id=sp.id,
        reason="Owner unresponsive",
        before={"is_active": True},
        after={"is_active": False},
    )


async def _reassign(db) -> Case:
    service = await _service(db)
    sp, _, member = await _team_sp(db, service)
    target = await _eligible_provider(db, service)
    return Case(
        url=f"{A}/pros/{member.id}/reassign",
        body={"target_sp_user_id": str(target.id), "reason": "Merger"},
        action="pro_reassigned",
        target_type="professional",
        target_id=member.id,
        sp_user_id=sp.id,
        to_sp_user_id=target.id,
        reason="Merger",
        before={"id": str(member.id), "service_provider_id": str(sp.id), "is_active": True},
        after={"service_provider_id": str(target.id), "is_active": True},
    )


async def _submitted_sp(db) -> User:
    sp = await _eligible_provider(db, await _service(db), kyc=KYCStatus.SUBMITTED)
    await _plant_profile_canaries(db, sp.id)
    return sp


async def _sp_kyc_verify(db) -> Case:
    sp = await _submitted_sp(db)
    return Case(
        url=f"{A}/providers/{sp.id}/kyc/verify",
        action="kyc.approve",
        target_type="provider",
        target_id=sp.id,
        sp_user_id=sp.id,
        before={"kyc_status": "submitted"},
        after={"kyc_status": "verified"},
    )


async def _sp_kyc_reject(db) -> Case:
    sp = await _submitted_sp(db)
    return Case(
        url=f"{A}/providers/{sp.id}/kyc/reject",
        body={"reason": "Aadhaar photo is blurry"},
        action="kyc.reject",
        target_type="provider",
        target_id=sp.id,
        sp_user_id=sp.id,
        reason="Aadhaar photo is blurry",
        before={"kyc_status": "submitted"},
        after={"kyc_status": "rejected", "kyc_rejection_reason": "Aadhaar photo is blurry"},
    )


async def _submitted_pro(db) -> tuple[User, Professional]:
    sp, _, _ = await _team_sp(db, await _service(db))
    pro = await _team_pro(db, sp.id, name="Meena", kyc=KYCStatus.SUBMITTED)
    pro.kyc_docs = {"aadhaar_front": CANARY}
    pro.kyc_ai_verdict = {"notes": CANARY}
    await db.flush()
    return sp, pro


async def _pro_kyc_verify(db) -> Case:
    sp, pro = await _submitted_pro(db)
    return Case(
        url=f"{A}/pros/{pro.id}/kyc/verify",
        action="kyc.approve",
        target_type="professional",
        target_id=pro.id,
        sp_user_id=sp.id,
        before={"kyc_status": "submitted"},
        after={"kyc_status": "verified"},
    )


async def _pro_kyc_reject(db) -> Case:
    sp, pro = await _submitted_pro(db)
    return Case(
        url=f"{A}/pros/{pro.id}/kyc/reject",
        body={"reason": "Selfie doesn't match"},
        action="kyc.reject",
        target_type="professional",
        target_id=pro.id,
        sp_user_id=sp.id,
        reason="Selfie doesn't match",
        before={"kyc_status": "submitted"},
        after={"kyc_status": "rejected"},
    )


async def _unpaid_booking(db, status=BookingStatus.ACCEPTED) -> tuple[User, Booking]:
    consumer = await UserFactory.create(db, role="consumer")
    provider = await UserFactory.create(db, role="provider")
    booking = await _seed_booking(
        db,
        consumer=consumer,
        provider=provider,
        status=status,
        payment_status=PaymentStatus.PENDING,
        with_captured_payment=False,
    )
    return provider, booking


async def _cancel(db) -> Case:
    provider, booking = await _unpaid_booking(db)
    booking.doorstep_otp = OTP_CANARY
    await db.flush()
    return Case(
        url=f"{A}/bookings/{booking.id}/cancel",
        body={"reason": "Duplicate booking"},
        action="booking.force_cancel",
        target_type="booking",
        target_id=booking.id,
        sp_user_id=provider.id,
        reason="Duplicate booking",
        before={"status": "accepted", "provider_id": str(provider.id)},
        after={"status": "cancelled", "cancellation_reason": "Duplicate booking"},
    )


async def _payout(db) -> Case:
    sp = await _eligible_provider(db, await _service(db))
    return Case(
        url=f"{A}/providers/{sp.id}/payout",
        status=202,
        action="payout.enqueue",
        target_type="provider",
        target_id=sp.id,
        sp_user_id=sp.id,
        details={"amount_paise": 16000, "booking_count": 2},
    )


async def _requeue(db, from_status: SettlementStatus, suffix: str, action: str) -> Case:
    provider, booking = await _unpaid_booking(db, status=BookingStatus.COMPLETED)
    booking.settlement_status = from_status
    await db.flush()
    return Case(
        url=f"{A}/providers/{provider.id}/payout/{suffix}",
        action=action,
        target_type="provider",
        target_id=provider.id,
        sp_user_id=provider.id,
        before={"settlement_status": from_status.value},
        after={"settlement_status": SettlementStatus.PENDING.value},
        details={"booking_count": 1},
    )


async def _requeue_failed(db) -> Case:
    return await _requeue(db, SettlementStatus.FAILED, "requeue-failed", "payout.requeue_failed")


async def _requeue_reversed(db) -> Case:
    return await _requeue(
        db, SettlementStatus.REVERSED, "requeue-reversed", "payout.requeue_reversed"
    )


async def _ensure_android(db) -> AppVersion:
    row = await db.scalar(select(AppVersion).where(AppVersion.platform == AppPlatform.ANDROID))
    if row is None:
        row = AppVersion(
            platform=AppPlatform.ANDROID, version="1.0.0", min_supported_version="1.0.0"
        )
        db.add(row)
        await db.flush()
    return row


async def _release(db) -> Case:
    row = await _ensure_android(db)
    return Case(
        url=f"{A}/app-versions/android/release",
        body={"version": "9.9.9", "notify": False},
        action="app_version.release",
        target_type="app_version",
        target_id="android",
        before={"version": row.version},
        after={"version": "9.9.9"},
        details={"notify": False},
    )


async def _service_create(db) -> Case:
    service = await _service(db)
    return Case(
        url=f"{V1}/services",
        body={
            "category_id": str(service.category_id),
            "name": "Sofa Cleaning",
            "base_price": 499.5,
            "duration_minutes": 60,
        },
        status=201,
        action="service.create",
        target_type="service",
        after={"name": "Sofa Cleaning", "base_price": 499.5, "is_active": True},
    )


async def _service_update(db) -> Case:
    service = await _service(db)
    return Case(
        url=f"{V1}/services/{service.id}",
        body={"name": "Very Deep Cleaning", "base_price": 1599.5},
        action="service.update",
        target_type="service",
        target_id=service.id,
        before={"name": "Deep Cleaning", "base_price": 1499.0},
        after={"name": "Very Deep Cleaning", "base_price": 1599.5},
    )


async def _service_delete(db) -> Case:
    service = await _service(db)
    return Case(
        url=f"{V1}/services/{service.id}",
        status=204,
        action="service.delete",
        target_type="service",
        target_id=service.id,
        before={"is_active": True},
    )


async def _pricing_category_update(db) -> Case:
    cat = await _pricing_category(db)
    return Case(
        url=f"{A}/pricing/categories/{cat.id}",
        body={"is_sensitive": True, "bounds_pct": 25},
        action="pricing.category_update",
        target_type="category",
        target_id=cat.id,
        before={"is_sensitive": False, "bounds_pct": None},
        after={"is_sensitive": True, "bounds_pct": 25.0},
    )


async def _pricing_config_update(db) -> Case:
    await seed_config(db)
    return Case(
        url=f"{A}/pricing/config/sheet_ttl_seconds",
        body={"value": 300},
        action="pricing.config_update",
        target_type="pricing_config",
        target_id="sheet_ttl_seconds",
        before={"value_json": 600},
        after={"value_json": 300},
    )


async def _coupon_create(db) -> Case:
    code = f"AUD{uuid.uuid4().hex[:8].upper()}"
    return Case(
        url=f"{A}/coupons",
        body={
            "code": code,
            "discount_type": "percent",
            "discount_value": 15,
            "valid_until": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
        status=201,
        action="coupon.create",
        target_type="coupon",
        after={"code": code.lower(), "discount_value": 15.0, "is_active": True},
    )


async def _coupon_update(db) -> Case:
    coupon = await _seed_coupon(db, f"AUD{uuid.uuid4().hex[:8]}")
    return Case(
        url=f"{A}/coupons/{coupon.code}",
        body={"discount_value": 25},
        action="coupon.update",
        target_type="coupon",
        target_id=coupon.id,
        before={"discount_value": 10.0},
        after={"discount_value": 25.0},
    )


async def _coupon_deactivate(db) -> Case:
    coupon = await _seed_coupon(db, f"AUD{uuid.uuid4().hex[:8]}")
    return Case(
        url=f"{A}/coupons/{coupon.code}/deactivate",
        action="coupon.deactivate",
        target_type="coupon",
        target_id=coupon.id,
        before={"is_active": True},
        after={"is_active": False},
    )


async def _coupon_activate(db) -> Case:
    coupon = await _seed_coupon(db, f"AUD{uuid.uuid4().hex[:8]}", is_active=False)
    return Case(
        url=f"{A}/coupons/{coupon.code}/activate",
        action="coupon.activate",
        target_type="coupon",
        target_id=coupon.id,
        before={"is_active": False},
        after={"is_active": True},
    )


AUDITED_WRITE_ROUTES = {
    ("PATCH", "/admin/users/{user_id}/deactivate"): _deactivate,
    ("POST", "/admin/pros/{professional_id}/force-remove"): _force_remove,
    ("POST", "/admin/pros/{professional_id}/reassign"): _reassign,
    ("POST", "/admin/providers/{user_id}/kyc/verify"): _sp_kyc_verify,
    ("POST", "/admin/providers/{user_id}/kyc/reject"): _sp_kyc_reject,
    ("POST", "/admin/pros/{professional_id}/kyc/verify"): _pro_kyc_verify,
    ("POST", "/admin/pros/{professional_id}/kyc/reject"): _pro_kyc_reject,
    ("POST", "/admin/bookings/{booking_id}/cancel"): _cancel,
    ("POST", "/admin/providers/{user_id}/payout"): _payout,
    ("POST", "/admin/providers/{user_id}/payout/requeue-failed"): _requeue_failed,
    ("POST", "/admin/providers/{user_id}/payout/requeue-reversed"): _requeue_reversed,
    ("POST", "/admin/app-versions/{platform}/release"): _release,
    ("POST", "/services"): _service_create,
    ("PATCH", "/services/{service_id}"): _service_update,
    ("DELETE", "/services/{service_id}"): _service_delete,
    ("PATCH", "/admin/pricing/categories/{category_id}"): _pricing_category_update,
    ("PATCH", "/admin/pricing/config/{key}"): _pricing_config_update,
    ("POST", "/admin/coupons"): _coupon_create,
    ("PATCH", "/admin/coupons/{code}"): _coupon_update,
    ("POST", "/admin/coupons/{code}/deactivate"): _coupon_deactivate,
    ("POST", "/admin/coupons/{code}/activate"): _coupon_activate,
}


def test_every_admin_write_route_has_an_audit_case():
    """A new admin write route fails here until it audits and has a case."""
    prefix = get_settings().API_V1_PREFIX
    found = set()
    for route in create_app().routes:
        path = getattr(route, "path", "").removeprefix(prefix)
        methods = getattr(route, "methods", None) or set()
        for method in methods - {"GET", "HEAD", "OPTIONS"}:
            is_admin = path.startswith("/admin") and not path.startswith("/admin/auth")
            if is_admin or path.startswith("/services"):
                found.add((method, path))
    assert found == set(AUDITED_WRITE_ROUTES)
    assert len(found) == 21


@pytest.fixture
def payable_plan():
    """The payout preflight, passing with a fixed plan.

    These tests are about the audit row, not the money checks (those live in
    tests/test_payout_refusals.py). A really payable SP would need owed bookings,
    and the payout handler commits, so they would leak into other files' tests.
    """
    plan = AsyncMock(return_value={"amount_paise": 16000, "booking_count": 2})
    with patch("app.services.payout_service.PayoutService.preflight_payout", plan):
        yield plan


@pytest.fixture
def no_broker():
    fake_task = type("T", (), {"id": "task_audit"})()
    with (
        patch(
            "app.tasks.payment_tasks.execute_provider_payout.delay", return_value=fake_task
        ) as payout,
        patch(
            "app.tasks.notification_tasks.send_booking_notification.delay",
            return_value=fake_task,
        ),
        patch(
            "app.tasks.notification_tasks.broadcast_update_available.delay",
            return_value=fake_task,
        ),
        patch(
            "app.tasks.notification_tasks.send_kyc_rejected_notification.delay",
            return_value=fake_task,
        ),
    ):
        yield payout


@pytest.mark.parametrize("route", sorted(AUDITED_WRITE_ROUTES))
async def test_write_route_writes_exactly_one_correct_row(
    client, db_session, no_broker, payable_plan, route
):
    admin = await _admin(db_session)
    case = await AUDITED_WRITE_ROUTES[route](db_session)

    method, _ = route
    kwargs = {"json": case.body} if case.body is not None else {}
    r = await client.request(method, case.url, headers=_headers(admin), **kwargs)
    assert r.status_code == case.status, r.text

    [row] = await _rows(db_session, admin_user_id=admin.id)
    target_id = case.target_id if case.target_id is not None else r.json()["id"]
    assert (row.action, row.target_type, row.target_id) == (
        case.action,
        case.target_type,
        str(target_id),
    )
    assert row.action in ADMIN_ACTIONS
    assert row.target_type in ADMIN_ACTION_TARGET_TYPES
    assert (row.sp_user_id, row.to_sp_user_id, row.reason) == (
        case.sp_user_id,
        case.to_sp_user_id,
        case.reason,
    )
    assert (str(row.ip_address), row.user_agent) == (IP, UA)

    for saved, expected in ((row.before_json, case.before), (row.after_json, case.after)):
        if expected is None:
            assert saved is None
        else:
            assert saved.items() >= expected.items(), saved
    assert row.details.items() >= (case.details or {}).items(), row.details
    _assert_nothing_blocked(row)

    # Really NULL, not the JSON value null: a create has no before, a delete no after.
    is_null = (
        await db_session.execute(
            select(AdminAction.before_json.is_(None), AdminAction.after_json.is_(None)).where(
                AdminAction.id == row.id
            )
        )
    ).one()
    assert tuple(is_null) == (case.before is None, case.after is None)


# ---------------------------------------------------------------------------
# Refused actions write nothing
# ---------------------------------------------------------------------------


async def test_refused_actions_write_no_row(client, db_session, no_broker):
    admin = await _admin(db_session)
    headers = _headers(admin)
    pending_sp = await _eligible_provider(db_session, await _service(db_session))
    _, cancelled = await _unpaid_booking(db_session, status=BookingStatus.CANCELLED)
    missing = uuid.uuid4()

    refused = [
        # Not SUBMITTED.
        ("POST", f"{A}/providers/{pending_sp.id}/kyc/verify", None, 403),
        ("POST", f"{A}/providers/{missing}/kyc/verify", None, 404),
        ("POST", f"{A}/pros/{missing}/kyc/verify", None, 404),
        ("POST", f"{A}/bookings/{cancelled.id}/cancel", {"reason": "dup"}, 409),
        ("POST", f"{A}/bookings/{missing}/cancel", {"reason": "dup"}, 404),
        ("POST", f"{A}/pros/{missing}/force-remove", {"reason": "gone"}, 404),
        ("PATCH", f"{V1}/services/{missing}", {"name": "Nothing here"}, 404),
        ("DELETE", f"{V1}/services/{missing}", None, 404),
        # Nothing to requeue.
        ("POST", f"{A}/providers/{pending_sp.id}/payout/requeue-failed", None, None),
        # Refused by the payout preflight: nothing payable, and no provider at all.
        ("POST", f"{A}/providers/{pending_sp.id}/payout", None, 402),
        ("POST", f"{A}/providers/{missing}/payout", None, 404),
    ]
    for method, url, body, expected in refused:
        kwargs = {"json": body} if body is not None else {}
        r = await client.request(method, url, headers=headers, **kwargs)
        if expected is None:
            assert r.status_code >= 400, r.text
        else:
            assert r.status_code == expected, f"{url}: {r.status_code} {r.text}"

    assert await _rows(db_session, admin_user_id=admin.id) == []


# ---------------------------------------------------------------------------
# Same transaction: a rolled-back action leaves no row
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_tx_client(db_session, redis_client):
    """``get_db`` as production has it: its own session per request, commit at the
    end, rollback on an exception. 500s come back as responses."""
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def real_get_db():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = real_get_db
    app.dependency_overrides[get_redis] = lambda: redis_client
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await engine.dispose()


async def test_a_failure_after_the_audit_call_rolls_back_the_row_and_the_change(
    real_tx_client, db_session
):
    admin = await _admin(db_session)
    _, booking = await _unpaid_booking(db_session)
    await db_session.commit()
    admin_id, booking_id = admin.id, booking.id

    with patch(
        "app.tasks.notification_tasks.send_booking_notification.delay",
        side_effect=RuntimeError("broker down"),
    ):
        r = await real_tx_client.post(
            f"{A}/bookings/{booking.id}/cancel",
            json={"reason": "Duplicate booking"},
            headers=_headers(admin),
        )
    assert r.status_code == 500

    assert await _rows(db_session, admin_user_id=admin_id) == []
    db_session.expire_all()
    assert (await db_session.get(Booking, booking_id)).status == BookingStatus.ACCEPTED


async def test_a_failed_audit_write_takes_the_change_with_it(real_tx_client, db_session):
    admin = await _admin(db_session)
    service = await _service(db_session)
    await db_session.commit()
    service_id = service.id

    with patch(
        "app.routers.services.audit_admin_action",
        AsyncMock(side_effect=RuntimeError("audit down")),
    ):
        r = await real_tx_client.patch(
            f"{V1}/services/{service.id}",
            json={"name": "Changed Name"},
            headers=_headers(admin),
        )
    assert r.status_code == 500

    db_session.expire_all()
    assert (await db_session.get(Service, service_id)).name == "Deep Cleaning"


async def test_a_committed_action_keeps_its_row_under_the_real_session(real_tx_client, db_session):
    """The routes that lean on ``get_db``'s own commit really do commit the row."""
    admin = await _admin(db_session)
    service = await _service(db_session)
    await db_session.commit()
    admin_id, service_id = admin.id, service.id

    r = await real_tx_client.delete(f"{V1}/services/{service.id}", headers=_headers(admin))
    assert r.status_code == 204

    assert [row.action for row in await _rows(db_session, admin_user_id=admin_id)] == [
        "service.delete"
    ]
    db_session.expire_all()
    assert (await db_session.get(Service, service_id)).is_active is False


# ---------------------------------------------------------------------------
# Payout
# ---------------------------------------------------------------------------


async def test_payout_row_is_committed_before_the_task_is_enqueued(
    client, db_session, payable_plan
):
    admin = await _admin(db_session)
    sp = await _eligible_provider(db_session, await _service(db_session))
    await db_session.commit()

    # A separate connection: it only sees the row if it has really committed.
    sync_url = make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg2")
    sync_engine = create_engine(sync_url)
    seen = []

    def on_delay(*_args):
        with sync_engine.connect() as conn:
            seen.append(
                conn.execute(
                    text("SELECT action FROM admin_actions WHERE admin_user_id = :a"),
                    {"a": admin.id},
                ).all()
            )
        return type("T", (), {"id": "task_audit"})()

    try:
        with patch(
            "app.tasks.payment_tasks.execute_provider_payout.delay", side_effect=on_delay
        ) as delay:
            r = await client.post(f"{A}/providers/{sp.id}/payout", headers=_headers(admin))
    finally:
        sync_engine.dispose()

    assert r.status_code == 202, r.text
    delay.assert_called_once_with(str(sp.id), str(admin.id))
    assert [tuple(row) for row in seen[0]] == [("payout.enqueue",)]


async def test_a_failed_enqueue_keeps_the_row(real_tx_client, db_session, payable_plan):
    admin = await _admin(db_session)
    sp = await _eligible_provider(db_session, await _service(db_session))
    await db_session.commit()

    with patch(
        "app.tasks.payment_tasks.execute_provider_payout.delay",
        side_effect=RuntimeError("broker down"),
    ):
        r = await real_tx_client.post(f"{A}/providers/{sp.id}/payout", headers=_headers(admin))
    assert r.status_code == 500

    [row] = await _rows(db_session, admin_user_id=admin.id)
    assert (row.action, row.target_id) == ("payout.enqueue", str(sp.id))


async def test_payout_details_carry_the_amount_and_booking_count(
    client, db_session, no_broker, payable_plan
):
    admin = await _admin(db_session)
    sp = await _eligible_provider(db_session, await _service(db_session))
    r = await client.post(f"{A}/providers/{sp.id}/payout", headers=_headers(admin))
    assert r.status_code == 202, r.text
    [row] = await _rows(db_session, admin_user_id=admin.id)
    assert row.details == {"amount_paise": 16000, "booking_count": 2}
    payable_plan.assert_awaited_once_with(sp.id)


async def test_a_refused_batch_writes_no_row_and_enqueues_nothing(client, db_session, no_broker):
    admin = await _admin(db_session)
    sp = await _eligible_provider(db_session, await _service(db_session))
    over_cap = PayoutRefused("Payout batch of 1 paise exceeds cap of 0 paise")
    with patch(
        "app.services.payout_service.PayoutService.preflight_payout",
        AsyncMock(side_effect=over_cap),
    ):
        r = await client.post(f"{A}/providers/{sp.id}/payout", headers=_headers(admin))
    assert r.status_code == 402, r.text
    assert await _rows(db_session, admin_user_id=admin.id) == []
    no_broker.assert_not_called()


async def test_payout_for_an_unknown_id_is_a_404_with_no_row(client, db_session, no_broker):
    """The preflight 404s an id with no provider profile, before the row: it used
    to be a 202 with a NULL sp_user_id (an FK to users) and a task that paid
    nothing."""
    admin = await _admin(db_session)
    unknown = uuid.uuid4()
    r = await client.post(f"{A}/providers/{unknown}/payout", headers=_headers(admin))
    assert r.status_code == 404, r.text
    assert await _rows(db_session, admin_user_id=admin.id) == []
    no_broker.assert_not_called()


# ---------------------------------------------------------------------------
# Release: admin writes one row, CI token writes none
# ---------------------------------------------------------------------------


async def test_ci_token_release_writes_no_row_and_keeps_the_log_line(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(get_settings(), "INTERNAL_RELEASE_TOKEN", "ci-token")
    await _ensure_android(db_session)
    before = len(await _rows(db_session, action="app_version.release"))

    with structlog.testing.capture_logs() as logs:
        r = await client.post(
            f"{A}/app-versions/android/release",
            json={"version": "8.8.8", "notify": False},
            headers={"X-Internal-Release-Token": "ci-token"},
        )
    assert r.status_code == 200, r.text
    assert len(await _rows(db_session, action="app_version.release")) == before
    [line] = [e for e in logs if e.get("event") == "app_version_published"]
    assert line["release_auth"] == "token"


# ---------------------------------------------------------------------------
# Allowed fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record_type", sorted(AUDIT_FIELDS))
def test_allow_lists_name_real_columns_and_nothing_blocked(record_type):
    columns = set(RECORD_MODELS[record_type].__table__.columns.keys())
    fields = AUDIT_FIELDS[record_type]
    assert set(fields) <= columns
    assert not [f for f in fields if is_blocked_field(f)]


def test_every_field_the_ticket_blocks_is_real_and_blocked():
    for model, fields in BLOCKED_BY_MODEL.items():
        columns = set(model.__table__.columns.keys())
        for field in fields:
            assert field in columns, f"{model.__name__}.{field}"
            assert is_blocked_field(field)

    encrypted = [c for c in ProviderProfile.__table__.columns.keys() if c.endswith("_encrypted")]
    assert encrypted
    assert all(is_blocked_field(c) for c in encrypted)


async def test_blocked_keys_are_dropped_even_if_a_caller_passes_them(db_session):
    admin = await _admin(db_session)
    row = await audit_admin_action(
        db_session,
        AuditActor(admin.id),
        action="service.update",
        target_type="service",
        target_id="x",
        before={"name": "ok", "kyc_docs": {"a": CANARY}, "nested": {"totp_secret": CANARY}},
        after={"items": [{"pan_encrypted": CANARY, "name": "ok"}], "doorstep_otp": OTP_CANARY},
        details={"password_hash": CANARY, "fcm_token": CANARY, "razorpayx_contact_id": CANARY},
    )
    assert row.before_json == {"name": "ok", "nested": {}}
    assert row.after_json == {"items": [{"name": "ok"}]}
    assert row.details == {}
    _assert_nothing_blocked(row)


async def test_values_are_json_safe(db_session):
    admin = await _admin(db_session)
    an_id = uuid.uuid4()
    moment = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    row = await audit_admin_action(
        db_session,
        AuditActor(admin.id),
        action="service.update",
        target_type="service",
        target_id=an_id,
        before={
            "id": an_id,
            "at": moment,
            "on": date(2026, 11, 6),
            "status": BookingStatus.ACCEPTED,
            "price": Decimal("499.50"),
            "many": (an_id,),
            "none": None,
        },
    )
    await db_session.flush()
    assert row.target_id == str(an_id)
    assert row.before_json == {
        "id": str(an_id),
        "at": moment.isoformat(),
        "on": "2026-11-06",
        "status": BookingStatus.ACCEPTED.value,
        "price": 499.5,
        "many": [str(an_id)],
        "none": None,
    }
    json.dumps(row.before_json)


def test_snapshot_of_no_row_is_none():
    assert snapshot("service", None) is None


# ---------------------------------------------------------------------------
# IP and User-Agent never fail the action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "xff",
    ["unknown", "not-an-ip", "1.2.3.4:5678", "1.2.3.4,", "fe80::1%eth0", "999.1.1.1"],
)
async def test_an_invalid_ip_saves_null_and_the_action_succeeds(client, db_session, xff):
    admin = await _admin(db_session)
    service = await _service(db_session)
    r = await client.delete(
        f"{V1}/services/{service.id}", headers=_headers(admin, **{"X-Forwarded-For": xff})
    )
    assert r.status_code == 204, r.text
    [row] = await _rows(db_session, admin_user_id=admin.id)
    assert row.ip_address is None


async def test_an_overlong_user_agent_is_cut_and_the_action_succeeds(client, db_session):
    admin = await _admin(db_session)
    service = await _service(db_session)
    r = await client.delete(
        f"{V1}/services/{service.id}", headers=_headers(admin, **{"User-Agent": "x" * 5000})
    )
    assert r.status_code == 204, r.text
    [row] = await _rows(db_session, admin_user_id=admin.id)
    assert row.user_agent == "x" * 512


@pytest.mark.parametrize(
    ("xff", "saved"),
    [
        # The rightmost entry: a client can only control what's to its left.
        ("6.6.6.6, 203.0.113.7", "203.0.113.7"),
        ("2001:db8::1", "2001:db8::1"),
    ],
)
async def test_a_valid_ip_is_saved(client, db_session, xff, saved):
    admin = await _admin(db_session)
    service = await _service(db_session)
    r = await client.delete(
        f"{V1}/services/{service.id}", headers=_headers(admin, **{"X-Forwarded-For": xff})
    )
    assert r.status_code == 204, r.text
    [row] = await _rows(db_session, admin_user_id=admin.id)
    assert str(row.ip_address) == saved


async def test_a_nul_in_the_user_agent_and_no_client_can_still_be_saved(db_session):
    """No real client can get a NUL through the HTTP layer, so this is built by
    hand. Postgres would refuse it, failing the admin's real action."""
    admin = await _admin(db_session)
    request = Request(
        {"type": "http", "method": "POST", "headers": [(b"user-agent", b"a\x00b")], "client": None}
    )
    actor = audit_actor(request, admin)
    assert (actor.ip_address, actor.user_agent) == (None, "ab")

    row = await audit_admin_action(
        db_session, actor, action="service.delete", target_type="service", target_id="x"
    )
    assert (row.ip_address, row.user_agent) == (None, "ab")


# ---------------------------------------------------------------------------
# GET /admin/audit
# ---------------------------------------------------------------------------


AUDIT_ITEM_FIELDS = {
    "id",
    "admin_user_id",
    "admin_name",
    "admin_email",
    "action",
    "target_type",
    "target_id",
    "sp_user_id",
    "to_sp_user_id",
    "reason",
    "details",
    "before_json",
    "after_json",
    "ip_address",
    "user_agent",
    "created_at",
}
T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


async def _seed_log(db, admin) -> None:
    """Five rows, an hour apart, oldest first."""
    for i, (action, target_type, target_id) in enumerate(
        [
            ("kyc.approve", "provider", "p-1"),
            ("kyc.reject", "provider", "p-2"),
            ("service.create", "service", "s-1"),
            ("app_version.release", "app_version", "android"),
            ("kyc.approve", "professional", "p-1"),
        ]
    ):
        db.add(
            AdminAction(
                admin_user_id=admin.id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                ip_address=IP,
                user_agent=UA,
                before_json={"n": i},
                after_json={"n": i + 1},
                created_at=T0 + timedelta(hours=i),
            )
        )
    await db.flush()


async def _audit(client, admin, **params):
    r = await client.get(
        f"{A}/audit", params={"admin_user_id": str(admin.id), **params}, headers=_session(admin)
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_audit_returns_the_contract_shape_newest_first(client, db_session):
    admin = await _admin(db_session)
    await _seed_log(db_session, admin)

    body = await _audit(client, admin)
    assert (body["total"], body["page"], body["per_page"]) == (5, 1, 50)
    assert [i["after_json"]["n"] for i in body["items"]] == [5, 4, 3, 2, 1]

    item = body["items"][0]
    assert set(item) == AUDIT_ITEM_FIELDS
    assert (item["admin_user_id"], item["admin_email"]) == (str(admin.id), admin.email)
    assert (item["before_json"], item["ip_address"], item["user_agent"]) == ({"n": 4}, IP, UA)
    # Text, whatever the target is keyed by.
    android = (await _audit(client, admin, target_type="app_version"))["items"][0]
    assert android["target_id"] == "android"


async def test_audit_pages(client, db_session):
    admin = await _admin(db_session)
    await _seed_log(db_session, admin)

    pages = [await _audit(client, admin, per_page=2, page=p) for p in (1, 2, 3, 4)]
    assert [[i["after_json"]["n"] for i in p["items"]] for p in pages] == [[5, 4], [3, 2], [1], []]
    assert {p["total"] for p in pages} == {5}
    assert (pages[1]["page"], pages[1]["per_page"]) == (2, 2)


@pytest.mark.parametrize("per_page", [0, 201])
async def test_audit_refuses_a_per_page_out_of_range(client, db_session, per_page):
    admin = await _admin(db_session)
    r = await client.get(f"{A}/audit", params={"per_page": per_page}, headers=_session(admin))
    assert r.status_code == 422


async def test_audit_filters(client, db_session):
    admin = await _admin(db_session)
    other = await _admin(db_session)
    await _seed_log(db_session, admin)
    await _seed_log(db_session, other)

    async def ns(**params):
        return [i["after_json"]["n"] for i in (await _audit(client, admin, **params))["items"]]

    assert await ns() == [5, 4, 3, 2, 1], "the other admin's rows are filtered out"
    assert await ns(action="kyc.approve") == [5, 1]
    assert await ns(target_type="provider") == [2, 1]
    assert await ns(target_id="p-1") == [5, 1]
    assert await ns(action="kyc.approve", target_type="professional", target_id="p-1") == [5]
    assert await ns(action="nothing.like.this") == []


async def test_audit_date_range_includes_from_and_excludes_to(client, db_session):
    admin = await _admin(db_session)
    await _seed_log(db_session, admin)

    async def ns(**params):
        return [i["after_json"]["n"] for i in (await _audit(client, admin, **params))["items"]]

    start = T0 + timedelta(hours=1)
    end = T0 + timedelta(hours=3)
    assert await ns(date_from=start.isoformat()) == [5, 4, 3, 2]
    assert await ns(date_to=end.isoformat()) == [3, 2, 1]
    assert await ns(date_from=start.isoformat(), date_to=end.isoformat()) == [3, 2]
    # An offset is honoured, and no offset means UTC.
    assert await ns(date_from="2026-01-10T18:30:00+05:30", date_to=end.isoformat()) == [3, 2]
    assert await ns(date_from="2026-01-10T13:00:00", date_to="2026-01-10T15:00:00") == [3, 2]


async def test_audit_refuses_a_bad_date(client, db_session):
    admin = await _admin(db_session)
    r = await client.get(f"{A}/audit", params={"date_from": "soon"}, headers=_session(admin))
    assert r.status_code == 422


@pytest.mark.parametrize("role", [AdminRole.OPS, AdminRole.SUPPORT])
@pytest.mark.parametrize("path", ["/audit", "/audit/filters"])
async def test_audit_is_refused_for_ops_and_support(client, db_session, role, path):
    someone = await _admin(db_session, role)
    assert (await client.get(f"{A}{path}", headers=_session(someone))).status_code == 403


async def test_audit_filters_route_lists_what_is_in_the_log(client, db_session):
    tag = uuid.uuid4().hex[:8]
    admin = await _admin(db_session)
    admin.name = f"Asha {tag}"
    absent = await _admin(db_session)
    db_session.add(
        AdminAction(
            admin_user_id=admin.id,
            action=f"test.{tag}",
            target_type=f"t{tag}",
            target_id="x",
        )
    )
    await db_session.flush()

    r = await client.get(f"{A}/audit/filters", headers=_session(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"admins", "actions", "target_types"}
    assert {"id": str(admin.id), "name": f"Asha {tag}", "email": admin.email} in body["admins"]
    assert str(absent.id) not in {a["id"] for a in body["admins"]}
    assert f"test.{tag}" in body["actions"]
    assert f"t{tag}" in body["target_types"]
    assert len(body["actions"]) == len(set(body["actions"]))
    assert body["actions"] == sorted(body["actions"])


# ---------------------------------------------------------------------------
# GET /admin/actions: the 3 team actions, same shape
# ---------------------------------------------------------------------------


async def test_actions_route_shows_only_team_actions_in_the_old_shape(client, db_session):
    admin = await _admin(db_session)
    support = await _admin(db_session, AdminRole.SUPPORT)
    sp, _, member = await _team_sp(db_session, await _service(db_session))
    for action, target_type, target_id in [
        ("pro_force_removed", "professional", str(member.id)),
        ("kyc.approve", "provider", str(sp.id)),
        # A target that isn't a uuid would be a 500 here if it leaked through.
        ("app_version.release", "app_version", "android"),
    ]:
        db_session.add(
            AdminAction(
                admin_user_id=admin.id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                sp_user_id=sp.id,
                before_json={"secret": "not for this route"},
            )
        )
    await db_session.flush()

    for reader in (admin, support):
        r = await client.get(
            f"{A}/actions", params={"sp_user_id": str(sp.id)}, headers=_session(reader)
        )
        assert r.status_code == 200, r.text
        [row] = r.json()
        assert row["action"] == "pro_force_removed"
        assert set(row) == {
            "id",
            "admin_user_id",
            "admin_name",
            "action",
            "target_type",
            "target_id",
            "sp_user_id",
            "to_sp_user_id",
            "reason",
            "details",
            "created_at",
        }

    r = await client.get(
        f"{A}/actions",
        params={"target_type": "professional", "target_id": str(member.id)},
        headers=_session(admin),
    )
    assert [row["target_id"] for row in r.json()] == [str(member.id)]


def test_the_team_action_names_are_unchanged():
    """The inline Teams screen (src/lib/admin-team.ts) reads these by name."""
    assert set(TEAM_ACTIONS) == {"user_deactivated", "pro_force_removed", "pro_reassigned"}
    assert set(TEAM_ACTIONS) <= set(ADMIN_ACTIONS)
