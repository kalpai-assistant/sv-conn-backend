"""
Provider payout tests.

Covers: happy-path payout, duplicate-payout rejection, payout failure handling,
batch isolation, the async RazorpayX client, webhook-handler error surfacing (no
silent returns), the no-details and IMPS/bank payout paths, and the dedicated
RazorpayX payout webhook endpoint (separate from the Payments webhook).
"""
import inspect
import json
import hmac
import hashlib
import uuid
import httpx
import pytest
from unittest.mock import patch, AsyncMock

from app.routers import payments as payments_router

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from tests.conftest import TEST_DATABASE_URL
from tests.factories.user_factory import UserFactory
from tests.factories.booking_factory import BookingFactory
from app.database import Base
from app.models.booking import Booking, BookingStatus, SettlementStatus
from app.models.provider_profile import ProviderProfile
from app.models.payout_audit import PayoutAudit
from app.models.user import User
from app.models.category import Category
from app.models.service import Service
from app.models.address import Address
from app.services.payout_service import PayoutService
from app.utils import razorpayx_client
from app.exceptions import PaymentError
from app.utils.encryption import encrypt
from app.config import get_settings

settings = get_settings()


async def _booking_for(db_session, consumer, provider, **kwargs):
    """Create a completed booking with all FK dependencies for the given provider.

    Uses a unique category/service per call so multiple bookings can be created in
    one test without violating the category name/slug unique constraints.
    """
    from app.models.category import Category
    from app.models.service import Service
    from app.models.address import Address

    suffix = uuid.uuid4().hex[:8]
    category = Category(
        name=f"Home Repair {suffix}", slug=f"home-repair-{suffix}", is_active=True, sort_order=1
    )
    db_session.add(category)
    await db_session.flush()

    service = Service(
        category_id=category.id,
        name=f"AC Service {suffix}",
        base_price=500.0,
        duration_minutes=60,
        is_active=True,
    )
    db_session.add(service)

    address = Address(user_id=consumer.id, label="home", full_address="789 Test Ave")
    db_session.add(address)
    await db_session.flush()

    return await BookingFactory.create(
        db_session,
        consumer_id=consumer.id,
        provider_id=provider.id,
        service_id=service.id,
        address_id=address.id,
        status=BookingStatus.COMPLETED,
        provider_payout=80.0,
        **kwargs,
    )


async def _provider_with_payout_vpa(db_session, provider):
    profile = ProviderProfile(
        user_id=provider.id,
        payout_vpa_encrypted=encrypt("provider@upi"),
    )
    db_session.add(profile)
    await db_session.flush()
    return profile


@pytest.mark.asyncio
async def test_payout_happy_path(db_session):
    """Completed + PENDING bookings are paid out and flipped to PROCESSING."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_payout_vpa(db_session, provider)
    b1 = await _booking_for(db_session, consumer, provider)
    b2 = await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_test_1", "status": "processing"}
        result = await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    # One payout for the summed amount, both bookings now PROCESSING.
    mock_payout.assert_called_once()
    _, kwargs = mock_payout.call_args
    assert kwargs["amount_paise"] == 16000  # (80 + 80) * 100
    assert kwargs["vpa"] == "provider@upi"
    assert result["booking_count"] == 2

    for b in (b1, b2):
        await db_session.refresh(b)
        assert b.settlement_status == SettlementStatus.PROCESSING

    # The idempotency key is derived from the batch's booking ids (stable + unique).
    expected_key = PayoutService._batch_idempotency_key([b1, b2])
    assert kwargs["idempotency_key"] == expected_key
    # Order-independent and distinct from a different batch.
    assert PayoutService._batch_idempotency_key([b2, b1]) == expected_key
    assert PayoutService._batch_idempotency_key([b1]) != expected_key


@pytest.mark.asyncio
async def test_payout_no_duplicate(db_session):
    """A second payout finds nothing PENDING and must not pay out again."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_payout_vpa(db_session, provider)
    await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_test_2", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

        # No PENDING settlements remain → second call raises, no second payout.
        with pytest.raises(PaymentError):
            await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    assert mock_payout.call_count == 1


