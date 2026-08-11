"""svc39 g31: money follow-up — discount_amount, tip_amount to Numeric(12,2)

Revision ID: 16d481cb80e3
Revises: 28c483927f67
Create Date: 2026-08-12 00:09:14.510651

SVC-39 G31 finishes what SVC-150 started. Coupons (`bookings.discount_amount`)
and tips (`bookings.tip_amount`) landed on `main` as `Float` *after* SVC-150
converted the other three money columns to `Numeric(12,2)`, and the merge
migration (`28c483927f67`) was intentionally empty. So a `bookings` row after
merge held five money values in two different representations — Numeric next
to Float — and `Booking.provider_payout + Booking.tip_amount` in the earnings
query resolved to `double precision` (Postgres promotes Numeric to float when
mixed), routing every provider's earnings-with-tip through the very
representation SVC-150 was written to eliminate.

Same shape as SVC-150: `ALTER … TYPE NUMERIC(12,2) USING …::numeric(12,2)`.
Existing rows round to two decimal places on the way through, matching what
the column now stores; the downgrade puts them back through `::double
precision`, which reintroduces the SVC-150 rounding bug by design (that is
the reason for going forward in the first place).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "16d481cb80e3"
down_revision: Union[str, None] = "28c483927f67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MONEY_COLUMNS = [
    ("bookings", "discount_amount"),
    ("bookings", "tip_amount"),
]


def upgrade() -> None:
    for table, column in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Float(),
            type_=sa.Numeric(12, 2),
            existing_nullable=False,
            postgresql_using=f"{column}::numeric(12,2)",
        )


def downgrade() -> None:
    for table, column in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Numeric(12, 2),
            type_=sa.Float(),
            existing_nullable=False,
            postgresql_using=f"{column}::double precision",
        )
