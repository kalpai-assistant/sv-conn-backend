"""pay2 three axis status schema

Revision ID: bf4fa2e92254
Revises: 44a9651a7ef8
Create Date: 2026-06-16 19:23:50.826787

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import geoalchemy2


# revision identifiers, used by Alembic.
revision: str = 'bf4fa2e92254'
down_revision: Union[str, None] = '44a9651a7ef8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PAY-2 (SVC-131): three-axis status schema. Additive only — no behavior change.

    # 1. Extend the existing paymentstatus enum (Postgres 16 allows ADD VALUE in a txn).
    op.execute("ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'EXPIRED'")
    op.execute("ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'REFUND_PROCESSING'")
    op.execute("ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'REFUND_FAILED'")

    # 2. New settlementstatus enum type.
    settlement_status = sa.Enum(
        "PENDING", "PROCESSING", "COMPLETED", "FAILED", name="settlementstatus"
    )
    settlement_status.create(op.get_bind(), checkfirst=True)

    # 3. New settlement_status column on bookings, defaulting existing rows to PENDING.
    op.add_column(
        "bookings",
        sa.Column(
            "settlement_status",
            settlement_status,
            nullable=False,
            server_default="PENDING",
        ),
    )


def downgrade() -> None:
    op.drop_column("bookings", "settlement_status")
    sa.Enum(name="settlementstatus").drop(op.get_bind(), checkfirst=True)
    # Note: Postgres cannot drop individual enum values, so the paymentstatus
    # additions (EXPIRED, REFUND_PROCESSING, REFUND_FAILED) are intentionally left
    # in place on downgrade. They are harmless and unused at runtime.