@pytest.mark.asyncio
async def test_payout_failure_webhook(db_session):
    """payout.failed webhook flips the provider's PROCESSING settlements to FAILED."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_payout_vpa(db_session, provider)
    booking = await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_test_3", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.PROCESSING
    assert booking.settlement_payout_id == "pout_test_3"

    await PayoutService(db_session).handle_payout_webhook(
        event="payout.failed",
        payload={"payout": {"entity": {"id": "pout_test_3"}}},
    )

    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.FAILED


@pytest.mark.asyncio
async def test_payout_webhook_only_affects_its_own_batch(db_session):
    """A payout webhook must settle only its own payout, not other in-flight batches."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_payout_vpa(db_session, provider)

    # Batch A: one booking, paid out as pout_A.
    b_a = await _booking_for(db_session, consumer, provider)
    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_A", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    # Batch B: a later booking, paid out as pout_B (both now PROCESSING).
    b_b = await _booking_for(db_session, consumer, provider)
    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_B", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    # Batch A succeeds — only A's booking should flip to COMPLETED.
    await PayoutService(db_session).handle_payout_webhook(
        event="payout.processed",
        payload={"payout": {"entity": {"id": "pout_A"}}},
    )

    await db_session.refresh(b_a)
    await db_session.refresh(b_b)
    assert b_a.settlement_status == SettlementStatus.COMPLETED
    assert b_b.settlement_status == SettlementStatus.PROCESSING  # untouched by A


async def _provider_with_bank_account(db_session, provider):
    """Provider with a bank account but no VPA → forces the IMPS payout path."""
    profile = ProviderProfile(
        user_id=provider.id,
        payout_account_number_encrypted=encrypt("1234567890"),
        payout_ifsc_encrypted=encrypt("HDFC0001234"),
    )
    db_session.add(profile)
    await db_session.flush()
    return profile


# ---------------------------------------------------------------------------
# #1 — RazorpayX client must be async (no blocking httpx.post on the event loop)
# ---------------------------------------------------------------------------

def test_create_payout_is_async():
    """Regression guard: the RazorpayX client must be awaitable, not blocking."""
    assert inspect.iscoroutinefunction(razorpayx_client.create_payout)


# ---------------------------------------------------------------------------
# #4 — handle_payout_webhook must not fail silently
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payout_webhook_missing_id_raises(db_session):
    """A payout.processed event with no payout id is malformed → raise, not swallow."""
    with pytest.raises(PaymentError):
        await PayoutService(db_session).handle_payout_webhook(
            event="payout.processed",
            payload={"payout": {"entity": {}}},
        )


@pytest.mark.asyncio
async def test_payout_webhook_ignores_unknown_event(db_session):
    """Non-terminal RazorpayX events are an acknowledged no-op (no raise, no change)."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_payout_vpa(db_session, provider)
    booking = await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_unknown", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    # An event type we don't handle must not raise and must leave state untouched.
    await PayoutService(db_session).handle_payout_webhook(
        event="payout.updated",
        payload={"payout": {"entity": {"id": "pout_unknown"}}},
    )
    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.PROCESSING


# ---------------------------------------------------------------------------
# Coverage gaps flagged in review: no-details error, IMPS/bank path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payout_no_details_raises(db_session):
    """Provider with neither a VPA nor a bank account → PaymentError, no payout."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    db_session.add(ProviderProfile(user_id=provider.id))
    await db_session.flush()
    await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        with pytest.raises(PaymentError):
            await PayoutService(db_session).create_provider_payout(provider.id, admin.id)
    mock_payout.assert_not_called()


@pytest.mark.asyncio
async def test_payout_uses_imps_for_bank_account(db_session):
    """A provider with only a bank account is paid via account_number + IFSC (IMPS)."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_bank_account(db_session, provider)
    await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_imps", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    _, kwargs = mock_payout.call_args
    assert kwargs["vpa"] is None
    assert kwargs["account_number"] == "1234567890"
    assert kwargs["ifsc"] == "HDFC0001234"


# ---------------------------------------------------------------------------
# #2 — RazorpayX payouts arrive on their own endpoint, not the Payments webhook
# ---------------------------------------------------------------------------

async def _booking_in_processing(db_session, payout_id: str):
    """Create a provider's booking already PROCESSING under the given payout id."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_payout_vpa(db_session, provider)
    booking = await _booking_for(db_session, consumer, provider)
    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": payout_id, "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)
    await db_session.refresh(booking)
    return booking


