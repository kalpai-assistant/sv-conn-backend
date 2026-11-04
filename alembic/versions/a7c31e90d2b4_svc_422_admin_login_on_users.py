"""SVC-422: admin console login columns on users

Revision ID: a7c31e90d2b4
Revises: ba56e901f1c4
Create Date: 2026-11-04 12:00:00.000000

Seeds nothing. Run scripts/create_admin.py after this, once for each existing
admin: until then that admin can't log in, because the old phone login is gone.

The CHECK is NOT VALID so an admin who predates it doesn't fail this migration,
which would fail the deploy. New and updated rows are still held to it, so any
update to such a row fails until the script has run.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c31e90d2b4'
down_revision: Union[str, None] = 'ba56e901f1c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    admin_role = postgresql.ENUM("admin", "ops", "support", name="adminrole", create_type=False)
    admin_role.create(op.get_bind(), checkfirst=False)
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("admin_role", admin_role, nullable=True))
    op.add_column("users", sa.Column("totp_secret", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("failed_login_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_users_admin_email",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("role = 'ADMIN'"),
    )
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_admin_has_login CHECK ("
        "role <> 'ADMIN' OR (email IS NOT NULL AND email = lower(email) "
        "AND password_hash IS NOT NULL AND admin_role IS NOT NULL)) NOT VALID"
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_admin_has_login", "users", type_="check")
    op.drop_index("uq_users_admin_email", table_name="users")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
    op.drop_column("users", "totp_enabled_at")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "admin_role")
    op.drop_column("users", "password_hash")
    op.execute("DROP TYPE adminrole")
