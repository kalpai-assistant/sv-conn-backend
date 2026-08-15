"""svc-180: settlement_statements table

Revision ID: 4fae9d061727
Revises: f87fa41dcd41
Create Date: 2026-08-14 21:11:17.401957

SVC-180 (resolves §11 Q5): per-payout settlement statement — the artifact
of what we told the provider each cycle. See app/models/settlement_statement.py
for the shape rationale.

No backfill: pre-SVC-180 payouts didn't produce statements, so retroactively
generating them would misrepresent what the provider was actually shown.
Statements start with the first post-migration payout.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4fae9d061727"
down_revision: Union[str, None] = "f87fa41dcd41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "settlement_statements",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # Nullable — a statement can exist without a payout when the provider
        # owed the platform more than they were owed. Deliberately no FK:
        # audits live in a separate committed transaction (breadcrumb
        # pattern from SVC-172), so a hard FK from statements would
        # couple two independent write paths.
        sa.Column(
            "payout_audit_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("gross_credit_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "cash_commission_deducted_paise",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "other_adjustments_paise",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("net_paid_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "platform_balance_owed_paise",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_settlement_statements_provider_created",
        "settlement_statements",
        ["provider_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_settlement_statements_provider_created",
        table_name="settlement_statements",
    )
    op.drop_table("settlement_statements")
