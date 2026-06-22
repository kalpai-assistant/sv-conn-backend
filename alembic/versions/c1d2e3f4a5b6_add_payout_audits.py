"""add payout_audits table

Revision ID: c1d2e3f4a5b6
Revises: 3195ab5985c8
Create Date: 2026-06-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = '3195ab5985c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payout_audits",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("admin_user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("booking_count", sa.Integer(), nullable=False),
        sa.Column("payout_id", sa.String(length=255), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["admin_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["provider_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payout_audits_admin_user_id", "payout_audits", ["admin_user_id"]
    )
    op.create_index(
        "ix_payout_audits_provider_id", "payout_audits", ["provider_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_payout_audits_provider_id", table_name="payout_audits")
    op.drop_index("ix_payout_audits_admin_user_id", table_name="payout_audits")
    op.drop_table("payout_audits")
