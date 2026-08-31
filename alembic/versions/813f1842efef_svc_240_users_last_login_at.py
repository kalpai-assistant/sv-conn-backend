"""SVC-240: add users.last_login_at column

Revision ID: 813f1842efef
Revises: a91c4d2b7f18
Create Date: 2026-08-31 08:30:00.000000

Adds a nullable `last_login_at` timestamp to `users`. The verify-otp handler
stamps it on every successful sign-in. Nullable so existing rows keep NULL
until their owner next signs in — no backfill assumption about historical
sessions we didn't observe.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "813f1842efef"
down_revision: str | None = "a91c4d2b7f18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
