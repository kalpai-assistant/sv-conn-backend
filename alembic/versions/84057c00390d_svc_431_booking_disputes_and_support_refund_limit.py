"""SVC-431: booking_disputes, and the support refund cap in pricing_config.

Refund + dispute workflow for the admin console (A11):

- ``booking_disputes``: one row per customer dispute. The enum-like columns are
  varchar + CHECK, not Postgres ENUMs (the SVC-436 convention). At most one open
  (RAISED / IN_REVIEW) dispute per booking, by a partial unique index. A dispute's
  Razorpay refund id is unique where set and is what the refund webhooks match;
  it is deliberately not ``payments.refund_id``, which is the cancel refund's.
- ``pricing_config`` gets ``support_refund_limit`` = 500: the most, in rupees, a
  support-role admin may refund on one dispute. ``ON CONFLICT DO NOTHING`` so a
  value already set by hand survives a re-run.

The CHECK strings are literals here, so a later edit to the model can't change
what this revision did.

Revision ID: 84057c00390d
Revises: 33122ac2373c
Create Date: 2026-11-11 12:28:53.090401
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "84057c00390d"
down_revision: str | None = "33122ac2373c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUPPORT_REFUND_LIMIT_KEY = "support_refund_limit"
SUPPORT_REFUND_LIMIT_SEED = "500"


def upgrade() -> None:
    op.create_table(
        "booking_disputes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("raised_by_user_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("reason_code", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="RAISED", nullable=False),
        sa.Column("resolution_type", sa.String(length=16), nullable=True),
        sa.Column("refund_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("refund_channel", sa.String(length=16), nullable=True),
        sa.Column("razorpay_refund_id", sa.String(length=255), nullable=True),
        sa.Column("refund_status", sa.String(length=16), nullable=True),
        sa.Column("refund_failed_reason", sa.Text(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason_code IN ('NO_SHOW', 'QUALITY', 'BILLING', 'BEHAVIOR', 'OTHER')",
            name="ck_dispute_reason_code",
        ),
        sa.CheckConstraint(
            "refund_channel IS NULL OR refund_channel IN ('RAZORPAY', 'MANUAL')",
            name="ck_dispute_refund_channel",
        ),
        sa.CheckConstraint(
            "refund_status IS NULL OR refund_status IN ('PENDING', 'PROCESSED', 'FAILED')",
            name="ck_dispute_refund_status",
        ),
        sa.CheckConstraint(
            "resolution_type IS NULL OR resolution_type IN ('FULL_REFUND', 'PARTIAL_REFUND', 'NO_REFUND')",
            name="ck_dispute_resolution_type",
        ),
        sa.CheckConstraint(
            "status IN ('RAISED', 'IN_REVIEW', 'RESOLVED', 'CLOSED')", name="ck_dispute_status"
        ),
        sa.CheckConstraint(
            "refund_amount IS NULL OR refund_amount > 0", name="ck_dispute_refund_amount"
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["raised_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_booking_disputes_booking", "booking_disputes", ["booking_id"], unique=False
    )
    op.create_index(
        "idx_booking_disputes_created", "booking_disputes", ["created_at"], unique=False
    )
    op.create_index(
        "uix_booking_disputes_open_booking",
        "booking_disputes",
        ["booking_id"],
        unique=True,
        postgresql_where="status IN ('RAISED', 'IN_REVIEW')",
    )
    op.create_index(
        "uix_booking_disputes_razorpay_refund_id",
        "booking_disputes",
        ["razorpay_refund_id"],
        unique=True,
        postgresql_where="razorpay_refund_id IS NOT NULL",
    )
    op.execute(
        sa.text(
            "INSERT INTO pricing_config (key, value_json) VALUES (:key, CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO NOTHING"
        ).bindparams(key=SUPPORT_REFUND_LIMIT_KEY, value=SUPPORT_REFUND_LIMIT_SEED)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM pricing_config WHERE key = :key").bindparams(
            key=SUPPORT_REFUND_LIMIT_KEY
        )
    )
    op.drop_index(
        "uix_booking_disputes_razorpay_refund_id",
        table_name="booking_disputes",
        postgresql_where="razorpay_refund_id IS NOT NULL",
    )
    op.drop_index(
        "uix_booking_disputes_open_booking",
        table_name="booking_disputes",
        postgresql_where="status IN ('RAISED', 'IN_REVIEW')",
    )
    op.drop_index("idx_booking_disputes_created", table_name="booking_disputes")
    op.drop_index("idx_booking_disputes_booking", table_name="booking_disputes")
    op.drop_table("booking_disputes")
