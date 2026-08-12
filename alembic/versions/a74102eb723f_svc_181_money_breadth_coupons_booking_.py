"""svc-181: money breadth — coupons/booking_items Float -> Numeric(12,2)

Revision ID: a74102eb723f
Revises: ccc5480fdb44
Create Date: 2026-08-13 00:26:20.348738

SVC-181 closes out the SVC-150 / G31 sweep. Three tables still held money as
`double precision`: `booking_items` (4 columns), `coupons` (3), and
`coupon_redemptions` (1). Each of them feeds into a booking's aggregates or is
paid from them — every multi-item cart sums item-level totals into the
booking, and every coupon reduction is written both onto the booking's
`discount_amount` (already Numeric since G31) and onto a `discount_applied`
row that survives after the coupon rule changes.

The pattern here is the same as SVC-150 and G31: `ALTER … TYPE NUMERIC(12,2)
USING …::numeric(12,2)` per column, in one transactional migration. Existing
rows round to two decimal places on the way through, matching the storage
they now sit in; the downgrade puts them back through `::double precision`,
which is deliberately lossy — it reintroduces the very rounding bug the
upgrade exists to remove.

Nullable columns (`coupons.max_discount`, `coupons.min_order_amount`) keep
their nullability across the ALTER — `existing_nullable=True` on the way up
and back down.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a74102eb723f"
down_revision: Union[str, None] = "ccc5480fdb44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, nullable) — nullability preserved across the ALTER.
MONEY_COLUMNS = [
    ("booking_items", "unit_price", False),
    ("booking_items", "total_price", False),
    ("booking_items", "platform_commission", False),
    ("booking_items", "provider_payout", False),
    ("coupons", "discount_value", False),
    ("coupons", "max_discount", True),
    ("coupons", "min_order_amount", True),
    ("coupon_redemptions", "discount_applied", False),
]


def upgrade() -> None:
    for table, column, nullable in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Float(),
            type_=sa.Numeric(12, 2),
            existing_nullable=nullable,
            postgresql_using=f"{column}::numeric(12,2)",
        )


def downgrade() -> None:
    for table, column, nullable in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Numeric(12, 2),
            type_=sa.Float(),
            existing_nullable=nullable,
            postgresql_using=f"{column}::double precision",
        )
