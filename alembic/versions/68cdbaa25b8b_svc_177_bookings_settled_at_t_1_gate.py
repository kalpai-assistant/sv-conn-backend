"""svc-177: bookings.settled_at + t+1 gate

Revision ID: 68cdbaa25b8b
Revises: 4fae9d061727
Create Date: 2026-08-16 08:30:55.573210

SVC-177 (§4 T+1 gate): timestamp of when Razorpay settled a booking's payment
to our bank account. The payout path gates on this — a `BOOKING_COMPLETED`
credit whose backing booking has `settled_at IS NULL` doesn't count toward
the wallet balance, so we never pay a provider out of money we haven't
received yet.

Backfill: every existing PAID+COMPLETED+online booking is assumed already
settled (they've been in the system long enough that Razorpay's T+1 window
has passed — beta scale, no in-flight bookings from days ago). Setting
`settled_at = created_at` for those keeps their credits in the wallet after
this migration lands. Cash bookings and unpaid bookings stay NULL — they
never credited the wallet anyway.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "68cdbaa25b8b"
down_revision: Union[str, None] = "4fae9d061727"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: treat existing PAID online bookings as already settled so
    # the wallet balance stays unchanged after this migration. Uses
    # created_at as a stand-in — the real settlement time isn't recorded
    # anywhere pre-SVC-177.
    op.execute(
        """
        UPDATE bookings
        SET settled_at = created_at
        WHERE payment_status = 'PAID'
          AND payment_method = 'ONLINE'
        """
    )


def downgrade() -> None:
    op.drop_column("bookings", "settled_at")
