"""Wallet fix: PAYOUT_RETURNED ledger entries, at most one per PAYOUT debit

A payout's PAYOUT debit stayed on the wallet when its bookings became owed
again (a payout.rejected webhook, or an admin requeue of FAILED/REVERSED), so
the next payout sent less than owed. This adds ``PAYOUT_RETURNED`` to
``ledgerentrytype`` and ``LEDGER_ENTRY`` to ``ledgerreferencetype`` (both columns
store member names, so upper case): a credit references the PAYOUT row it undoes.
A partial unique index on ``reference_id`` for PAYOUT_RETURNED means one debit is
never credited back twice.

``ADD VALUE`` runs in an autocommit block because Postgres won't let a value
added in a transaction be used in that transaction, and the index predicate
uses it. The index is a plain CREATE: the ledger table is small and has no
``PAYOUT_RETURNED`` rows yet, so it builds instantly on container boot.

Downgrade drops the index only. Postgres has no ``ALTER TYPE ... DROP VALUE``,
and the code that writes the value is gone after a downgrade anyway. A re-upgrade
is safe (``IF NOT EXISTS`` on both).

Revision ID: d7e3b5a9c1f2
Revises: ba56e901f1c4
Create Date: 2026-11-08 21:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7e3b5a9c1f2"
down_revision: str | None = "ba56e901f1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE ledgerentrytype ADD VALUE IF NOT EXISTS 'PAYOUT_RETURNED'")
        op.execute("ALTER TYPE ledgerreferencetype ADD VALUE IF NOT EXISTS 'LEDGER_ENTRY'")

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_ledger_payout_returned_ref "
        "ON provider_ledger_entries (reference_id) "
        "WHERE entry_type = 'PAYOUT_RETURNED'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_provider_ledger_payout_returned_ref")
