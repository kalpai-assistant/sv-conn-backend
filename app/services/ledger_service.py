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

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def wallet_balance_paise(db: AsyncSession, provider_id: UUID) -> int:
    """Live wallet balance for a provider.

    Positive = platform owes them; negative = they owe us (dispute loss,
    unbalanced cash commission after SVC-180). Uses ``COALESCE`` so a
    provider with no entries reads 0 rather than None.

    Consumers should treat the returned int as authoritative; it is the
    single source of truth for "what is this provider owed" now that the
    wallet exists.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(ProviderLedgerEntry.amount_paise), 0)).where(
            ProviderLedgerEntry.provider_id == provider_id
        )
    )
    return int(result.scalar_one())


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
