"""Wallet fix: a payout's debit comes back when its bookings are owed again, and a
reconciled orphan that completed gets the debit it never had.

- payout.rejected → PENDING, and requeue of FAILED/REVERSED → PENDING each write
  one PAYOUT_RETURNED credit per payout, as a DB guarantee (partial unique index).
- payout.failed / payout.reversed themselves credit nothing, so the admin requeue
  stays the only way those bookings become payable again.
- No PAYOUT debit (rejected before acceptance, an ``unknown`` outcome, a FAILED
  orphan) means no credit.
- A Case B orphan re-linked as COMPLETED gets its PAYOUT debit, once.

The audit writers normally commit through the app DB, so here they write into the
test session instead: the credit finds its debit through ``payout_audits.payout_id``.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.exceptions import PaymentError
from app.models.booking import SettlementStatus
from app.models.payout_audit import PayoutAudit
from app.models.provider_ledger_entry import (
    LedgerEntryType,
    LedgerReferenceType,
    ProviderLedgerEntry,
)
from app.services.ledger_service import credit_payout_returned, wallet_balance_paise
from app.services.payout_service import PayoutService
from tests.factories.user_factory import UserFactory
from tests.test_payouts import _booking_for, _provider_with_payout_vpa


@pytest.fixture(autouse=True)
def in_session_audits(db_session):
    async def record(admin_user_id, provider_id, amount_paise, booking_count, reference_id):
        existing = await db_session.scalar(
            select(PayoutAudit.id).where(PayoutAudit.reference_id == reference_id)
        )
        if existing is not None:
            return existing
        audit = PayoutAudit(
            admin_user_id=admin_user_id,
            provider_id=provider_id,
            amount_paise=amount_paise,
            booking_count=booking_count,
            reference_id=reference_id,
            outcome="attempting",
        )
        db_session.add(audit)
        await db_session.flush()
        return audit.id

    async def resolve(audit_id, *, outcome, payout_id=None, error=None):
        await db_session.execute(
            update(PayoutAudit)
            .where(PayoutAudit.id == audit_id)
            .values(outcome=outcome, payout_id=payout_id, error=error)
        )

    with (
        patch.object(PayoutService, "_record_attempting_audit", staticmethod(record)),
        patch.object(PayoutService, "_resolve_audit", staticmethod(resolve)),
        patch.object(PayoutService, "_record_refused_audit", new_callable=AsyncMock),
        patch("app.tasks.notification_tasks.send_payout_notification.delay"),
    ):
        yield


async def _sp(db):
    consumer = await UserFactory.create(db, role="consumer")
    provider = await UserFactory.create(db, role="provider")
    admin = await UserFactory.create(db, role="admin")
    await _provider_with_payout_vpa(db, provider)
    return consumer, provider, admin


async def _pay(db, provider, admin, payout_id: str) -> int:
    """Run a payout as RazorpayX accepting it; return the amount sent."""
    with patch("app.services.payout_service.create_payout", new_callable=AsyncMock) as rx:
        rx.return_value = {"id": payout_id, "status": "processing"}
        await PayoutService(db).create_provider_payout(provider.id, admin.id)
    return rx.call_args.kwargs["amount_paise"]


async def _webhook(db, event: str, payout_id: str) -> None:
    entity = {"id": payout_id, "status_details": {"description": "test reason"}}
    await PayoutService(db).handle_payout_webhook(event, {"payout": {"entity": entity}})


async def _entries(db, provider, entry_type) -> list[ProviderLedgerEntry]:
    return list(
        (
            await db.execute(
                select(ProviderLedgerEntry).where(
                    ProviderLedgerEntry.provider_id == provider.id,
                    ProviderLedgerEntry.entry_type == entry_type,
                )
            )
        )
        .scalars()
        .all()
    )


async def _audit_id(db, payout_id: str) -> uuid.UUID:
    return await db.scalar(select(PayoutAudit.id).where(PayoutAudit.payout_id == payout_id))


RETURNED = LedgerEntryType.PAYOUT_RETURNED


# --- payout.rejected -----------------------------------------------------------------


async def test_rejected_credits_back_once_and_the_next_payout_pays_in_full(db_session):
    consumer, provider, admin = await _sp(db_session)
    booking = await _booking_for(db_session, consumer, provider)
    assert await wallet_balance_paise(db_session, provider.id) == 8000

    assert await _pay(db_session, provider, admin, "pout_rej1") == 8000
    assert await wallet_balance_paise(db_session, provider.id) == 0

    await _webhook(db_session, "payout.rejected", "pout_rej1")
    await db_session.refresh(booking)
    assert booking.settlement_status is SettlementStatus.PENDING
    [credit] = await _entries(db_session, provider, RETURNED)
    [debit] = await _entries(db_session, provider, LedgerEntryType.PAYOUT)
    assert credit.amount_paise == 8000 == -debit.amount_paise
    assert (credit.reference_type, credit.reference_id) == (
        LedgerReferenceType.LEDGER_ENTRY,
        debit.id,
    )
    assert debit.reference_id == await _audit_id(db_session, "pout_rej1")
    assert await wallet_balance_paise(db_session, provider.id) == 8000

    # A redelivered webhook writes nothing more.
    await _webhook(db_session, "payout.rejected", "pout_rej1")
    assert len(await _entries(db_session, provider, RETURNED)) == 1

    # Before the fix the wallet stayed at 0 here and this raised "owes platform".
    assert await _pay(db_session, provider, admin, "pout_rej2") == 8000
    assert await wallet_balance_paise(db_session, provider.id) == 0


async def test_a_retried_batch_rejected_twice_is_credited_twice(db_session):
    """The same bookings retried reuse one payout audit; each debit still comes back."""
    consumer, provider, admin = await _sp(db_session)
    await _booking_for(db_session, consumer, provider)
    await _pay(db_session, provider, admin, "pout_try1")
    await _webhook(db_session, "payout.rejected", "pout_try1")
    await _pay(db_session, provider, admin, "pout_try2")
    await _webhook(db_session, "payout.rejected", "pout_try2")

    audits = (
        (
            await db_session.execute(
                select(PayoutAudit).where(PayoutAudit.provider_id == provider.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1  # reused, so keying credits on the audit would miss one
    assert len(await _entries(db_session, provider, LedgerEntryType.PAYOUT)) == 2
    assert len(await _entries(db_session, provider, RETURNED)) == 2
    assert await wallet_balance_paise(db_session, provider.id) == 8000
    assert await _pay(db_session, provider, admin, "pout_try3") == 8000


# --- failed / reversed: nothing until the admin requeues ------------------------------


async def test_failed_credits_nothing_until_requeue_then_once_per_payout(db_session):
    consumer, provider, admin = await _sp(db_session)
    first = await _booking_for(db_session, consumer, provider)
    await _pay(db_session, provider, admin, "pout_fa")
    await _webhook(db_session, "payout.failed", "pout_fa")
    second = await _booking_for(db_session, consumer, provider, provider_payout=50.0)
    await _pay(db_session, provider, admin, "pout_fb")
    await _webhook(db_session, "payout.failed", "pout_fb")
    for b in (first, second):
        await db_session.refresh(b)
        assert b.settlement_status is SettlementStatus.FAILED

    # The failed webhook alone gives nothing back: no requeue, no payable money.
    assert await _entries(db_session, provider, RETURNED) == []
    assert await wallet_balance_paise(db_session, provider.id) == 0

    await PayoutService(db_session).requeue_settlements(
        provider.id, admin.id, from_status=SettlementStatus.FAILED
    )
    credits = await _entries(db_session, provider, RETURNED)
    assert sorted(c.amount_paise for c in credits) == [5000, 8000]
    debits = await _entries(db_session, provider, LedgerEntryType.PAYOUT)
    assert {c.reference_id for c in credits} == {d.id for d in debits}

    # Replaying the credit for the same payouts is a no-op.
    assert await credit_payout_returned(db_session, ["pout_fa", "pout_fb"]) == 0
    with pytest.raises(PaymentError):  # nothing left to requeue
        await PayoutService(db_session).requeue_settlements(
            provider.id, admin.id, from_status=SettlementStatus.FAILED
        )
    assert len(await _entries(db_session, provider, RETURNED)) == 2

    # fail → requeue → pay again sends the full amount owed.
    assert await _pay(db_session, provider, admin, "pout_fc") == 13000
    assert await wallet_balance_paise(db_session, provider.id) == 0


async def test_reversed_credits_only_on_requeue(db_session):
    consumer, provider, admin = await _sp(db_session)
    booking = await _booking_for(db_session, consumer, provider)
    await _pay(db_session, provider, admin, "pout_rv")
    await _webhook(db_session, "payout.processed", "pout_rv")
    await _webhook(db_session, "payout.reversed", "pout_rv")
    await db_session.refresh(booking)
    assert booking.settlement_status is SettlementStatus.REVERSED
    assert await _entries(db_session, provider, RETURNED) == []

    await PayoutService(db_session).requeue_settlements(
        provider.id, admin.id, from_status=SettlementStatus.REVERSED
    )
    [credit] = await _entries(db_session, provider, RETURNED)
    assert credit.amount_paise == 8000
    assert await _pay(db_session, provider, admin, "pout_rv2") == 8000


async def test_failed_then_reversed_for_one_payout_credits_once(db_session):
    consumer, provider, admin = await _sp(db_session)
    await _booking_for(db_session, consumer, provider)
    await _pay(db_session, provider, admin, "pout_fr")
    await _webhook(db_session, "payout.failed", "pout_fr")
    await PayoutService(db_session).requeue_settlements(
        provider.id, admin.id, from_status=SettlementStatus.FAILED
    )
    await _webhook(db_session, "payout.reversed", "pout_fr")
    await _webhook(db_session, "payout.rejected", "pout_fr")
    assert len(await _entries(db_session, provider, RETURNED)) == 1


# --- no debit, no credit --------------------------------------------------------------


async def test_a_payout_with_no_debit_gets_no_credit(db_session):
    consumer, provider, admin = await _sp(db_session)
    # An attempt whose outcome is unknown: the audit has a payout id, no debit.
    db_session.add(
        PayoutAudit(
            admin_user_id=admin.id,
            provider_id=provider.id,
            amount_paise=8000,
            booking_count=1,
            reference_id=f"ref-{uuid.uuid4().hex}",
            payout_id="pout_nodebit",
            outcome="unknown",
        )
    )
    await db_session.flush()

    await _webhook(db_session, "payout.rejected", "pout_nodebit")
    await _webhook(db_session, "payout.rejected", "pout_never_seen")
    assert await credit_payout_returned(db_session, ["pout_nodebit", None]) == 0
    assert await credit_payout_returned(db_session, []) == 0
    assert await _entries(db_session, provider, RETURNED) == []


async def test_the_index_refuses_a_second_credit_for_one_audit(db_session):
    _, provider, _ = await _sp(db_session)
    ref = uuid.uuid4()
    for _ in range(2):
        db_session.add(
            ProviderLedgerEntry(
                provider_id=provider.id,
                amount_paise=100,
                entry_type=RETURNED,
                reference_type=LedgerReferenceType.PAYOUT_AUDIT,
                reference_id=ref,
            )
        )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.flush()


# --- reconcile Case B orphans ---------------------------------------------------------


async def _orphan(db, consumer, provider):
    """A batch RazorpayX accepted while our create path raised: PENDING bookings, an
    ``unknown`` audit with the batch key, and no PAYOUT debit."""
    booking = await _booking_for(db, consumer, provider)
    key = PayoutService._batch_idempotency_key([booking])
    audit = PayoutAudit(
        provider_id=provider.id,
        amount_paise=8000,
        booking_count=1,
        reference_id=key,
        outcome="unknown",
    )
    db.add(audit)
    await db.flush()
    return booking, key, audit


def _list(payouts):
    return patch(
        "app.services.payout_service.list_payouts", new_callable=AsyncMock, return_value=payouts
    )


async def test_orphan_completed_gets_its_debit_once(db_session):
    consumer, provider, admin = await _sp(db_session)
    booking, key, audit = await _orphan(db_session, consumer, provider)
    assert await wallet_balance_paise(db_session, provider.id) == 8000

    rx = [{"id": "pout_orph", "status": "processed", "amount": 8000, "reference_id": key}]
    with _list(rx):
        first = await PayoutService(db_session).reconcile_payouts(since_ts=0, until_ts=1)
        await PayoutService(db_session).reconcile_payouts(since_ts=0, until_ts=1)
    assert first["case_b"] == 1
    await db_session.refresh(booking)
    assert booking.settlement_status is SettlementStatus.COMPLETED

    [debit] = await _entries(db_session, provider, LedgerEntryType.PAYOUT)
    assert (debit.amount_paise, debit.reference_id) == (-8000, audit.id)
    # The wallet no longer holds the paid-out 8000.
    assert await wallet_balance_paise(db_session, provider.id) == 0

    await _booking_for(db_session, consumer, provider, provider_payout=50.0)
    assert await wallet_balance_paise(db_session, provider.id) == 5000
    assert await _pay(db_session, provider, admin, "pout_next") == 5000


async def test_orphan_failed_gets_no_debit_and_its_requeue_no_credit(db_session):
    consumer, provider, admin = await _sp(db_session)
    booking, key, _ = await _orphan(db_session, consumer, provider)

    rx = [{"id": "pout_orph_f", "status": "failed", "amount": 8000, "reference_id": key}]
    with _list(rx):
        await PayoutService(db_session).reconcile_payouts(since_ts=0, until_ts=1)
    await db_session.refresh(booking)
    assert booking.settlement_status is SettlementStatus.FAILED
    assert await _entries(db_session, provider, LedgerEntryType.PAYOUT) == []

    await PayoutService(db_session).requeue_settlements(
        provider.id, admin.id, from_status=SettlementStatus.FAILED
    )
    assert await _entries(db_session, provider, RETURNED) == []
    assert await wallet_balance_paise(db_session, provider.id) == 8000
    assert await _pay(db_session, provider, admin, "pout_orph_f2") == 8000
