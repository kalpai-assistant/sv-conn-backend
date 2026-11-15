"""SVC-479: bookings.chosen_sp_id, the SP the customer picked in the marketplace.

When set, the booking is offered to that SP only, and that SP's decline or
release ends it rather than re-broadcasting it. NULL is the open broadcast every
booking had before, and every existing row stays NULL: there is no backfill.

``chosen_sp_id`` references ``provider_profiles.user_id``, the SP identifier
``sp_service_pricing.sp_id`` uses (SVC-408). ON DELETE RESTRICT like that FK: SPs
are soft-deleted, so a hard delete has to deal with their bookings first. The
index is partial because only marketplace bookings name an SP.

Adding a column doesn't touch the SVC-432 analytics matviews.

Revision ID: d9bbf36bb62a
Revises: ed0ba663c93c
Create Date: 2026-11-15 17:38:28.164237
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9bbf36bb62a"
down_revision: str | None = "ed0ba663c93c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("chosen_sp_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_bookings_chosen_sp_id",
        "bookings",
        "provider_profiles",
        ["chosen_sp_id"],
        ["user_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_bookings_chosen_sp_id",
        "bookings",
        ["chosen_sp_id"],
        unique=False,
        postgresql_where="chosen_sp_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("ix_bookings_chosen_sp_id", table_name="bookings")
    op.drop_constraint("fk_bookings_chosen_sp_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "chosen_sp_id")
