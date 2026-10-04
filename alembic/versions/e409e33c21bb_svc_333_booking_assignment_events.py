"""SVC-333: booking_assignment_events — the assignment audit trail

Revision ID: e409e33c21bb
Revises: dc120cbb3548
Create Date: 2026-10-04

One append-only row per change of a booking's Pro: claimed (accept / assign),
repicked, declined (by the Pro), released (by the SP), provider_cancelled (a
provider cancel that reopens). Read only by the admin timeline
(GET /admin/bookings/{id}/timeline).

A separate table, not more rows in ``booking_status_logs``: that one feeds the
consumer's ``status_history``, where a re-pick would show "Accepted" twice.

The kind list is spelled out here, not imported from the model, so this
revision keeps creating the same constraint if the model's list changes later.
No backfill: nothing recorded assignments before this.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e409e33c21bb"
down_revision: str | None = "dc120cbb3548"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "booking_assignment_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("from_professional_id", sa.UUID(), nullable=True),
        sa.Column("to_professional_id", sa.UUID(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('claimed', 'repicked', 'declined', 'released', 'provider_cancelled')",
            name="ck_booking_assignment_events_kind",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.ForeignKeyConstraint(["from_professional_id"], ["professionals.id"]),
        sa.ForeignKeyConstraint(["to_professional_id"], ["professionals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_booking_assignment_events_booking_id_created_at",
        "booking_assignment_events",
        ["booking_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_booking_assignment_events_booking_id_created_at",
        table_name="booking_assignment_events",
    )
    op.drop_table("booking_assignment_events")
