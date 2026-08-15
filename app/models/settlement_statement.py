"""
Per-payout settlement statement — the "invoice document" from §11 Q5.

One row per payout attempt (whether money actually moved or not), summarising
the four numbers a provider needs to reconcile their settlement:

- ``gross_credit_paise`` — sum of BOOKING_COMPLETED credits since the last
  statement.
- ``cash_commission_deducted_paise`` — sum of CASH_COMMISSION debits since
  the last statement (positive number; the entry itself is negative on the
  ledger).
- ``other_adjustments_paise`` — everything else (dispute-loss, refund
  debits). Kept as a single lump for beta; can be broken out per-type if
  operations asks.
- ``net_paid_paise`` — what actually left our account this cycle.
- ``platform_balance_owed_paise`` — positive when the provider owes the
  platform (cash commission > online earnings for the period). This is what
  a real invoice would ask them to pay.

Rows are append-only. Statements are the source of truth for "here is what
we told the provider on Xth of the month" — the ledger is the source of
truth for the running balance, but the statement is the snapshot the
provider actually saw.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.provider_ledger_entry import _now_utc


class SettlementStatement(Base):
    """One row per payout attempt (see PayoutService.create_provider_payout).

    ``payout_audit_id`` is nullable because a payout that resolved to
    "provider owes platform more than the online balance" produces a
    statement but no RazorpayX call (and therefore no audit row) — the
    statement itself is the artifact.
    """

    __tablename__ = "settlement_statements"

    __table_args__ = (
        # Read pattern: latest N statements for a provider, on the admin
        # payouts screen and in the "since last statement" window query.
        Index(
            "idx_settlement_statements_provider_created",
            "provider_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # NULL when no payout fired this cycle (provider owed us more than we
    # owed them). The statement still exists so the provider gets a
    # continuous artifact stream.
    #
    # Loose reference (no FK constraint): audits live in a separate write
    # path (see PayoutService._record_attempting_audit — its own committed
    # transaction), and a hard FK would prevent the statement from being
    # written when an audit was recorded then later cleaned up (test
    # isolation, admin data ops). The join is done in application code
    # when needed.
    payout_audit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Composition of the period. All BigInteger paise, all non-negative
    # (signs are absorbed into the specific columns — e.g. deducted is
    # positive, not negative).
    gross_credit_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cash_commission_deducted_paise: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    other_adjustments_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Outcome of the period. Exactly one is non-zero at a time.
    net_paid_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    platform_balance_owed_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Python-side default so statements written in one transaction get
    # distinct timestamps — the "windowed sums since previous statement"
    # query depends on it. See _now_utc for the full rationale.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        server_default=func.now(),
    )
