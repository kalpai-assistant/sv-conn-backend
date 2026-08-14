"""svc-178: payments refund_id + refund_amount_paise + refunded_at

Revision ID: f87fa41dcd41
Revises: 6de213f7bc09
Create Date: 2026-08-14 20:53:20.440368

SVC-178: refund lifecycle plumbing on the Payment row.

- `refund_id` (Razorpay `rfnd_*`) — populated when
  `PaymentService.initiate_refund` returns from the Razorpay Refund API.
- `refund_amount_paise` — how much we asked Razorpay to refund (customer-
  facing). Paise BigInt matches every other money-on-the-wire figure.
- `refunded_at` — set only when `refund.processed` webhook lands (the
  terminal-success signal). `refund.failed` moves `booking.payment_status`
  to REFUND_FAILED but leaves this null.

All three are nullable: most payment rows never refund.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f87fa41dcd41"
down_revision: Union[str, None] = "6de213f7bc09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("refund_id", sa.String(length=255), nullable=True))
    op.add_column("payments", sa.Column("refund_amount_paise", sa.BigInteger(), nullable=True))
    op.add_column(
        "payments",
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payments", "refunded_at")
    op.drop_column("payments", "refund_amount_paise")
    op.drop_column("payments", "refund_id")
