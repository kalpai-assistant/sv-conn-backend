"""SVC-410: store the GST on the platform's commission per booking and item.

From SVC-410 on, ``create_booking`` charges GST on the platform's commission at
the service's category ``commission_gst_pct`` (18% by default) and takes it off
the SP's share, so ``total_price = platform_commission + commission_gst +
provider_payout`` on every item, and the same for the booking's sums. The amount
is stored, so payouts, refunds and statements read it and never recompute it.

Both columns are ``NUMERIC(12, 2) NOT NULL DEFAULT 0``, like the other money
columns. Existing rows read 0, which is true: no GST was charged on them, and
their ``provider_payout`` already equals price minus commission. No backfill.

Adding a column doesn't touch the SVC-432 analytics views. The downgrade drops
both columns, losing the stored GST on bookings made since.

Revision ID: 417438a429b2
Revises: ac2f13dad2a1
Create Date: 2026-11-17 15:56:43.478946
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "417438a429b2"
down_revision: str | None = "ac2f13dad2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("bookings", "booking_items")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "commission_gst",
                sa.Numeric(precision=12, scale=2),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "commission_gst")
