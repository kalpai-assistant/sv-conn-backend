"""service_variants table + nullable booking_items.service_variant_id

Revision ID: a91c4d2b7f18
Revises: 68cdbaa25b8b
Create Date: 2026-08-20 10:30:00.000000

Introduces the pricing "variant" concept so a single service (e.g. "Full home
deep cleaning") can carry multiple priced options (1 BHK / 2 BHK / 3 BHK /
4 BHK). See the launch pricing sheet — 18 categories, ~375 variants — none
of which fit the old one-price-per-service shape.

Backend contract change:
  - `services.base_price` is retained (still NOT NULL) for backwards-compat
    with the current mobile app that reads it as the "starting at" price.
    The seed sets it to the minimum variant price per service.
  - `booking_items.service_variant_id` is nullable in this migration so
    existing rows (none on beta today, but non-zero in prod eventually) do
    not need a backfill. Booking creation gets a variant-first path with a
    fallback to service.base_price when variant_id isn't supplied — the
    frontend variant-picker PR flips clients over gradually.

Payout snapshot on booking_items already exists (`unit_price`,
`platform_commission`, `provider_payout`) — variants just become the source
that flows into those fields, replacing the hardcoded 80/20 split in
booking_service.py.
"""

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a91c4d2b7f18"
down_revision: Union[str, None] = "68cdbaa25b8b"
branch_labels: Union[str, tuple[str], None] = None
depends_on: Union[str, tuple[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        # Vertical sheets group variants under an inner category (Basic /
        # Installation / Filter / Wash / Interior / ...). Nullable because
        # the "Pricing" summary sheet doesn't carry the same concept.
        sa.Column("subcategory", sa.String(length=100), nullable=True),
        # Customer-facing amount, pre-GST. Numeric everywhere for money
        # (SVC-150) — the split derives from this and rounding drift would
        # accumulate through the booking → payment → payout chain.
        sa.Column("base_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("provider_payout", sa.Numeric(precision=12, scale=2), nullable=False),
        # Stored rather than computed. Cheaper on read, and the invariant
        # (base_price = provider_payout + platform_commission) is a check
        # constraint at seed time rather than at every query.
        sa.Column("platform_commission", sa.Numeric(precision=12, scale=2), nullable=False),
        # Percent, not amount — the amount is derived from base_price at
        # display time. 0 = "no GST modelled yet" (Pricing summary rows).
        sa.Column(
            "gst_percent",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("materials_note", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        sa.PrimaryKeyConstraint("id"),
        # A service can't have two variants with the same label. Enforced
        # at DB so a buggy seed script can't produce two "1 BHK" rows and
        # then have the app pick a nondeterministic one.
        sa.UniqueConstraint("service_id", "label", name="uix_variant_service_label"),
    )
    # Partial index mirrors idx_services_category_active: catalog reads for
    # active variants only, most common path.
    op.create_index(
        "idx_variants_service_active",
        "service_variants",
        ["service_id"],
        postgresql_where=sa.text("is_active = true"),
    )

    # Additive on booking_items — nullable so pre-variant rows (none today,
    # but bookings created against pre-variant callers stay legal). The
    # frontend-variant-picker PR will start populating this per booking;
    # a follow-up migration can NOT NULL it once every caller sends it.
    op.add_column(
        "booking_items",
        sa.Column(
            "service_variant_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "booking_items_service_variant_id_fkey",
        "booking_items",
        "service_variants",
        ["service_variant_id"],
        ["id"],
    )
    op.create_index(
        "ix_booking_items_service_variant_id",
        "booking_items",
        ["service_variant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_booking_items_service_variant_id", table_name="booking_items")
    op.drop_constraint(
        "booking_items_service_variant_id_fkey",
        "booking_items",
        type_="foreignkey",
    )
    op.drop_column("booking_items", "service_variant_id")
    op.drop_index("idx_variants_service_active", table_name="service_variants")
    op.drop_table("service_variants")
