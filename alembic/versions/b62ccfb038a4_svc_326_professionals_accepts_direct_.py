"""SVC-326: professionals.accepts_direct_jobs — an SP can stop taking jobs itself

An SP that runs a team may not do jobs itself. This flag on its Pro-for-self
switches that off while the team keeps working; broadcast eligibility then
requires a team Pro who could be assigned the job. Not ``is_active`` (removed
from the team) and not ``is_online`` (mirrors the SP's online switch).

Server default true, so every existing row keeps today's behaviour.

Revision ID: b62ccfb038a4
Revises: e409e33c21bb
Create Date: 2026-10-09 19:53:00.523354

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b62ccfb038a4"
down_revision: str | None = "e409e33c21bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "professionals",
        sa.Column(
            "accepts_direct_jobs", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("professionals", "accepts_direct_jobs")
