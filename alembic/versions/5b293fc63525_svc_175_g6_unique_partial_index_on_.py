"""svc-175 g6: unique partial index on payments.razorpay_payment_id

Revision ID: 5b293fc63525
Revises: 16d481cb80e3
Create Date: 2026-08-12 14:28:18.807004

`44a9651a7ef8_drop_payment_unique_constraint` deliberately dropped
`uq_payment_booking` so a booking can have several payment rows (failed
attempts + captured, etc.). But nothing replaced it, so `verify_payment`'s
un-guarded `db.add(Payment(...))` could write N CAPTURED rows for one real
payment — every re-submission of the same signature (which is a pure
function of order_id|payment_id and stays valid forever) writes a duplicate.

Partial unique index on `razorpay_payment_id` where NOT NULL: enforces one
row per real Razorpay payment id at the DB level, while still allowing the
multiple-attempts-per-booking pattern the earlier constraint blocked. NULLs
(the failed branch before Razorpay assigns an id) are excluded from the
constraint by the partial predicate — several failed attempts on the same
booking may still coexist.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b293fc63525"
down_revision: Union[str, None] = "16d481cb80e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uix_payments_razorpay_payment_id",
        "payments",
        ["razorpay_payment_id"],
        unique=True,
        postgresql_where=sa.text("razorpay_payment_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uix_payments_razorpay_payment_id", table_name="payments")
