"""add payment disputed_at

Revision ID: e5f6a7b8c9d0
Revises: c1d2e3f4a5b6
Create Date: 2026-07-17 11:04:12.338517

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("disputed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The admin bookings list filters on "has a disputed payment"; the partial index
    # keeps that EXISTS lookup off a full scan while staying tiny (disputes are rare).
    op.create_index(
        "idx_payments_disputed_booking",
        "payments",
        ["booking_id"],
        postgresql_where=sa.text("disputed_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_payments_disputed_booking", table_name="payments")
    op.drop_column("payments", "disputed_at")
