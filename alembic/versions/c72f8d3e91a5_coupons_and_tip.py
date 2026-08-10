"""coupons: coupons + coupon_redemptions tables, and tip/discount fields on bookings

Revision ID: c72f8d3e91a5
Revises: a3f1c4e28d9b
Create Date: 2026-08-10 12:00:00.000000

Two related additions:

1. ``coupons`` + ``coupon_redemptions`` — the coupon domain. A single
   coupon can be user-tied (nullable user_id) or global; a redemption row
   captures one use on one booking with the actual discount that was
   applied, so a subsequent change to the coupon's discount rule doesn't
   rewrite history. ``UNIQUE(booking_id)`` on redemptions enforces "one
   coupon per booking" — the race between two /coupons/apply calls on the
   same cart cannot both win.

2. ``bookings.discount_amount`` + ``bookings.tip_amount`` — money the
   original ``service_amount`` doesn't capture. Discount is what the
   platform absorbed from a coupon; tip is a consumer gratuity that flows
   100% to the provider on top of ``provider_payout``. Both default to 0.0
   and are non-null so a boolean "is there any" check is a straightforward
   ``> 0.0`` on every row, backfilled or new.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c72f8d3e91a5"
down_revision: Union[str, None] = "a3f1c4e28d9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # coupons
    # ------------------------------------------------------------------
    op.create_table(
        "coupons",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column(
            "discount_type",
            sa.Enum("PERCENT", "FLAT", name="discounttype"),
            nullable=False,
        ),
        sa.Column("discount_value", sa.Float(), nullable=False),
        sa.Column("max_discount", sa.Float(), nullable=True),
        sa.Column("min_order_amount", sa.Float(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usage_limit", sa.Integer(), nullable=True),
        sa.Column(
            "usage_limit_per_user",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("code", name="uix_coupon_code"),
    )
    op.create_index(op.f("ix_coupons_code"), "coupons", ["code"], unique=False)
    op.create_index(op.f("ix_coupons_user_id"), "coupons", ["user_id"], unique=False)

    # ------------------------------------------------------------------
    # coupon_redemptions
    # ------------------------------------------------------------------
    op.create_table(
        "coupon_redemptions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("coupon_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("discount_applied", sa.Float(), nullable=False),
        sa.Column(
            "redeemed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.UniqueConstraint("booking_id", name="uix_redemption_booking"),
    )
    op.create_index(
        op.f("ix_coupon_redemptions_coupon_id"),
        "coupon_redemptions",
        ["coupon_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_coupon_redemptions_user_id"),
        "coupon_redemptions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_coupon_redemptions_booking_id"),
        "coupon_redemptions",
        ["booking_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # bookings.discount_amount + bookings.tip_amount
    # ------------------------------------------------------------------
    # Both default to 0.0 at the DB level so the ALTER doesn't need a
    # separate backfill for existing rows.
    op.add_column(
        "bookings",
        sa.Column(
            "discount_amount",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "tip_amount",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
    )
    # Server-default is a migration convenience only — the ORM writes both
    # columns explicitly. Drop the DB-side default so a future INSERT that
    # omits either column fails loudly rather than silently defaulting.
    op.alter_column("bookings", "discount_amount", server_default=None)
    op.alter_column("bookings", "tip_amount", server_default=None)


def downgrade() -> None:
    op.drop_column("bookings", "tip_amount")
    op.drop_column("bookings", "discount_amount")

    op.drop_index(op.f("ix_coupon_redemptions_booking_id"), table_name="coupon_redemptions")
    op.drop_index(op.f("ix_coupon_redemptions_user_id"), table_name="coupon_redemptions")
    op.drop_index(op.f("ix_coupon_redemptions_coupon_id"), table_name="coupon_redemptions")
    op.drop_table("coupon_redemptions")

    op.drop_index(op.f("ix_coupons_user_id"), table_name="coupons")
    op.drop_index(op.f("ix_coupons_code"), table_name="coupons")
    op.drop_table("coupons")

    # Drop the enum after both tables that reference it are gone.
    sa.Enum(name="discounttype").drop(op.get_bind(), checkfirst=False)
