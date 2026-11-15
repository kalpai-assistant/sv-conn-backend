"""SVC-408: sp_service_pricing, SP boost columns, bookings.locked_price.

The schema base for the marketplace pricing epic (SVC-406). SVC-428 already
created the category pricing columns and ``pricing_config``, so they are not here.

- ``sp_service_pricing``: one price per (SP, service). ``sp_id`` references
  ``provider_profiles.user_id``, the SP identifier used everywhere (the ticket's
  ``service_providers`` table doesn't exist). Money is Numeric(12, 2) like every
  other money column, not the ticket's (10, 2). ``price_unit`` is varchar(16) +
  CHECK, not a Postgres ENUM (Vipul, 2026-11-04). Both FKs are ON DELETE
  RESTRICT (Vipul, 2026-11-04): SPs and services are soft-deleted, so a hard
  delete has to clear its prices first.
- ``provider_profiles.boost_eligible_at`` / ``boost_completed_jobs_count``:
  new-SP boost state. The server default 0 fills every existing row.
- ``bookings.locked_price``: the price frozen at booking time. Added nullable,
  backfilled from ``service_amount`` (the pre-discount total; Vipul's SQL said
  ``total_amount``, which doesn't exist), then made NOT NULL. No server default,
  so a write path that forgets it fails instead of storing a made-up price.

Adding columns doesn't touch the SVC-432 analytics matviews: none of them
selects every column of ``bookings`` or ``provider_profiles``.

Revision ID: ed0ba663c93c
Revises: 66ca0b0a9c76
Create Date: 2026-11-15 15:31:47.829606
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ed0ba663c93c"
down_revision: str | None = "66ca0b0a9c76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here rather than imported from app.models.price_unit, so a later edit
# to app code can't change what this revision did. A test checks it still equals
# the model's PRICE_UNIT_CHECK.
PRICE_UNIT_CHECK = "price_unit IN ('PER_HOUR', 'PER_ITEM', 'PER_SQFT', 'PER_VISIT')"


def upgrade() -> None:
    op.create_table(
        "sp_service_pricing",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("sp_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("price_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("price_unit", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(PRICE_UNIT_CHECK, name="ck_sp_service_pricing_price_unit"),
        sa.ForeignKeyConstraint(
            ["sp_id"],
            ["provider_profiles.user_id"],
            name="fk_sp_service_pricing_sp_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name="fk_sp_service_pricing_service_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sp_id", "service_id", name="uq_sp_service_pricing_sp_service"),
    )
    op.create_index(
        "ix_sp_service_pricing_service_active",
        "sp_service_pricing",
        ["service_id", "is_active"],
        unique=False,
    )

    op.add_column(
        "provider_profiles",
        sa.Column("boost_eligible_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column(
            "boost_completed_jobs_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    # Three steps so existing bookings get their historical price.
    op.add_column(
        "bookings", sa.Column("locked_price", sa.Numeric(precision=12, scale=2), nullable=True)
    )
    op.execute("UPDATE bookings SET locked_price = service_amount WHERE locked_price IS NULL")
    op.alter_column("bookings", "locked_price", nullable=False)


def downgrade() -> None:
    op.drop_column("bookings", "locked_price")
    op.drop_column("provider_profiles", "boost_completed_jobs_count")
    op.drop_column("provider_profiles", "boost_eligible_at")
    op.drop_index("ix_sp_service_pricing_service_active", table_name="sp_service_pricing")
    op.drop_table("sp_service_pricing")
