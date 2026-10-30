"""SVC-388: store each service's qualifying review count and snippet pointer

``GET /services`` shows a review snippet on every card. Computing it live
meant aggregating every qualifying review of every listed service on each
load (965 ms at 40k reviews), so the two inputs are stored on ``services``
instead and kept current by ``ReviewService.create_review``, the only place a
review is written.

The backfill runs ``review_stats_update()`` from the app, the same statement
the write path uses, so the old reviews follow exactly the rule new ones do.
It only names the columns it touches. Downgrade drops both columns; they are
derived and the upgrade recomputes them.

Revision ID: b3f1c7a9d388
Revises: 9486c2fc98b3
Create Date: 2026-10-30 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.services.catalog_service import review_stats_update

# revision identifiers, used by Alembic.
revision: str = "b3f1c7a9d388"
down_revision: str | None = "9486c2fc98b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "services",
        sa.Column("qualifying_review_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "services",
        sa.Column("latest_snippet_review_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "services_latest_snippet_review_id_fkey",
        "services",
        "reviews",
        ["latest_snippet_review_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.get_bind().execute(review_stats_update())


def downgrade() -> None:
    op.drop_constraint("services_latest_snippet_review_id_fkey", "services", type_="foreignkey")
    op.drop_column("services", "latest_snippet_review_id")
    op.drop_column("services", "qualifying_review_count")
