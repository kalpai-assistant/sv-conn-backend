"""bookings.doorstep_otp nullable + zero out on state transition

Revision ID: b9a4e2d61f30
Revises: f8d3c9a1b5e2
Create Date: 2026-08-04 01:15:00.000000

Two coupled changes:

1. `doorstep_otp` becomes nullable. The service now zeros the field once
   the OTP has served its purpose (post en_route → in_progress transition,
   or on cancellation). Keeping a plaintext 4-digit secret in a row that
   will outlive its use by weeks is unnecessary attack surface; a DB dump
   would expose every used OTP alongside consumer id.
2. Backfill existing terminal-state rows to NULL so we don't leak stale
   OTPs from bookings that already completed / cancelled before this
   migration ran.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b9a4e2d61f30"
down_revision: str | None = "f8d3c9a1b5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "bookings",
        "doorstep_otp",
        existing_type=sa.String(4),
        nullable=True,
    )
    # Wipe OTPs on bookings that already moved past the transition that
    # needed them. Enum values are lowercase per app.models.booking.
    op.execute(
        """
        UPDATE bookings
        SET doorstep_otp = NULL
        WHERE status IN ('in_progress', 'completed', 'cancelled')
        """
    )


def downgrade() -> None:
    # Backfill any NULLs with a dummy 4-digit value so the NOT NULL
    # constraint can be restored. In practice this only affects rows we
    # wiped in upgrade(), and those bookings are past the OTP-gated
    # transition so the value is functionally meaningless — the placeholder
    # is fine.
    op.execute(
        """
        UPDATE bookings
        SET doorstep_otp = '0000'
        WHERE doorstep_otp IS NULL
        """
    )
    op.alter_column(
        "bookings",
        "doorstep_otp",
        existing_type=sa.String(4),
        nullable=False,
    )
