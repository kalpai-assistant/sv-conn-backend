"""SVC-371: users.fcm_platform so the release push targets one platform

Records which app registered ``users.fcm_token`` (``android`` | ``ios`` |
``web``). ``broadcast_update_available`` sends a release only to matching
tokens plus NULL ones. Plain nullable string, not a Postgres enum, no default,
no backfill: existing tokens stay NULL and keep getting every release push
until their app re-registers with a platform.

Revision ID: f95c86fc28a7
Revises: cc86ad10f2a6
Create Date: 2026-10-17 13:52:59.347044

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f95c86fc28a7"
down_revision: str | None = "cc86ad10f2a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("fcm_platform", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "fcm_platform")
