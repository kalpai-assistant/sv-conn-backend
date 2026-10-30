"""Retire the FIRST20 coupon (soft delete)

Revision ID: ba56e901f1c4
Revises: efe67aee5402
Create Date: 2026-10-30 14:30:00.000000

Data only, no DDL. FIRST20 (seeded 2026-09-10, 20% off) is replaced by EXPO25
and FIRST100 (added 2026-10-30). The row is deactivated, not deleted:
``is_active`` is the coupon kill switch, so FIRST20 drops out of
``GET /coupons/available`` and is refused on validate, while the row and its
redemptions stay for the bookings that already used it.

Matched on ``lower(code)``, the same way ``CouponService`` looks codes up. On CI
and any fresh database there is no FIRST20 row and the UPDATE matches nothing.

Downgrade reactivates FIRST20.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ba56e901f1c4'
down_revision: Union[str, None] = 'efe67aee5402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE coupons SET is_active = false WHERE lower(code) = 'first20'")


def downgrade() -> None:
    op.execute("UPDATE coupons SET is_active = true WHERE lower(code) = 'first20'")
