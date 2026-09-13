"""svc-293: banner_image_url + banner_video_url on services

Revision ID: e5b8f1c92d47
Revises: d1a7b3c9e5f2
Create Date: 2026-09-14 10:00:00.000000

SVC-293 (Phase 3 of the SVC-286-v3 services redesign umbrella): a per-service
banner slot at the top of the service sheet. Static illustration first,
animated 6-10s video where the ROI justifies it — driven by two nullable
columns on ``services``.

Both columns default to NULL. The frontend falls back through the sub-category
banner (SVC-286 v2), then the L1 banner, then a plain coloured block, so
partial coverage renders intentionally rather than as a broken image. NULL is
the design default — a service without curated media is fine.

URLs are stored full-length (bucket-qualified) rather than as keys because the
frontend embeds them directly in ``<video src>`` / ``<img src>``; keeping the
join to bucket + prefix here would push that concatenation onto the client for
zero benefit. 512 chars matches the ``users.avatar_url`` precedent for image
URLs stored elsewhere in the schema.

Downgrade drops the columns cleanly. A row-count check first isn't necessary
— the columns are nullable + additive; the pre-SVC-293 code path never read
them, so dropping them cannot leave a live process with a dangling reference.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b8f1c92d47"
down_revision: str | None = "d1a7b3c9e5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "services",
        sa.Column("banner_image_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "services",
        sa.Column("banner_video_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("services", "banner_video_url")
    op.drop_column("services", "banner_image_url")
