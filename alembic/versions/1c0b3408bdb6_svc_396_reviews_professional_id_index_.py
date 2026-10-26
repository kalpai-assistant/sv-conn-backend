"""SVC-396: backfill reviews.professional_id and index it for per-Pro ratings

``reviews.professional_id`` (SVC-339) was never written, so a Pro's rating on
the booking's trust card would always be zero. New reviews now copy it from
``bookings.professional_id``; this fills the old ones the same way. Reviews of
bookings that never had a Pro stay NULL. SVC-339's own migration pointed every
then-existing booking at its SP's Pro-for-self, so that is where old reviews
land.

The index serves the grouped rating query in
``BookingService.attach_professionals``. Downgrade drops only the index: the
backfilled values are correct data and stay.

Revision ID: 1c0b3408bdb6
Revises: c66464b70d44
Create Date: 2026-10-26 20:40:08.989545

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1c0b3408bdb6"
down_revision: str | None = "c66464b70d44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE reviews r
        SET professional_id = b.professional_id
        FROM bookings b
        WHERE r.booking_id = b.id
          AND r.professional_id IS NULL
          AND b.professional_id IS NOT NULL
        """
    )
    op.create_index(
        op.f("ix_reviews_professional_id"), "reviews", ["professional_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_reviews_professional_id"), table_name="reviews")
