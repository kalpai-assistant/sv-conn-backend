"""SVC-277: soft-delete addresses so they can be removed and edited safely

`bookings.consumer_address_id` is a foreign key to `addresses` with no ON
DELETE rule, so Postgres refuses to remove any address a booking has ever
used — and almost every address has one. The Profile screen's Delete button
would 500 on the FK violation.

Editing had the opposite problem: no constraint at all, so an update rewrote
history. A completed booking would show an address the job never happened at,
and an edit during a live job changed the destination for a provider already
en route.

Soft delete resolves both without touching the FK. Nothing is ever removed, so
the constraint never fires and keeps doing its real job — guaranteeing no
booking references a missing address. Editing becomes copy-on-write: the old
row is soft-deleted and a new one created, so old bookings keep pointing at
the address their job actually went to.

The alternative considered was snapshotting the address onto the booking
(copying `full_address` + coordinates at creation), which is the more correct
long-term shape and what mature systems do. Rejected for now: its two
dangerous steps — backfilling every historical booking and dropping the FK —
buy an outcome copy-on-write already delivers. Worth revisiting if a booking's
historical address ever needs to be independent of the address book.

Revision ID: e8f21c4a9b73
Revises: c3d9e1f70a2b
Create Date: 2026-09-06

"""

import sqlalchemy as sa
from alembic import op

revision = "e8f21c4a9b73"
down_revision = "c3d9e1f70a2b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, so every existing row reads as "not deleted" with no backfill.
    op.add_column(
        "addresses",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The address book filters on this on every read; the booking-history reads
    # deliberately do not. Partial index because only live rows are ever
    # filtered for — deleted ones are looked up by primary key, from a booking.
    op.create_index(
        "ix_addresses_user_live",
        "addresses",
        ["user_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    # Safe in both directions: nothing outside this feature reads the column,
    # and dropping it restores the pre-existing "hard delete or nothing"
    # behaviour rather than losing data.
    op.drop_index("ix_addresses_user_live", table_name="addresses")
    op.drop_column("addresses", "deleted_at")
