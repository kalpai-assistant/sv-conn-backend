"""SVC-344: bookings.last_chance_at + nullable booking_status_logs.changed_by

Revision ID: dc120cbb3548
Revises: e937ca1c5a84
Create Date: 2026-10-03

A PAID booking nobody takes no longer waits forever: when its offer window ends
it is broadcast once more and kept open BOOKING_LAST_CHANCE_SECONDS longer; if
still untaken, the system cancels it and refunds in full.

- ``bookings.last_chance_at`` — when that second offer started. NULL otherwise.
  It is what stops the sweep re-offering the same booking forever, and it moves
  the offer's expiry: ``offer_expires_at = COALESCE(last_chance_at + 90 s,
  offer_window_start + 120 s)``. No backfill: NULL is right for every booking.
- ``booking_status_logs.changed_by`` becomes nullable: NULL = the system acted,
  not a person (the auto-cancel). The column is never exposed by the API.

Downgrade drops ``last_chance_at`` and restores NOT NULL on ``changed_by``,
attributing any system rows to the booking's consumer first so the constraint
can be re-added.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dc120cbb3548'
down_revision: Union[str, None] = 'e937ca1c5a84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('booking_status_logs', 'changed_by', existing_type=sa.UUID(), nullable=True)
    op.add_column('bookings', sa.Column('last_chance_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('bookings', 'last_chance_at')
    op.execute(
        sa.text(
            """
            UPDATE booking_status_logs l
            SET changed_by = b.consumer_id
            FROM bookings b
            WHERE l.booking_id = b.id AND l.changed_by IS NULL
            """
        )
    )
    op.alter_column('booking_status_logs', 'changed_by', existing_type=sa.UUID(), nullable=False)
