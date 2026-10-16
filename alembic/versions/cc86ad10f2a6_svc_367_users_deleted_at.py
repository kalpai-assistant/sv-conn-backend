"""SVC-367: users.deleted_at for in-app account deletion

``DELETE /users/me`` tombstones the users row instead of deleting it (about
twenty non-cascading FKs point at it). This column records when, and is the
only schema change: the rest of the deletion is scrubbing existing columns.
Nullable, no default, no backfill.

Revision ID: cc86ad10f2a6
Revises: e721a87d86e1
Create Date: 2026-10-16 11:09:40.150077

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cc86ad10f2a6"
down_revision: str | None = "e721a87d86e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deleted_at")
