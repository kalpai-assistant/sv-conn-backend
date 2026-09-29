"""SVC-319: add parent_provider_id to provider_profiles for team model

Revision ID: fb1430a83256
Revises: b4e0a7c19d52
Create Date: 2026-09-29 22:36:28.972315

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb1430a83256'
down_revision: Union[str, None] = 'b4e0a7c19d52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Team model Phase A (SVC-318 epic). NULL = solo provider or team owner; set =
    # team member pointing at their owner. No data migration: every existing row
    # lands at NULL, which is the correct value for "solo".
    op.add_column(
        "provider_profiles",
        sa.Column("parent_provider_id", sa.UUID(), nullable=True),
    )
    # SET NULL: an owner row removed turns its members solo rather than deleting them.
    op.create_foreign_key(
        "fk_provider_profiles_parent",
        "provider_profiles",
        "provider_profiles",
        ["parent_provider_id"],
        ["user_id"],
        ondelete="SET NULL",
    )
    # Partial, so it stays empty while every provider is solo.
    op.create_index(
        "ix_provider_profiles_parent",
        "provider_profiles",
        ["parent_provider_id"],
        postgresql_where=sa.text("parent_provider_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_provider_profiles_parent", table_name="provider_profiles")
    op.drop_constraint("fk_provider_profiles_parent", "provider_profiles", type_="foreignkey")
    op.drop_column("provider_profiles", "parent_provider_id")
