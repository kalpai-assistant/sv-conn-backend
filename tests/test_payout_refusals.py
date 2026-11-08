"""A refused payout is a 402 up front, not a 202 "queued" that silently vanishes.

- ``POST /admin/providers/{id}/payout`` runs ``preflight_payout`` (every refusal
  check the task makes, same messages, no locks) before queuing. A refusal is a
  402: nothing queued, no ``admin_actions`` row, no audit row.
- A refusal the task still hits (something changed after the preflight) writes
  one ``refused`` ``payout_audits`` row in its own committed session. ``refused``
  isn't an open attempt, never blocks the next payout, and reconcile ignores it.

The audit writers commit through ``AsyncSessionLocal`` (the app DB). Here that
factory hands back the test session, so the real writer code runs against test
data.
"""

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.exceptions import PayoutRefused
from app.models.admin_action import AdminAction
from app.models.payout_audit import PayoutAudit
from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.settlement_statement import SettlementStatement
from app.services import payout_service as payout_service_module
from app.services.payout_service import PayoutService
from tests.factories.user_factory import UserFactory
from tests.test_cash_netting import _seed_cash_booking
from tests.test_payouts import _booking_for, _provider_with_payout_vpa


@pytest.fixture(autouse=True)
def audits_in_test_session(db_session):
    """Point the audit writers' own session at the test session."""

    class _Session:
        def add(self, obj):
            db_session.add(obj)

        async def execute(self, *args, **kwargs):
            return await db_session.execute(*args, **kwargs)

        async def commit(self):
            await db_session.flush()

    @asynccontextmanager
    async def factory():
        yield _Session()

    with (
        patch("app.services.payout_service.AsyncSessionLocal", factory),
        patch("app.tasks.notification_tasks.send_payout_notification.delay"),
    ):
        yield


@pytest.fixture
def mock_delay():
    fake_task = type("T", (), {"id": "task_refusal"})()
    with patch(
        "app.tasks.payment_tasks.execute_provider_payout.delay", return_value=fake_task
    ) as delay:
        yield delay


async def _people(db):
    consumer = await UserFactory.create(db, role="consumer")
    provider = await UserFactory.create(db, role="provider")
    admin = await UserFactory.create(db, role="admin")
    return consumer, provider, admin


