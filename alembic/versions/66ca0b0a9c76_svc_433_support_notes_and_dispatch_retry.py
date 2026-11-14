"""SVC-433: support notes on bookings, and bookings.dispatch_retried_at.

``booking_support_notes`` is append-only: a support agent's free-text note, or a
"contacted the customer" mark, with its author. The CHECK strings are written
out here rather than built from the model's tuple, so a later change to the
model can't silently change what this migration did.

``bookings.dispatch_retried_at`` is when support last re-broadcast a REQUESTED
booking. The offer window now starts at
GREATEST(created_at, reopened_at, dispatch_retried_at). That equals the old
COALESCE(reopened_at, created_at) for every existing row, since this column
starts NULL and reopened_at is always after created_at.

Adding a column doesn't touch the SVC-432 analytics matviews; only a type change
or a drop of a column they read would.

Revision ID: 66ca0b0a9c76
Revises: 0319a6a0b99b
Create Date: 2026-11-12 10:55:50.065694
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "66ca0b0a9c76"
down_revision: str | None = "0319a6a0b99b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "booking_support_notes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind = 'contacted' OR (body IS NOT NULL AND btrim(body) <> '')",
            name="ck_booking_support_notes_note_has_body",
        ),
        sa.CheckConstraint("kind IN ('note', 'contacted')", name="ck_booking_support_notes_kind"),
        sa.CheckConstraint(
            "body IS NULL OR char_length(body) <= 2000",
            name="ck_booking_support_notes_body_length",
        ),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_booking_support_notes_booking_id_created_at",
        "booking_support_notes",
        ["booking_id", "created_at"],
        unique=False,
    )
    op.add_column(
        "bookings", sa.Column("dispatch_retried_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("bookings", "dispatch_retried_at")
    op.drop_index(
        "ix_booking_support_notes_booking_id_created_at", table_name="booking_support_notes"
    )
    op.drop_table("booking_support_notes")