@pytest.mark.asyncio
async def test_payout_webhook_endpoint_settles(client, db_session):
    """POST /payments/payout-webhook routes a payout.processed event to PayoutService."""
    booking = await _booking_in_processing(db_session, "pout_endpoint")

    resp = await client.post(
        f"{settings.API_V1_PREFIX}/payments/payout-webhook",
        json={"event": "payout.processed",
              "payload": {"payout": {"entity": {"id": "pout_endpoint"}}}},
    )
    assert resp.status_code == 200

    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.COMPLETED


@pytest.mark.asyncio
async def test_payments_webhook_does_not_settle_payouts(client, db_session):
    """A payout event sent to the Payments webhook must not settle bookings.

    The Payments and RazorpayX products use different webhook secrets; payout
    events belong to /payments/payout-webhook, so the Payments webhook must leave
    settlements untouched (regression for the merged-webhook bug).
    """
    booking = await _booking_in_processing(db_session, "pout_wrong_endpoint")

    resp = await client.post(
        f"{settings.API_V1_PREFIX}/payments/webhook",
        json={"event": "payout.processed",
              "payload": {"payout": {"entity": {"id": "pout_wrong_endpoint"}}}},
    )
    assert resp.status_code == 200

    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.PROCESSING


# ---------------------------------------------------------------------------
# #3 — Concurrent payouts must not double-pay (FOR UPDATE SKIP LOCKED)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_payouts_do_not_double_pay():
    """Two concurrent payout attempts for one provider must not both pay out.

    Uses two real transactions (separate connections), since the race is between
    concurrent DB sessions. Transaction A pays out and holds the row locks without
    committing; transaction B then runs and, thanks to FOR UPDATE SKIP LOCKED,
    skips A's locked rows, finds nothing PENDING, and raises instead of issuing a
    second payout. Without the lock, B would read the same still-PENDING rows
    (A's flip is uncommitted, hence invisible) and pay again — a double-pay.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Setup committed so both concurrent transactions can see it.
    async with factory() as s:
        consumer = await UserFactory.create(s, role="consumer")
        provider = await UserFactory.create(s, role="provider")
        admin = await UserFactory.create(s, role="admin")
        s.add(ProviderProfile(user_id=provider.id, payout_vpa_encrypted=encrypt("provider@upi")))
        booking = await _booking_for(s, consumer, provider)
        await s.commit()
        provider_id = provider.id
        admin_id = admin.id
        ids = {
            "booking": booking.id,
            "service": booking.service_id,
            "address": booking.consumer_address_id,
            "consumer": consumer.id,
            "provider": provider.id,
            "admin": admin.id,
        }

    session_a = factory()
    session_b = factory()
    try:
        with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
            mock_payout.return_value = {"id": "pout_race", "status": "processing"}

            # A pays out and holds the locks (no commit yet).
            await PayoutService(session_a).create_provider_payout(provider_id, admin_id)

            # B runs concurrently: SKIP LOCKED hides A's rows → nothing to settle.
            with pytest.raises(PaymentError):
                await PayoutService(session_b).create_provider_payout(provider_id, admin_id)

            assert mock_payout.call_count == 1  # exactly one real payout

        await session_a.commit()
    finally:
        await session_a.close()
        await session_b.rollback()
        await session_b.close()
        # Remove the committed rows (FK-safe order) so the test self-cleans.
        async with factory() as s:
            cat_id = (
                await s.execute(select(Service.category_id).where(Service.id == ids["service"]))
            ).scalar()
            await s.execute(
                delete(PayoutAudit).where(PayoutAudit.provider_id == ids["provider"])
            )
            await s.execute(delete(Booking).where(Booking.id == ids["booking"]))
            await s.execute(delete(Address).where(Address.id == ids["address"]))
            await s.execute(delete(Service).where(Service.id == ids["service"]))
            if cat_id:
                await s.execute(delete(Category).where(Category.id == cat_id))
            await s.execute(delete(ProviderProfile).where(ProviderProfile.user_id == ids["provider"]))
            await s.execute(
                delete(User).where(User.id.in_([ids["consumer"], ids["provider"], ids["admin"]]))
            )
            await s.commit()
        await engine.dispose()


# ---------------------------------------------------------------------------
# #7 — Audit trail: every payout attempt is recorded (success and failure)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payout_writes_success_audit(db_session):
    """A successful payout writes a payout_audits row: who, whom, amount, outcome."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_payout_vpa(db_session, provider)
    await _booking_for(db_session, consumer, provider)

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout:
        mock_payout.return_value = {"id": "pout_audit", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    audit = (
        await db_session.execute(
            select(PayoutAudit).where(PayoutAudit.provider_id == provider.id)
        )
    ).scalar_one()
    assert audit.admin_user_id == admin.id
    assert audit.payout_id == "pout_audit"
    assert audit.outcome == "processing"
    assert audit.amount_paise == 8000  # 80 * 100
    assert audit.booking_count == 1
    assert audit.error is None


@pytest.mark.asyncio
async def test_payout_failure_records_audit_and_keeps_bookings_pending(db_session):
    """A failed RazorpayX call records a failed audit, re-raises, and flips nothing.

    ``_record_failed_audit`` is patched (it commits in its own session, which the
    test's rolled-back fixture can't satisfy via FKs); this asserts the service
    records the failure and leaves the bookings untouched for a safe retry.
    """
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    await _provider_with_payout_vpa(db_session, provider)
    booking = await _booking_for(db_session, consumer, provider)

    boom = RuntimeError("RazorpayX 503")
    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as mock_payout, \
         patch.object(PayoutService, "_record_failed_audit", new_callable=AsyncMock) as mock_audit:
        mock_payout.side_effect = boom
        with pytest.raises(RuntimeError):
            await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    # Failure audit was recorded with the right facts.
    mock_audit.assert_awaited_once()
    args = mock_audit.call_args.args
    assert args[0] == admin.id and args[1] == provider.id
    assert args[2] == 8000 and args[3] == 1  # amount_paise, booking_count

    # Bookings remain PENDING — nothing was settled, so a retry is safe.
    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.PENDING


# ---------------------------------------------------------------------------
# #8 — Transient RazorpayX failures are retried with backoff
# ---------------------------------------------------------------------------

class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient that returns canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


def _resp(status_code, json_data=None):
    return httpx.Response(
        status_code,
        json=json_data or {},
        request=httpx.Request("POST", razorpayx_client._PAYOUTS_URL),
    )


@pytest.mark.asyncio
async def test_create_payout_retries_transient_then_succeeds():
    """A 503 is retried; the next 200 succeeds (idempotency key makes this safe)."""
    fake = _FakeAsyncClient([_resp(503), _resp(200, {"id": "pout_ok", "status": "processing"})])
    with patch("app.utils.razorpayx_client.httpx.AsyncClient", lambda *a, **k: fake), \
         patch("app.utils.razorpayx_client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await razorpayx_client.create_payout(
            amount_paise=8000, idempotency_key="k", reference_id="r", vpa="x@upi",
        )
    assert result == {"id": "pout_ok", "status": "processing"}
    assert fake.calls == 2
    mock_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_payout_does_not_retry_client_error():
    """A 400 is a real error, not transient — raise immediately, no retry."""
    fake = _FakeAsyncClient([_resp(400)])
    with patch("app.utils.razorpayx_client.httpx.AsyncClient", lambda *a, **k: fake), \
         patch("app.utils.razorpayx_client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(httpx.HTTPStatusError):
            await razorpayx_client.create_payout(
                amount_paise=8000, idempotency_key="k", reference_id="r", vpa="x@upi",
            )
    assert fake.calls == 1
    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_payout_exhausts_retries():
    """Persistent 503s exhaust the retry budget and raise."""
    fake = _FakeAsyncClient([_resp(503), _resp(503), _resp(503)])
    with patch("app.utils.razorpayx_client.httpx.AsyncClient", lambda *a, **k: fake), \
         patch("app.utils.razorpayx_client.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(httpx.HTTPStatusError):
            await razorpayx_client.create_payout(
                amount_paise=8000, idempotency_key="k", reference_id="r", vpa="x@upi",
            )
    assert fake.calls == 3


# ---------------------------------------------------------------------------
# #2 — RazorpayX payout webhook verifies its OWN secret (signature path)
# ---------------------------------------------------------------------------

_RZPX_SECRET = "razorpayx_test_secret"


def _signed_body(payload: dict, secret: str) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, sig


@pytest.mark.asyncio
async def test_payout_webhook_accepts_valid_signature(client, db_session):
    """With RAZORPAYX_WEBHOOK_SECRET set, a correctly-signed payout webhook settles."""
    booking = await _booking_in_processing(db_session, "pout_signed")
    payload = {"event": "payout.processed",
               "payload": {"payout": {"entity": {"id": "pout_signed"}}}}
    raw, sig = _signed_body(payload, _RZPX_SECRET)

    with patch.object(payments_router.settings, "RAZORPAYX_WEBHOOK_SECRET", _RZPX_SECRET):
        resp = await client.post(
            f"{settings.API_V1_PREFIX}/payments/payout-webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
        )
    assert resp.status_code == 200

    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.COMPLETED


@pytest.mark.asyncio
async def test_payout_webhook_rejects_invalid_signature(client, db_session):
    """A bad signature on the payout webhook is rejected (400) and settles nothing."""
    booking = await _booking_in_processing(db_session, "pout_badsig")
    payload = {"event": "payout.processed",
               "payload": {"payout": {"entity": {"id": "pout_badsig"}}}}
    raw, _ = _signed_body(payload, _RZPX_SECRET)

    with patch.object(payments_router.settings, "RAZORPAYX_WEBHOOK_SECRET", _RZPX_SECRET):
        resp = await client.post(
            f"{settings.API_V1_PREFIX}/payments/payout-webhook",
            content=raw,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": "wrong"},
        )
    assert resp.status_code == 400

    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.PROCESSING


# ---------------------------------------------------------------------------
# SVC-137 — reconciliation schema groundwork:
# REVERSED state, nullable admin, source, reference_id (nothing reads these yet)
# ---------------------------------------------------------------------------

def test_settlement_status_has_reversed():
    """REVERSED is a distinct settlement state (completed money clawed back)."""
    assert SettlementStatus.REVERSED.value == "reversed"


@pytest.mark.asyncio
async def test_booking_settlement_status_accepts_reversed(db_session):
    """A booking's settlement_status persists as REVERSED (DB enum accepts it)."""
    consumer = await UserFactory.create(db_session, role="consumer")
    provider = await UserFactory.create(db_session, role="provider")
    booking = await _booking_for(db_session, consumer, provider)

    booking.settlement_status = SettlementStatus.REVERSED
    await db_session.flush()
    await db_session.refresh(booking)
    assert booking.settlement_status == SettlementStatus.REVERSED


@pytest.mark.asyncio
async def test_payout_audit_reconciliation_row_has_no_admin(db_session):
    """A reconciliation audit row persists with a null admin + source='reconciliation'."""
    provider = await UserFactory.create(db_session, role="provider")
    audit = PayoutAudit(
        admin_user_id=None,  # automated action — no human admin
        provider_id=provider.id,
        source="reconciliation",
        amount_paise=8000,
        booking_count=1,
        reference_id="payout_batch_abc",
        outcome="processing",
    )
    db_session.add(audit)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(PayoutAudit).where(PayoutAudit.id == audit.id))
    ).scalar_one()
    assert fetched.admin_user_id is None
    assert fetched.source == "reconciliation"
    assert fetched.reference_id == "payout_batch_abc"


@pytest.mark.asyncio
async def test_payout_audit_source_defaults_to_admin(db_session):
    """An audit row created without an explicit source defaults to 'admin'."""
    provider = await UserFactory.create(db_session, role="provider")
    admin = await UserFactory.create(db_session, role="admin")
    audit = PayoutAudit(
        admin_user_id=admin.id,
        provider_id=provider.id,
        amount_paise=8000,
        booking_count=1,
        outcome="processing",
    )
    db_session.add(audit)
    await db_session.flush()
    await db_session.refresh(audit)
    assert audit.source == "admin"
