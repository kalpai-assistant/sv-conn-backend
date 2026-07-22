"""SVC-137 payout reconciliation schema

Adds the schema groundwork for payout reconciliation (no behaviour change yet):
- SettlementStatus gains REVERSED (completed payout later clawed back).
- payout_audits.admin_user_id becomes nullable (non-human actions have no admin).
- payout_audits gains `source` ("admin" | "reconciliation") and an indexed
  `reference_id` (batch key, for mapping an orphaned payout back to its bookings).

Revision ID: a1b2c3d4e5f6
Revises: c1d2e3f4a5b6
Create Date: 2026-07-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. New settlementstatus value. ADD VALUE is safe inside the migration
    #    transaction on PostgreSQL 12+ as long as the value isn't used here.
    op.execute("ALTER TYPE settlementstatus ADD VALUE IF NOT EXISTS 'REVERSED'")

    # 2. Automated actions (reconciliation) have no admin user.
    op.alter_column(
        "payout_audits", "admin_user_id",
        existing_type=sa.UUID(as_uuid=True), nullable=True,
    )

    # 3. Who initiated the row. server_default backfills existing rows to "admin".
    op.add_column(
        "payout_audits",
        sa.Column("source", sa.String(length=20), nullable=False, server_default="admin"),
    )

    # 4. Batch key (RazorpayX reference_id) for orphan → bookings mapping.
    op.add_column(
        "payout_audits",
        sa.Column("reference_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_payout_audits_reference_id", "payout_audits", ["reference_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_payout_audits_reference_id", table_name="payout_audits")
    op.drop_column("payout_audits", "reference_id")
    op.drop_column("payout_audits", "source")
    # Reverting admin_user_id to NOT NULL assumes no reconciliation rows exist yet.
    op.alter_column(
        "payout_audits", "admin_user_id",
        existing_type=sa.UUID(as_uuid=True), nullable=False,
    )
    # Note: PostgreSQL can't drop an enum value without recreating the type; the
    # REVERSED value is left in place on downgrade (harmless if unused).
