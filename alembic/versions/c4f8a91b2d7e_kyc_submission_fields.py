"""kyc submission fields

Revision ID: c4f8a91b2d7e
Revises: 44a9651a7ef8
Create Date: 2026-06-20 10:00:00.000000

Adds the storage + audit fields needed by SC-31 (provider KYC submission +
admin verify/reject endpoints).

- Extends the kycstatus enum with a SUBMITTED value (between PENDING and
  VERIFIED). Existing rows keep their current value; no data migration needed.
- Adds kyc_docs JSONB to store S3 object keys for uploaded documents.
- Adds kyc_submitted_at and kyc_reviewed_at timestamps for audit.
- Adds kyc_rejection_reason for admin rejection notes (max 500 chars; UI
  enforces shorter).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4f8a91b2d7e"
down_revision: Union[str, None] = "44a9651a7ef8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres enums need ALTER TYPE ... ADD VALUE outside a transaction in
    # older versions; modern alembic + Postgres 12+ handles this inside the
    # migration transaction. If we ever target older Postgres, split this out.
    op.execute("ALTER TYPE kycstatus ADD VALUE IF NOT EXISTS 'SUBMITTED' BEFORE 'VERIFIED'")

    op.add_column(
        "provider_profiles",
        sa.Column("kyc_docs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("kyc_submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("kyc_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("kyc_rejection_reason", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_profiles", "kyc_rejection_reason")
    op.drop_column("provider_profiles", "kyc_reviewed_at")
    op.drop_column("provider_profiles", "kyc_submitted_at")
    op.drop_column("provider_profiles", "kyc_docs")
    # Note: dropping an enum value cleanly requires recreating the type. Not
    # reversed here — rolling back this migration on a populated DB requires
    # a manual step. See backend/MIGRATIONS.md if/when that exists.
