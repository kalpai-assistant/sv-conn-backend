"""SVC-299: booking_status_logs.note — reason string for admin-driven transitions

The audit trail could always say who moved a booking (``changed_by``) and what
JSONB blob went with it (``metadata``). Neither surfaces the free-text reason
admin support types when they force-cancel: JSON is not what the review sheet
should read, and the operator's "why" is the whole point of the row.

One nullable string column, 500 chars — matching the ceiling on
``cancellation_reason`` and the KYC-reject reason. Every existing row gets a
NULL, no backfill, no rewrite.

Revision ID: 2990f7fbc5fd
Revises: b7e4d2a81f36
Create Date: 2026-09-15

"""

import sqlalchemy as sa
from alembic import op

revision = "2990f7fbc5fd"
down_revision = "b7e4d2a81f36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("booking_status_logs", sa.Column("note", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("booking_status_logs", "note")
