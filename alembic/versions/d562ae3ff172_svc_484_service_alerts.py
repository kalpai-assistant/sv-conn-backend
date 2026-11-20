"""SVC-484: service_alerts ("Notify me" on a service tile) and
provider_profiles.online_since.

``service_alerts`` holds one row per customer tap. The partial unique index
keeps one open alert (not notified, not cancelled) per (user, service); the
partial index on ``expires_at`` is what the sweep reads. ``location`` is a
geography point, so the sweep passes it straight into the dispatch stage rules.

``online_since`` is when an SP's switch last went from off to on. No backfill:
NULL never counts as "online 10 minutes", so an SP who is online at deploy has
to switch on again before a "No pros online" alert fires on them.

The downgrade drops the table and its rows, and the column.

Revision ID: d562ae3ff172
Revises: c4e19a7d2b60
Create Date: 2026-11-20 18:00:00.000000
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d562ae3ff172"
down_revision: str | None = "c4e19a7d2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPEN = "notified_at IS NULL AND cancelled_at IS NULL"


def upgrade() -> None:
    op.create_table(
        "service_alerts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column(
            "location",
            geoalchemy2.types.Geography(
                geometry_type="POINT",
                srid=4326,
                spatial_index=False,
                from_text="ST_GeogFromText",
                name="geography",
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('coming_soon', 'no_pros_online')", name="ck_service_alerts_kind"
        ),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_service_alerts_open_user_service",
        "service_alerts",
        ["user_id", "service_id"],
        unique=True,
        postgresql_where=sa.text(OPEN),
    )
    op.create_index(
        "ix_service_alerts_open_expires_at",
        "service_alerts",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text(OPEN),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("online_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_profiles", "online_since")
    op.drop_index("ix_service_alerts_open_expires_at", table_name="service_alerts")
    op.drop_index("uq_service_alerts_open_user_service", table_name="service_alerts")
    op.drop_table("service_alerts")