async def _audits(db, provider) -> list[PayoutAudit]:
    return list(
        (
            await db.execute(
                select(PayoutAudit)
                .where(PayoutAudit.provider_id == provider.id)
                .order_by(PayoutAudit.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _admin_action_count(db) -> int:
    return await db.scalar(select(func.count()).select_from(AdminAction))


# Each case stages one reason to refuse; the payout would otherwise go through.


async def _kyc_pending(db, consumer, provider, admin, monkeypatch):
    await _provider_with_payout_vpa(db, provider, kyc_status=KYCStatus.PENDING)
    await _booking_for(db, consumer, provider)


async def _no_payout_details(db, consumer, provider, admin, monkeypatch):
    db.add(ProviderProfile(user_id=provider.id, kyc_status=KYCStatus.VERIFIED))
    await db.flush()
    await _booking_for(db, consumer, provider)


async def _nothing_pending(db, consumer, provider, admin, monkeypatch):
    await _provider_with_payout_vpa(db, provider)


async def _open_attempt(db, consumer, provider, admin, monkeypatch):
    await _provider_with_payout_vpa(db, provider)
    await _booking_for(db, consumer, provider)
    db.add(
        PayoutAudit(
            admin_user_id=admin.id,
            provider_id=provider.id,
            amount_paise=8000,
            booking_count=1,
            reference_id="earlier-batch",
            outcome="unknown",
        )
    )
    await db.flush()


async def _wallet_owes(db, consumer, provider, admin, monkeypatch):
    await _provider_with_payout_vpa(db, provider)
    await _booking_for(db, consumer, provider, provider_payout=10.0)
    await _seed_cash_booking(db, consumer=consumer, provider=provider, platform_commission=50.0)


async def _over_cap(db, consumer, provider, admin, monkeypatch):
    await _provider_with_payout_vpa(db, provider)
    await _booking_for(db, consumer, provider, provider_payout=80.0)
    # The service's own settings object: a test elsewhere clears the
    # get_settings cache, so get_settings() may no longer return it.
    monkeypatch.setattr(payout_service_module.settings, "PAYOUT_MAX_BATCH_PAISE", 5000)


REFUSALS = [
    pytest.param(_kyc_pending, "KYC is not verified", id="kyc"),
    pytest.param(_no_payout_details, "no payout details on file", id="no-details"),
    pytest.param(_nothing_pending, "No pending settlements", id="nothing-pending"),
    pytest.param(_open_attempt, "Previous payout attempt is unresolved", id="open-attempt"),
    pytest.param(_wallet_owes, "owes platform 4000 paise", id="wallet-owes"),
    pytest.param(_over_cap, "exceeds cap of 5000 paise", id="over-cap"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage, message", REFUSALS)
async def test_route_refuses_up_front(
    stage, message, client, db_session, auth_headers, mock_delay, monkeypatch
):
    """Every task-time refusal is now a 402 at the route: nothing queued, no
    admin_actions row, no audit row of its own."""
    consumer, provider, admin = await _people(db_session)
    await stage(db_session, consumer, provider, admin, monkeypatch)
    audits_before = len(await _audits(db_session, provider))
    actions_before = await _admin_action_count(db_session)

    resp = await client.post(
        f"/api/v1/admin/providers/{provider.id}/payout",
        headers=auth_headers(str(admin.id), "admin"),
    )

    assert resp.status_code == 402, resp.text
    assert message in resp.json()["message"]
    mock_delay.assert_not_called()
    assert await _admin_action_count(db_session) == actions_before
    assert len(await _audits(db_session, provider)) == audits_before


@pytest.mark.asyncio
@pytest.mark.parametrize("stage, message", REFUSALS)
async def test_task_refusal_writes_one_refused_audit(stage, message, db_session, monkeypatch):
    """The same refusal hit inside the task leaves a ``refused`` audit row,
    where before the rollback left nothing at all."""
    consumer, provider, admin = await _people(db_session)
    await stage(db_session, consumer, provider, admin, monkeypatch)
    before = {a.id for a in await _audits(db_session, provider)}

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as rx:
        with pytest.raises(PayoutRefused, match=message):
            await PayoutService(db_session).create_provider_payout(provider.id, admin.id)
    rx.assert_not_called()

    new = [a for a in await _audits(db_session, provider) if a.id not in before]
    assert len(new) == 1
    refused = new[0]
    assert refused.outcome == "refused"
    assert refused.source == "admin"
    assert refused.admin_user_id == admin.id
    assert message in refused.error
    assert refused.reference_id is None
    assert refused.payout_id is None


@pytest.mark.asyncio
async def test_refused_audit_carries_amount_and_count_when_known(db_session, monkeypatch):
    consumer, provider, admin = await _people(db_session)
    await _over_cap(db_session, consumer, provider, admin, monkeypatch)

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as rx:
        with pytest.raises(PayoutRefused):
            await PayoutService(db_session).create_provider_payout(provider.id, admin.id)
    rx.assert_not_called()

    (refused,) = await _audits(db_session, provider)
    assert (refused.amount_paise, refused.booking_count) == (8000, 1)


@pytest.mark.asyncio
async def test_refused_audit_does_not_block_the_next_payout(db_session):
    """A ``refused`` row is not an open attempt: once the cause is fixed, the
    next payout goes through."""
    consumer, provider, admin = await _people(db_session)
    profile = await _provider_with_payout_vpa(db_session, provider, kyc_status=KYCStatus.PENDING)
    await _booking_for(db_session, consumer, provider, provider_payout=80.0)

    service = PayoutService(db_session)
    with pytest.raises(PayoutRefused, match="KYC"):
        await service.create_provider_payout(provider.id, admin.id)
    assert await service._find_open_attempt(provider.id) is None

    profile.kyc_status = KYCStatus.VERIFIED
    await db_session.flush()

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as rx:
        rx.return_value = {"id": "pout_after_refusal", "status": "processing"}
        result = await service.create_provider_payout(provider.id, admin.id)

    rx.assert_called_once()
    assert result["payout_id"] == "pout_after_refusal"
    outcomes = [a.outcome for a in await _audits(db_session, provider)]
    assert outcomes == ["refused", "processing"]


@pytest.mark.asyncio
async def test_reconcile_ignores_refused_audits(db_session):
    """Reconcile maps a RazorpayX payout to an audit by ``reference_id``; a
    refused row has none, so even a payout with no reference leaves it alone."""
    consumer, provider, admin = await _people(db_session)
    await _provider_with_payout_vpa(db_session, provider, kyc_status=KYCStatus.REJECTED)
    await _booking_for(db_session, consumer, provider)
    with pytest.raises(PayoutRefused):
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    stray = [
        {"id": "pout_stray_1", "status": "processed", "amount": 0, "reference_id": None},
        {"id": "pout_stray_2", "status": "failed", "amount": 0, "reference_id": ""},
    ]
    with patch("app.services.payout_service.list_payouts", new_callable=AsyncMock) as lp:
        lp.return_value = stray
        report = await PayoutService(db_session).reconcile_payouts(since_ts=0, until_ts=1)

    assert report["case_b"] == 0
    (refused,) = await _audits(db_session, provider)
    assert (refused.outcome, refused.payout_id) == ("refused", None)


@pytest.mark.asyncio
async def test_failed_refused_write_is_logged_and_the_refusal_still_raised(db_session, caplog):
    consumer, provider, admin = await _people(db_session)
    await _provider_with_payout_vpa(db_session, provider, kyc_status=KYCStatus.PENDING)

    @asynccontextmanager
    async def broken():
        raise RuntimeError("audit DB down")
        yield  # pragma: no cover

    caplog.set_level(logging.ERROR, logger="app.services.payout_service")
    with patch("app.services.payout_service.AsyncSessionLocal", broken):
        with pytest.raises(PayoutRefused, match="KYC"):
            await PayoutService(db_session).create_provider_payout(provider.id, admin.id)

    assert "payout.refused_audit_write_failed" in caplog.text


@pytest.mark.asyncio
async def test_wallet_owes_writes_no_statement_on_either_path(
    client, db_session, auth_headers, mock_delay, monkeypatch
):
    """The wallet ≤ 0 refusal saves no settlement statement, at the route or in
    the task, and its message no longer claims one was generated."""
    consumer, provider, admin = await _people(db_session)
    await _wallet_owes(db_session, consumer, provider, admin, monkeypatch)

    resp = await client.post(
        f"/api/v1/admin/providers/{provider.id}/payout",
        headers=auth_headers(str(admin.id), "admin"),
    )
    assert resp.status_code == 402
    assert "statement" not in resp.json()["message"].lower()

    with pytest.raises(PayoutRefused) as exc_info:
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)
    assert "statement" not in exc_info.value.message.lower()

    count = await db_session.scalar(
        select(func.count())
        .select_from(SettlementStatement)
        .where(SettlementStatement.provider_id == provider.id)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_route_amount_is_what_the_task_sends(
    client, db_session, auth_headers, mock_delay, monkeypatch
):
    """The 202's amount is min(wallet, batch): here cash commission nets the
    wallet below the batch sum, and the queued figure matches the wire amount."""
    # An accepted payout commits its admin_actions row before queuing (SVC-423).
    # A real commit here would leave owed bookings behind for other files'
    # pending-settlements tests, so it flushes into the test session instead.
    monkeypatch.setattr(db_session, "commit", db_session.flush)
    consumer, provider, admin = await _people(db_session)
    await _provider_with_payout_vpa(db_session, provider)
    await _booking_for(db_session, consumer, provider, provider_payout=80.0)
    await _seed_cash_booking(
        db_session, consumer=consumer, provider=provider, platform_commission=20.0
    )

    resp = await client.post(
        f"/api/v1/admin/providers/{provider.id}/payout",
        headers=auth_headers(str(admin.id), "admin"),
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["status"], body["amount_paise"], body["booking_count"]) == ("queued", 6000, 1)
    mock_delay.assert_called_once_with(str(provider.id), str(admin.id))

    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as rx:
        rx.return_value = {"id": "pout_net", "status": "processing"}
        await PayoutService(db_session).create_provider_payout(provider.id, admin.id)
    assert rx.call_args.kwargs["amount_paise"] == body["amount_paise"]
