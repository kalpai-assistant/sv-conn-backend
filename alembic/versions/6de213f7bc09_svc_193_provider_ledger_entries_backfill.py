"""svc-193: provider_ledger_entries + backfill

Revision ID: 6de213f7bc09
Revises: 5c902ec6a57e
Create Date: 2026-08-14 17:23:31.642077

SVC-193 introduces the provider wallet ledger — append-only, signed rows
against which every subsequent money-touching ticket (refunds, cash netting,
T+1 gate, dispute clawbacks, collect reconcile) will land.

Backfill strategy:
- One CREDIT (`booking_completed`, +`provider_payout + tip_amount`) per
  COMPLETED+PAID+online booking. Cash bookings are excluded — the provider
  collected at the door, no platform debt was ever incurred. The debit
  against them (cash commission) lands under SVC-180.
- One DEBIT (`payout`, -`amount_paise`) per PayoutAudit row whose outcome
  represents a payout that reached (or is en route to) the provider:
  `initiated`, `processing`, `processed`. `failed` and `unknown` audits
  are ignored — they didn't move money away from the wallet.

Post-backfill invariant: for every provider, the current wallet balance
equals the pre-migration formula `SUM(booking.provider_payout +
booking.tip_amount)` filtered on `payment_status = PAID AND
settlement_status = PENDING` for their online bookings. Verified in tests.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6de213f7bc09"
down_revision: Union[str, None] = "5c902ec6a57e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- Table (Enum types are auto-created by create_table for the
    # inline sa.Enum columns; we don't call .create() explicitly to avoid
    # duplicate-type errors) --------------------------------------------------
    op.create_table(
        "provider_ledger_entries",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "provider_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column(
            "entry_type",
            sa.Enum(
                "BOOKING_COMPLETED",
                "PAYOUT",
                "DISPUTE_LOSS",
                "CASH_COMMISSION",
                "REFUND",
                name="ledgerentrytype",
            ),
            nullable=False,
        ),
        sa.Column(
            "reference_type",
            sa.Enum(
                "BOOKING",
                "PAYOUT_AUDIT",
                "PAYMENT",
                name="ledgerreferencetype",
            ),
            nullable=True,
        ),
        sa.Column(
            "reference_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_provider_ledger_provider_created",
        "provider_ledger_entries",
        ["provider_id", "created_at"],
    )

    # ---- Backfill ----------------------------------------------------------
    #
    # Credits: one row per COMPLETED + PAID + online booking. `tip_amount`
    # is summed in here — post-SVC-193 tips remain a booking-row update
    # (Q4: "defer to payout"), and the payout math already reads
    # `provider_payout + tip_amount` from the booking, so we don't create
    # a per-tip ledger entry.
    #
    # `provider_id IS NOT NULL` filters out requested-but-never-assigned
    # bookings. Multiplication by 100 converts rupees (Numeric) → paise
    # (BigInteger).
    op.execute(
        """
        INSERT INTO provider_ledger_entries
            (id, provider_id, amount_paise, entry_type, reference_type,
             reference_id, note, created_at)
        SELECT
            gen_random_uuid(),
            b.provider_id,
            ((b.provider_payout + b.tip_amount) * 100)::bigint,
            'BOOKING_COMPLETED',
            'BOOKING',
            b.id,
            'backfill: pre-SVC-193 completion',
            COALESCE(b.updated_at, b.created_at)
        FROM bookings b
        WHERE b.status = 'COMPLETED'
          AND b.payment_status = 'PAID'
          AND b.payment_method = 'ONLINE'
          AND b.provider_id IS NOT NULL
        """
    )

    # Debits: one per payout audit that moved money to a provider.
    # `initiated`, `processing`, and `processed` all indicate RazorpayX
    # accepted the payout. `failed` and `unknown` didn't (or we don't know),
    # so we don't debit against them.
    op.execute(
        """
        INSERT INTO provider_ledger_entries
            (id, provider_id, amount_paise, entry_type, reference_type,
             reference_id, note, created_at)
        SELECT
            gen_random_uuid(),
            pa.provider_id,
            -pa.amount_paise,
            'PAYOUT',
            'PAYOUT_AUDIT',
            pa.id,
            'backfill: pre-SVC-193 payout',
            pa.created_at
        FROM payout_audits pa
        WHERE pa.outcome IN ('initiated', 'processing', 'processed')
        """
    )


def downgrade() -> None:
    op.drop_index("idx_provider_ledger_provider_created", table_name="provider_ledger_entries")
    op.drop_table("provider_ledger_entries")
    sa.Enum(name="ledgerreferencetype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="ledgerentrytype").drop(op.get_bind(), checkfirst=True)
