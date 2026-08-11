"""SVC-150 money columns Float -> Numeric(12, 2)

Money was stored as double precision, which cannot represent values like 80.07
exactly (it is really 80.0699999...). Converting to paise with int(x * 100) then
truncated the remainder, so a provider owed Rs 80.07 was paid Rs 80.06. Around 1 in
22 realistic amounts hit this, always short, never over — and the same arithmetic
built the Razorpay order that charges the customer.

numeric(12, 2) stores the value exactly, and scale 2 makes the column itself round
imprecise input to paise on write. Ceiling is Rs 9,999,999,999.99, far beyond any
plausible booking, while still catching a nonsense amount.

Postgres casts double precision -> numeric in place and rounds to the declared
scale, so existing rows are repaired by the conversion (80.0699999... -> 80.07).
Nothing is deployed yet, so there are no settled payouts whose historical amount
would be contradicted by that repair.

Revision ID: a3c5e7f9b1d2
Revises: f2b3c4d5e6a7
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c5e7f9b1d2'
down_revision: Union[str, None] = 'f2b3c4d5e6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column) for every money value in the schema. provider_profiles.avg_rating
# is deliberately absent — it is a rating, not money, and 2dp would be wrong for it.
MONEY_COLUMNS = [
    ("services", "base_price"),
    ("bookings", "service_amount"),
    ("bookings", "platform_commission"),
    ("bookings", "provider_payout"),
    ("payments", "amount"),
    ("payments", "platform_fee"),
    ("payments", "provider_settlement"),
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
    # Reverting reintroduces the rounding bug by design — the point of the forward
    # migration is that float cannot hold these values. Existing data survives the
    # round trip to the cent it was stored at.
    for table, column in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Numeric(12, 2),
            type_=sa.Float(),
            existing_nullable=False,
            postgresql_using=f"{column}::double precision",
        )
