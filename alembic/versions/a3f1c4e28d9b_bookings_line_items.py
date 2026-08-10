"""bookings: line items table + backfill + drop service_id

Revision ID: a3f1c4e28d9b
Revises: b9a4e2d61f30
Create Date: 2026-08-09 18:30:00.000000

Moves the "which service was booked" fact off the bookings row and into a
new booking_items table (one row per service in the cart). Existing
bookings are backfilled as 1-item carts using the booking's own amounts —
no live prices are read, so the snapshot semantics that this table exists
to guarantee (see BookingItem module docstring) hold even for legacy rows.

The service_id column on bookings is dropped in the same revision. Every
consumer of the old column was updated to read via booking.items in the
same PR, so there is no window in which application code and schema
disagree. Rollback recreates service_id and repopulates it from
booking_items.service_id where present (the first item wins for carts
that grow past one — this loses the extra items but preserves the
primary service reference the pre-migration app expected).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f1c4e28d9b"
down_revision: Union[str, None] = "b9a4e2d61f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create the new table.
    op.create_table(
        "booking_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.Column("total_price", sa.Float(), nullable=False),
        sa.Column("platform_commission", sa.Float(), nullable=False),
        sa.Column("provider_payout", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        sa.UniqueConstraint("booking_id", "service_id", name="uix_booking_item_service"),
    )
    op.create_index(
        op.f("ix_booking_items_booking_id"),
        "booking_items",
        ["booking_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_booking_items_service_id"),
        "booking_items",
        ["service_id"],
        unique=False,
    )

    # 2. Backfill: one item per existing booking. Uses gen_random_uuid() so we
    #    aren't depending on a Python UUID generator inside the migration —
    #    keeps the whole backfill in one SQL statement, no Python row loop.
    #    Every value comes from the booking's own amounts; no join to services,
    #    so a service row that has since been deleted or repriced does not
    #    corrupt the snapshot for the item that already belonged to a booking.
    op.execute(
        """
        INSERT INTO booking_items (
            id, booking_id, service_id, quantity,
            unit_price, total_price,
            platform_commission, provider_payout,
            created_at
        )
        SELECT
            gen_random_uuid(),
            id,
            service_id,
            1,
            service_amount,
            service_amount,
            platform_commission,
            provider_payout,
            created_at
        FROM bookings
        WHERE service_id IS NOT NULL
        """
    )

    # 3. Drop the now-redundant column. Its FK constraint has to go first —
    #    Postgres won't drop a column that owns a named FK without the
    #    constraint being torn down alongside it in older versions, and being
    #    explicit here keeps the migration readable.
    op.drop_constraint("bookings_service_id_fkey", "bookings", type_="foreignkey")
    op.drop_column("bookings", "service_id")


def downgrade() -> None:
    # 1. Recreate service_id as nullable so the ALTER doesn't require a
    #    default for existing rows before we backfill.
    op.add_column("bookings", sa.Column("service_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "bookings_service_id_fkey",
        "bookings",
        "services",
        ["service_id"],
        ["id"],
    )

    # 2. Backfill from booking_items. For carts with more than one item, this
    #    picks the earliest-created item — arbitrary but deterministic. The
    #    pre-migration app didn't have multi-item bookings so this only kicks
    #    in when downgrading a database that has already accepted multi-item
    #    carts, and there's no "right" answer there.
    op.execute(
        """
        UPDATE bookings b
        SET service_id = (
            SELECT bi.service_id
            FROM booking_items bi
            WHERE bi.booking_id = b.id
            ORDER BY bi.created_at ASC
            LIMIT 1
        )
        """
    )

    # 3. Enforce NOT NULL now that every row has a value.
    op.alter_column("bookings", "service_id", nullable=False)

    # 4. Drop the new table.
    op.drop_index(op.f("ix_booking_items_service_id"), table_name="booking_items")
    op.drop_index(op.f("ix_booking_items_booking_id"), table_name="booking_items")
    op.drop_table("booking_items")
