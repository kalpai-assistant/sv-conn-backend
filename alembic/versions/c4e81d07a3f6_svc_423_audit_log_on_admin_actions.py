"""SVC-423: audit log columns on admin_actions

Revision ID: c4e81d07a3f6
Revises: a7c31e90d2b4
Create Date: 2026-11-06 18:00:00.000000

Reuses admin_actions instead of a new table (SVC-423, comment 14838).

target_id becomes text because pricing_config (SVC-428) is keyed by a text key,
so a uuid column couldn't audit it. Both CHECKs go so a new admin story doesn't
need a migration just to log an action; the names stay as constants in
app/models/admin_action.py.

DOWNGRADE DELETES AUDIT ROWS. Every row outside the 3 SVC-329 team actions is
removed, because the old schema can't hold it: its CHECKs refuse the new action
names, and a target_id like "android" can't cast back to uuid. The downgrade
also drops before_json, after_json, ip_address and user_agent from the rows it
keeps. Only a person running it by hand can trigger this; App Runner's
auto-rollback redeploys the old image and never downgrades. Copy the table first
if those rows matter.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e81d07a3f6"
down_revision: str | None = "a7c31e90d2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_admin_actions_action", "admin_actions", type_="check")
    op.drop_constraint("ck_admin_actions_target_type", "admin_actions", type_="check")
    op.alter_column(
        "admin_actions",
        "target_id",
        existing_type=sa.UUID(),
        type_=sa.String(length=64),
        existing_nullable=False,
        postgresql_using="target_id::text",
    )
    op.add_column(
        "admin_actions",
        sa.Column("before_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "admin_actions",
        sa.Column("after_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("admin_actions", sa.Column("ip_address", postgresql.INET(), nullable=True))
    op.add_column("admin_actions", sa.Column("user_agent", sa.String(length=512), nullable=True))
    op.create_index(
        "ix_admin_actions_admin_created",
        "admin_actions",
        ["admin_user_id", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    # See the module docstring: these rows can't exist in the old schema.
    op.execute(
        "DELETE FROM admin_actions "
        "WHERE action NOT IN ('user_deactivated', 'pro_force_removed', 'pro_reassigned')"
    )
    op.drop_index("ix_admin_actions_admin_created", table_name="admin_actions")
    op.drop_column("admin_actions", "user_agent")
    op.drop_column("admin_actions", "ip_address")
    op.drop_column("admin_actions", "after_json")
    op.drop_column("admin_actions", "before_json")
    op.alter_column(
        "admin_actions",
        "target_id",
        existing_type=sa.String(length=64),
        type_=sa.UUID(),
        existing_nullable=False,
        postgresql_using="target_id::uuid",
    )
    op.create_check_constraint(
        "ck_admin_actions_target_type", "admin_actions", "target_type IN ('user', 'professional')"
    )
    op.create_check_constraint(
        "ck_admin_actions_action",
        "admin_actions",
        "action IN ('user_deactivated', 'pro_force_removed', 'pro_reassigned')",
    )
