"""
Provider wallet ledger — append-only, one row per money event.

Introduced by SVC-193 to resolve §11 Q2 of PAYMENT_FLOW_v1.1.md: "build the
ledger" (over the alternative of deriving 'owed' from
``bookings.provider_payout WHERE settlement_status=PENDING``). The derived
formula is fine for the happy path but has no coherent place to land the four
non-happy events: refund debits (SVC-178), dispute-loss clawbacks (SVC-193 wires
this), cash-commission debits (SVC-180), and post-completion tips (already
handled via ``booking.tip_amount`` — no ledger entry needed since Q4 landed on
"defer to payout" and the payout math already sums tips).

**Wallet balance = SUM(amount_paise) WHERE provider_id = ?**

- Positive amount = credit (platform owes provider more).
- Negative amount = debit (platform owes provider less, or provider owes
  platform when the balance goes negative).

Append-only: rows are never updated or deleted, so the balance at any point
in time can be reconstructed by summing entries created up to that time. That
property matters for reconciliation (SVC-193/§8) and for the settlement
statement (SVC-180) which needs a per-payout view of the delta since the last
payout.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now_utc() -> datetime:
    """Distinct wall-clock timestamp per row.

    SVC-180 windows statement periods on ``created_at > previous_statement.created_at``.
    Postgres ``NOW()`` returns transaction-start time, so multiple rows
    written inside one transaction share a timestamp and windowing misses
    them. Client-side ``datetime.now(UTC)`` gives microsecond-distinct
    values per call, matching what production request boundaries would
    produce naturally.
    """
    return datetime.now(UTC)


class LedgerEntryType(str, enum.Enum):
    """What kind of event this entry represents.

    Kept as an enum column (not free text) so a bad value is caught at write
    time and the settlement statement can render each type differently
    without stringly-typed switches everywhere.
    """

    # Credit: booking completed online, provider is owed their payout share.
    # Not written for cash bookings — the provider collected at the door.
    BOOKING_COMPLETED = "booking_completed"

    # Debit: RazorpayX payout succeeded, provider was paid.
    PAYOUT = "payout"

    # Debit: chargeback lost. The provider had already been paid; wallet
    # goes negative until the next credit or the balance is cleared out of
    # band.
    DISPUTE_LOSS = "dispute_loss"

    # Debit: 20% platform commission owed on a completed cash booking. Netted
    # against future online payouts (SVC-180). Not written by SVC-193 — the
    # writer lives in SVC-180. Present in the enum here so both tickets agree
    # on the vocabulary and the DB enum type doesn't need an ALTER on SVC-180.
    CASH_COMMISSION = "cash_commission"

    # Debit: consumer was refunded, provider's owed amount for that booking
    # is reversed. Written by SVC-178. Same reasoning as CASH_COMMISSION —
    # enum value declared here so SVC-178 doesn't need a schema change.
    REFUND = "refund"


class LedgerReferenceType(str, enum.Enum):
    """What kind of row the ``reference_id`` points at.

    Loose FK: kept in-column rather than a hard FK because different types
    resolve to different tables. Reconciliation and the settlement statement
    join on ``(reference_type, reference_id)`` — same pattern as an audit
    log's ``target_type/target_id``.
    """

    BOOKING = "booking"
    PAYOUT_AUDIT = "payout_audit"
    PAYMENT = "payment"


class ProviderLedgerEntry(Base):
    """One money event on a provider's wallet.

    Never updated, never deleted. Balance is a live SUM.
    """

    __tablename__ = "provider_ledger_entries"

    __table_args__ = (
        # The one query we actually run against this table:
        # SUM(amount_paise) WHERE provider_id = ?
        # and the settlement-statement query which reads entries by provider,
        # newest-first. Composite index serves both.
        Index("idx_provider_ledger_provider_created", "provider_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # BigInteger because a busy provider's lifetime ledger balance can exceed
    # int32 (~₹21 crore). Cheap to widen now, painful later.
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_type: Mapped[LedgerEntryType] = mapped_column(Enum(LedgerEntryType), nullable=False)
    # Loose FK: nullable because a hand-inserted correction (rare, admin
    # only) may not have a well-formed reference.
    reference_type: Mapped[LedgerReferenceType | None] = mapped_column(Enum(LedgerReferenceType))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Free-text hint for operator triage. Never machine-parsed — every rule
    # that needs to distinguish entries reads ``entry_type`` instead.
    note: Mapped[str | None] = mapped_column(String(500))
    # Python-side default (see `_now_utc` docstring): SVC-180 statement
    # windowing needs distinct timestamps per row, which Postgres NOW()
    # can't provide inside a single transaction. `server_default` kept as
    # a belt-and-braces for any direct SQL insert that bypasses the ORM.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        server_default=func.now(),
    )
