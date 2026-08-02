"""add app_versions table + seed defaults

Revision ID: f8d3c9a1b5e2
Revises: e1c7a4f9b2d3
Create Date: 2026-08-02 20:00:00.000000

SVC-162: creates the `app_versions` table backing the in-app version
check + upgrade nudge. Seeds two rows (android + pwa) at 1.0.0-beta so
the endpoint has something to return on Day 1 without a manual insert.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8d3c9a1b5e2"
down_revision: Union[str, None] = "e1c7a4f9b2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enum as its own type so future platforms (ios native, when we add it
    # post-beta) can be added via ALTER TYPE without touching the table.
    app_platform_enum = postgresql.ENUM("android", "pwa", name="appplatform", create_type=False)
    app_platform_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "app_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("platform", app_platform_enum, nullable=False, unique=True, index=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("min_supported_version", sa.String(32), nullable=False),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("release_notes", sa.String(500), nullable=True),
        sa.Column(
            "released_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Seed Day-1 rows so the endpoint doesn't 404 on first boot. Both
    # platforms start at 1.0.0-beta as both current + minimum supported;
    # `min_supported` gets bumped later during an incident to force
    # everyone off a bad build.
    op.execute(
        """
        INSERT INTO app_versions
            (id, platform, version, min_supported_version, url, release_notes)
        VALUES
            (gen_random_uuid(), 'android', '1.0.0-beta', '1.0.0-beta', NULL, 'First beta'),
            (gen_random_uuid(), 'pwa',     '1.0.0-beta', '1.0.0-beta', NULL, 'First beta')
        """
    )


def downgrade() -> None:
    op.drop_table("app_versions")
    # Drop the enum type too — leaving orphans behind pollutes psql \dT.
    postgresql.ENUM(name="appplatform").drop(op.get_bind(), checkfirst=True)
