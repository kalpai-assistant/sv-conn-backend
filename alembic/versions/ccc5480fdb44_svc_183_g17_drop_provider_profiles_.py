"""svc-183 g17: drop provider_profiles.razorpay_linked_account_id (dead Route column)

Revision ID: ccc5480fdb44
Revises: 5b293fc63525
Create Date: 2026-08-12 23:39:56.164207

SVC-183 (G17). The column is a leftover from the Razorpay Route architecture,
which was replaced by the RazorpayX-payouts split shipped in SVC-132. Nothing
populates it and its only reader — ``PaymentService._get_provider_profile`` —
was already unreachable after SVC-130 dropped the `transfers` array from
``create_order`` (all deleted alongside this migration).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ccc5480fdb44"
down_revision: Union[str, None] = "5b293fc63525"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("provider_profiles", "razorpay_linked_account_id")


def downgrade() -> None:
    op.add_column(
        "provider_profiles",
        sa.Column("razorpay_linked_account_id", sa.String(length=255), nullable=True),
    )
