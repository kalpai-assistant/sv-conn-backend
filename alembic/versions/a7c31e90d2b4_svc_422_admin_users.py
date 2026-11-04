"""SVC-422: admin_users, plus the seed admin when a password is provided

Revision ID: a7c31e90d2b4
Revises: ba56e901f1c4
Create Date: 2026-11-04 12:00:00.000000

The seed runs here once. If ADMIN_SEED_PASSWORD is unset (or unusable) it skips
and logs, and setting the variable later does nothing because this revision never
runs again: use scripts/create_admin_user.py instead.

Each admin gets a linked ``users`` row as its actor id. See ``AdminUser``.

Downgrade leaves the linked ``users`` rows: they may already be referenced by
``admin_actions`` and ``payout_audit``, and being inactive they are harmless.
"""
import logging
import os
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.utils.security import admin_password_problem, hash_password

# revision identifiers, used by Alembic.
revision: str = 'a7c31e90d2b4'
down_revision: Union[str, None] = 'ba56e901f1c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

SEED_EMAIL = "vipul@kalpai.in"


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("totp_secret", sa.String(length=255), nullable=True),
        sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "role",
            postgresql.ENUM("admin", "ops", "support", name="adminrole"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("failed_login_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("email = lower(email)", name="ck_admin_users_email_lowercase"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("user_id"),
    )
    _seed()


def _seed() -> None:
    password = os.environ.get("ADMIN_SEED_PASSWORD")
    if not password:
        logger.warning("ADMIN_SEED_PASSWORD not set: no seed admin created (SVC-422)")
        return
    problem = admin_password_problem(password)
    if problem:
        logger.warning("ADMIN_SEED_PASSWORD %s: no seed admin created (SVC-422)", problem)
        return

    admin_id = uuid.uuid4()
    user_id = uuid.uuid4()
    bind = op.get_bind()
    # users.role is a PG enum of member NAMES, so 'ADMIN'; adminrole holds values.
    bind.execute(
        sa.text(
            "INSERT INTO users (id, phone, name, email, role, is_active) "
            "VALUES (:id, :phone, :email, :email, 'ADMIN', false)"
        ),
        {"id": user_id, "phone": "A" + admin_id.hex[:14], "email": SEED_EMAIL},
    )
    bind.execute(
        sa.text(
            "INSERT INTO admin_users (id, user_id, email, password_hash, role) "
            "VALUES (:id, :user_id, :email, :password_hash, 'admin')"
        ),
        {
            "id": admin_id,
            "user_id": user_id,
            "email": SEED_EMAIL,
            "password_hash": hash_password(password),
        },
    )
    logger.info("Seeded admin %s (SVC-422)", SEED_EMAIL)


def downgrade() -> None:
    op.drop_table("admin_users")
    op.execute("DROP TYPE adminrole")
