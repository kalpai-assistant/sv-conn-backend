"""SVC-141 add bookings.settlement_failure_reason

Nullable column recording why the most recent payout attempt didn't succeed (e.g. a
RazorpayX rejection reason). Set on ``payout.rejected``; cleared when the next payout
attempt starts.

Revision ID: f2b3c4d5e6a7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-25 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b3c4d5e6a7'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("settlement_failure_reason", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bookings", "settlement_failure_reason")
