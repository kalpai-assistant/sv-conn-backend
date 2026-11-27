"""SVC-436: service_requests and sp_request_bans.

``service_requests`` is an SP's proposal for a new catalog service, and how an
admin settled it. ``sp_request_bans`` holds one row per spam ban. There is
deliberately no unique on ``sp_id``, so "banned, then cleared" history is kept.

Status and price unit are varchar + CHECK, not Postgres ENUMs, so a downgrade
has no type to drop and a new value is a one-line CHECK swap. The CHECK strings
are written out here rather than built from the model, so a later model change
can't silently change what this migration did.

Foreign keys: ``resolved_by`` / ``cleared_by`` → users are ON DELETE SET NULL
(Vipul, SVC-436 comment 14840: keep the record, drop attribution). The rest are
RESTRICT: accounts are tombstoned, never deleted, and categories and services
are only ever soft-deleted.

Neither table is read by the SVC-432 analytics matviews.

Revision ID: 6d50518b92ec
Revises: 66ca0b0a9c76
Create Date: 2026-11-27 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6d50518b92ec"
down_revision: str | None = "66ca0b0a9c76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("sp_id", sa.UUID(), nullable=False),
        sa.Column("proposed_name", sa.String(length=200), nullable=False),
        sa.Column("proposed_description", sa.Text(), nullable=False),
        sa.Column("proposed_category_id", sa.UUID(), nullable=False),
        sa.Column("proposed_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("proposed_price_unit", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("recommended_service_id", sa.UUID(), nullable=True),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_service_id", sa.UUID(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED_WITH_REASON', "
            "'REJECTED_WITH_RECOMMENDATION')",
            name="ck_service_requests_status",
        ),
        sa.CheckConstraint(
            "proposed_price_unit IN ('PER_VISIT', 'PER_SQFT', 'PER_HOUR', 'PER_ITEM')",
            name="ck_service_requests_proposed_price_unit",
        ),
        sa.ForeignKeyConstraint(["sp_id"], ["provider_profiles.user_id"]),
        sa.ForeignKeyConstraint(["proposed_category_id"], ["categories.id"]),
        sa.ForeignKeyConstraint(["recommended_service_id"], ["services.id"]),
        sa.ForeignKeyConstraint(["created_service_id"], ["services.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_requests_sp_id_status", "service_requests", ["sp_id", "status"], unique=False
    )
    op.create_index(
        "ix_service_requests_status_created_at",
        "service_requests",
        ["status", sa.text("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "sp_request_bans",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("sp_id", sa.UUID(), nullable=False),
        sa.Column("banned_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["sp_id"], ["provider_profiles.user_id"]),
        sa.ForeignKeyConstraint(["cleared_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sp_request_bans_sp_id", "sp_request_bans", ["sp_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sp_request_bans_sp_id", table_name="sp_request_bans")
    op.drop_table("sp_request_bans")
    op.drop_index("ix_service_requests_status_created_at", table_name="service_requests")
    op.drop_index("ix_service_requests_sp_id_status", table_name="service_requests")
    op.drop_table("service_requests")
