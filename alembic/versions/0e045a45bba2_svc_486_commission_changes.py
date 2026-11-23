"""SVC-486: commission_changes and commission_change_notices.

``commission_changes`` holds one row per change of a variant's commission
percent: scheduled (an increase at least 14 days out, or a later decrease),
applied (the amounts before and after are stored on the row), or cancelled. A
partial unique index allows one scheduled change per variant. The partial index
on ``effective_from`` is what the every-minute sweep reads.

``commission_change_notices`` is the pros' paper trail: one row per SP, channel
(push / email / sms) and kind (scheduled / cancelled), with its outcome.

Nothing existing is touched and nothing is backfilled. The downgrade drops both
tables and their rows. The SVC-486 audit actions in ``admin_actions`` are left
as they are, since ``action`` has no CHECK.

Revision ID: 0e045a45bba2
Revises: d562ae3ff172
Create Date: 2026-11-22 23:08:56.320958
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0e045a45bba2"
down_revision: str | None = "d562ae3ff172"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "commission_changes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("service_variant_id", sa.UUID(), nullable=False),
        sa.Column("old_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("new_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("old_platform_commission", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("old_provider_payout", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("new_platform_commission", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("new_provider_payout", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("cancelled_by", sa.UUID(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status <> 'applied' OR (applied_at IS NOT NULL AND new_platform_commission IS NOT NULL AND new_provider_payout IS NOT NULL AND old_platform_commission IS NOT NULL AND old_provider_payout IS NOT NULL)",
            name="ck_commission_changes_applied",
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR (cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL)",
            name="ck_commission_changes_cancelled",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'applied', 'cancelled')", name="ck_commission_changes_status"
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_variant_id"],
            ["service_variants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_commission_changes_scheduled_effective",
        "commission_changes",
        ["effective_from"],
        unique=False,
        postgresql_where=sa.text("status = 'scheduled'"),
    )
    op.create_index(
        op.f("ix_commission_changes_service_variant_id"),
        "commission_changes",
        ["service_variant_id"],
        unique=False,
    )
    op.create_index(
        "uq_commission_changes_one_scheduled",
        "commission_changes",
        ["service_variant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'scheduled'"),
    )
    op.create_table(
        "commission_change_notices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("change_id", sa.UUID(), nullable=False),
        sa.Column("sp_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "channel IN ('push', 'email', 'sms')", name="ck_commission_change_notices_channel"
        ),
        sa.CheckConstraint(
            "kind IN ('scheduled', 'cancelled')", name="ck_commission_change_notices_kind"
        ),
        sa.CheckConstraint(
            "status IN ('sent', 'failed', 'skipped_no_email', 'skipped_no_token', 'mocked')",
            name="ck_commission_change_notices_status",
        ),
        sa.ForeignKeyConstraint(
            ["change_id"],
            ["commission_changes.id"],
        ),
        sa.ForeignKeyConstraint(
            ["sp_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "change_id", "sp_id", "kind", "channel", name="uq_commission_change_notices_once"
        ),
    )
    op.create_index(
        "ix_commission_change_notices_sp_id", "commission_change_notices", ["sp_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_commission_change_notices_sp_id", table_name="commission_change_notices")
    op.drop_table("commission_change_notices")
    op.drop_index(
        "uq_commission_changes_one_scheduled",
        table_name="commission_changes",
        postgresql_where=sa.text("status = 'scheduled'"),
    )
    op.drop_index(op.f("ix_commission_changes_service_variant_id"), table_name="commission_changes")
    op.drop_index(
        "ix_commission_changes_scheduled_effective",
        table_name="commission_changes",
        postgresql_where=sa.text("status = 'scheduled'"),
    )
    op.drop_table("commission_changes")
