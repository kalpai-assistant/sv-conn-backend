"""SVC-329: admin_actions — an append-only audit row for every admin team mutation

One row per user deactivation, Pro force-remove and Pro reassign, written in
the same transaction as the change. ``target_type`` + ``target_id`` name what
was acted on (no FK: either table); ``sp_user_id`` / ``to_sp_user_id`` are the
owners it touched, so an owner's history is one indexed read.

New table only; nothing existing changes.

Revision ID: 1866e2059674
Revises: b62ccfb038a4
Create Date: 2026-10-10 13:44:41.661655

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1866e2059674"
down_revision: str | None = "b62ccfb038a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_actions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("admin_user_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("sp_user_id", sa.UUID(), nullable=True),
        sa.Column("to_sp_user_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('user_deactivated', 'pro_force_removed', 'pro_reassigned')",
            name="ck_admin_actions_action",
        ),
        sa.CheckConstraint(
            "target_type IN ('user', 'professional')", name="ck_admin_actions_target_type"
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["sp_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_sp_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_actions_created_at", "admin_actions", ["created_at"], unique=False)
    op.create_index("ix_admin_actions_sp_user_id", "admin_actions", ["sp_user_id"], unique=False)
    op.create_index(
        "ix_admin_actions_target", "admin_actions", ["target_type", "target_id"], unique=False
    )
    op.create_index(
        "ix_admin_actions_to_sp_user_id", "admin_actions", ["to_sp_user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_admin_actions_to_sp_user_id", table_name="admin_actions")
    op.drop_index("ix_admin_actions_target", table_name="admin_actions")
    op.drop_index("ix_admin_actions_sp_user_id", table_name="admin_actions")
    op.drop_index("ix_admin_actions_created_at", table_name="admin_actions")
    op.drop_table("admin_actions")
