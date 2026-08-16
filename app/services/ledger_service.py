"""
Provider wallet ledger writes.

Single-purpose module — three functions, each appending one row to
``provider_ledger_entries`` for a specific event. Kept together (rather than
inlined at each write site) so the shape of a ledger entry, the sign
convention, and the mapping from domain event → ``entry_type`` all live in
one file. Adding a new event kind (SVC-178 refund, SVC-180 cash commission)
extends this module with one more function.

**Sign convention:**
- Positive ``amount_paise`` = credit — platform owes the provider more.
- Negative ``amount_paise`` = debit — platform owes less (or provider owes us
  when the balance goes negative).

Every write runs inside the caller's DB transaction — commit is the caller's
responsibility, matching every other service in this codebase. That means a
booking completion + its ledger credit land atomically: a crash between the
two would roll both back, not leave one orphaned.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.provider_ledger_entry import (
    LedgerEntryType,
    LedgerReferenceType,
    ProviderLedgerEntry,
)


async def credit_booking_completed(
    db: AsyncSession,
    *,
    provider_id: UUID,
    booking_id: UUID,
    amount_paise: int,
    note: str | None = None,
) -> ProviderLedgerEntry:
    """A completed online booking. Credit the provider for their payout share.

    Only called for online bookings — cash bookings don't credit the wallet
    (the provider collected at the door). Amount here is
    ``(provider_payout + tip_amount) * 100`` per the SVC-193 backfill
    convention; the caller does the rupees→paise conversion because it also
    owns the split.
    """
    return await _append(
        db,
        provider_id=provider_id,
        amount_paise=amount_paise,  # positive credit
        entry_type=LedgerEntryType.BOOKING_COMPLETED,
        reference_type=LedgerReferenceType.BOOKING,
        reference_id=booking_id,
        note=note,
    )


async def debit_payout(
    db: AsyncSession,
    *,
    provider_id: UUID,
    payout_audit_id: UUID,
    amount_paise: int,
    note: str | None = None,
) -> ProviderLedgerEntry:
    """A successful RazorpayX payout. Debit the wallet by the amount sent.

    Called after ``create_payout`` returns — the payout has been accepted at
    RazorpayX and the audit row is being resolved to a positive outcome.
    The failure path (audit outcome = ``failed`` / ``unknown``) does not
    debit: money hasn't left, or we don't know it has.
    """
    return await _append(
        db,
        provider_id=provider_id,
        amount_paise=-abs(amount_paise),  # normalise: always debit here
        entry_type=LedgerEntryType.PAYOUT,
        reference_type=LedgerReferenceType.PAYOUT_AUDIT,
        reference_id=payout_audit_id,
        note=note,
    )


async def debit_dispute_loss(
    db: AsyncSession,
    *,
    provider_id: UUID,
    payment_id: UUID,
    amount_paise: int,
    note: str | None = None,
) -> ProviderLedgerEntry:
    """A lost chargeback. Claw back the provider's share of the disputed booking.

    Called from ``PaymentService._handle_dispute`` on ``payment.dispute.lost``.
    Provider was already paid out — this walks the wallet down (potentially
    negative). An admin resolves the negative balance out of band for beta;
    the auto-clawback-from-next-payout flow lands under SVC-180.
    """
    return await _append(
        db,
        provider_id=provider_id,
        amount_paise=-abs(amount_paise),
        entry_type=LedgerEntryType.DISPUTE_LOSS,
        reference_type=LedgerReferenceType.PAYMENT,
        reference_id=payment_id,
        note=note,
    )


async def statement_window_sum_paise(
    db: AsyncSession,
    *,
    provider_id: UUID,
    entry_types: list[LedgerEntryType],
    since: datetime | None,
) -> int:
    """SVC-180: sum entries of the given types for a provider, since ``since``.

    Used to compose the settlement statement's per-period breakdown
    (BOOKING_COMPLETED credits, CASH_COMMISSION debits, other adjustments).
    ``since`` is the previous statement's ``created_at`` — for a provider's
    first statement it is ``None`` and the window is all of history. Returned
    as a signed int; callers absorb the sign into the specific column (e.g.
    ``cash_commission_deducted_paise`` is stored positive but ``entry_type ==
    CASH_COMMISSION`` sums come back negative).
    """
    stmt = select(func.coalesce(func.sum(ProviderLedgerEntry.amount_paise), 0)).where(
        ProviderLedgerEntry.provider_id == provider_id,
        ProviderLedgerEntry.entry_type.in_(entry_types),
    )
    if since is not None:
        stmt = stmt.where(ProviderLedgerEntry.created_at > since)
    return int((await db.execute(stmt)).scalar_one())


async def wallet_balance_paise(db: AsyncSession, provider_id: UUID) -> int:
    """Live wallet balance for a provider, gated on T+1 settlement (SVC-177).

    Positive = platform owes them; negative = they owe us (dispute loss,
    unbalanced cash commission after SVC-180).

    **T+1 gate**: BOOKING_COMPLETED credits whose backing booking has
    ``settled_at IS NULL`` are excluded — Razorpay hasn't deposited that
    money in our account yet, and paying a provider from platform float
    breaks the RBI PA rule (see PAYMENT_FLOW_v1.1.md §4). All other entry
    types count immediately: PAYOUT debits (money we already sent),
    CASH_COMMISSION debits (owed to platform regardless of Razorpay
    settlement), DISPUTE_LOSS (already-lost money we're clawing back),
    REFUND (already-refunded money).

    Two queries — one for the non-credit rows and one for the
    settlement-gated credits — rather than a single query with a
    conditional sum, because ``settled_at`` lives on ``bookings`` and
    reaching it needs a join only for the BOOKING_COMPLETED subset.
    Both are cheap: `(provider_id, created_at)` index + small per-provider
    row counts at beta scale.
    """
    from app.models.booking import Booking

    # Non-credit entries: sum unconditionally.
    non_credit_sum = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(ProviderLedgerEntry.amount_paise), 0)).where(
                    ProviderLedgerEntry.provider_id == provider_id,
                    ProviderLedgerEntry.entry_type != LedgerEntryType.BOOKING_COMPLETED,
                )
            )
        ).scalar_one()
    )
    # BOOKING_COMPLETED credits: only count those whose backing booking has
    # settled. Join on reference_id → bookings.id (BOOKING_COMPLETED entries
    # always have reference_type = BOOKING).
    settled_credit_sum = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(ProviderLedgerEntry.amount_paise), 0))
                .select_from(ProviderLedgerEntry)
                .join(Booking, Booking.id == ProviderLedgerEntry.reference_id)
                .where(
                    ProviderLedgerEntry.provider_id == provider_id,
                    ProviderLedgerEntry.entry_type == LedgerEntryType.BOOKING_COMPLETED,
                    Booking.settled_at.is_not(None),
                )
            )
        ).scalar_one()
    )
    return non_credit_sum + settled_credit_sum


async def _append(
    db: AsyncSession,
    *,
    provider_id: UUID,
    amount_paise: int,
    entry_type: LedgerEntryType,
    reference_type: LedgerReferenceType | None,
    reference_id: UUID | None,
    note: str | None,
) -> ProviderLedgerEntry:
    entry = ProviderLedgerEntry(
        provider_id=provider_id,
        amount_paise=amount_paise,
        entry_type=entry_type,
        reference_type=reference_type,
        reference_id=reference_id,
        note=note,
    )
    db.add(entry)
    await db.flush()
    return entry


# ---------------------------------------------------------------------------
# Sync variants — for Celery tasks that use SyncSessionLocal.
#
# Celery workers run against a sync session (see app.database.SyncSessionLocal
# and the reasoning in payment_tasks.process_cash_payment). Async helpers
# above can't be awaited from that context, so we mirror the small subset of
# writers those tasks need. Kept as a thin duplicate of `_append`'s shape
# rather than a helper that dispatches on session type, so the sync tests
# stay simple and there is no hidden async smuggled into a sync task.
# ---------------------------------------------------------------------------


def sync_debit_cash_commission(
    db: Session,
    *,
    provider_id: UUID,
    booking_id: UUID,
    amount_paise: int,
    note: str | None = None,
) -> ProviderLedgerEntry:
    """SVC-180: cash-commission debit written from `process_cash_payment`.

    Cash booking: the provider collected the full amount at the door and
    owes the platform its 20% commission. This is the debit side of the
    settlement — it will be netted against future online payouts (see
    ``PayoutService.create_provider_payout``).

    Sync twin of ``debit_dispute_loss`` shaped for the Celery worker's
    session. Rides the task's commit — Celery tasks own their transaction,
    so this returns after `add` + `flush` (like the async helpers) and lets
    the caller commit as part of its own success path.
    """
    entry = ProviderLedgerEntry(
        provider_id=provider_id,
        amount_paise=-abs(amount_paise),
        entry_type=LedgerEntryType.CASH_COMMISSION,
        reference_type=LedgerReferenceType.BOOKING,
        reference_id=booking_id,
        note=note,
    )
    db.add(entry)
    db.flush()
    return entry
