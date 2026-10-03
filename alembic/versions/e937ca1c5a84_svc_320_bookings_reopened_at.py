"""SVC-320: bookings.reopened_at (Phase B, PR 2 of 3)

Revision ID: e937ca1c5a84
Revises: 17702cbd14a4
Create Date: 2026-10-03 21:32:17.019185

Two-level dispatch lets an accepted booking go back to REQUESTED: the SP's last
available Pro declined, or the SP released it. The 2-minute offer window (the
expiry sweep, the offer poll and the card countdown) was measured from
``created_at``, so a booking reopened minutes after creation would be expired by
the next sweep. ``reopened_at`` restarts the window:
``offer_window_start = COALESCE(reopened_at, created_at)``.

Nullable, no default, no backfill: NULL means "never reopened", which is every
existing booking. Downgrade drops it; any reopen timestamps go with it, and the
window falls back to ``created_at``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e937ca1c5a84'
down_revision: Union[str, None] = '17702cbd14a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bookings', sa.Column('reopened_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'reopened_at')
