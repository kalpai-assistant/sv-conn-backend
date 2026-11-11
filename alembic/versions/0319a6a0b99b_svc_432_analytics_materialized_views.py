"""SVC-432: materialized views behind the admin analytics dashboards.

Eight views (``app.analytics.views.VIEWS_V1``), each WITH DATA and with a unique
index so the 15-minute Celery refresh can run CONCURRENTLY. The SQL is imported,
not copied, so the suite's ``create_all`` builds exactly the same views;
``VIEWS_V1`` is frozen, and a later change ships as ``VIEWS_V2`` plus its own
migration.

Downgrade drops all eight. Nothing else depends on them.

Note for later migrations: Postgres refuses to ALTER a column's type while a view
reads it. Drop the analytics views first and recreate them after.

Revision ID: 0319a6a0b99b
Revises: 84057c00390d
Create Date: 2026-11-11 21:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from app.analytics.views import VIEWS_V1, create_statements, drop_statements

revision: str = "0319a6a0b99b"
down_revision: str | None = "84057c00390d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for stmt in create_statements(VIEWS_V1):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_statements(VIEWS_V1):
        op.execute(stmt)
