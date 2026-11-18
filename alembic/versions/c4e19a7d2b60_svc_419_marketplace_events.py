"""SVC-419: marketplace_events, the marketplace analytics event table.

Five events come from the customer app through ``POST /analytics/events``; two
(``sp.price_set``, ``booking.marketplace_created``) are written by the backend
in the transaction of the change they describe. ADMIN.md lists them.

Typed ids are plain nullable FKs with no cascade, like the other log tables.
``event_id`` is unique so a client retry is ignored (``ON CONFLICT DO
NOTHING``). The two indexes serve "this event, over time" and "this service".

The downgrade drops the table and its events.

Revision ID: c4e19a7d2b60
Revises: 3bf2aa2a9884
Create Date: 2026-11-18 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e19a7d2b60"
down_revision: str | None = "3bf2aa2a9884"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "marketplace_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("sp_id", sa.UUID(), nullable=True),
        sa.Column("booking_id", sa.UUID(), nullable=True),
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column("service_variant_id", sa.UUID(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        sa.ForeignKeyConstraint(["service_variant_id"], ["service_variants.id"]),
        sa.ForeignKeyConstraint(["sp_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_marketplace_events_event_id"),
    )
    op.create_index(
        "ix_marketplace_events_name_created_at",
        "marketplace_events",
        ["name", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_marketplace_events_service_id",
        "marketplace_events",
        ["service_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_marketplace_events_service_id", table_name="marketplace_events")
    op.drop_index("ix_marketplace_events_name_created_at", table_name="marketplace_events")
    op.drop_table("marketplace_events")
