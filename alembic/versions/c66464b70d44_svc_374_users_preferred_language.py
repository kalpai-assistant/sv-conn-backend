"""SVC-374: users.preferred_language for the provider/Pro app and its pushes

The language a user picked: ``en`` | ``hi`` | ``mr``, held to those by a CHECK
(autogenerate does not emit CHECK constraints, so it is written here by hand).
Nullable, no default, no backfill: NULL means "never chosen", which the app's
one-time language prompt keys off, so existing users must stay NULL rather
than become ``en``.

Revision ID: c66464b70d44
Revises: f95c86fc28a7
Create Date: 2026-10-18 20:45:10.613651

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c66464b70d44"
down_revision: str | None = "f95c86fc28a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("preferred_language", sa.String(length=2), nullable=True))
    op.create_check_constraint(
        "ck_users_preferred_language",
        "users",
        "preferred_language IN ('en', 'hi', 'mr')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_preferred_language", "users", type_="check")
    op.drop_column("users", "preferred_language")
