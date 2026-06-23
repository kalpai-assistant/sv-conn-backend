"""add languages to provider_profiles

Revision ID: e1c7a4f9b2d3
Revises: c4f8a91b2d7e
Create Date: 2026-06-23 20:30:00.000000

Adds the provider `languages` field for SVC-145 (provider onboarding Step 1).

- text[] column holding the languages a provider speaks (e.g. {Hindi,English}).
- NOT NULL with a server_default of '{}' so existing rows backfill to an empty
  list without a data migration; lets discovery filter with `'X' = ANY(languages)`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e1c7a4f9b2d3"
down_revision: Union[str, None] = "c4f8a91b2d7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "provider_profiles",
        sa.Column(
            "languages",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("provider_profiles", "languages")
