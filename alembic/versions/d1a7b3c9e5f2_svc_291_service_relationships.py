"""svc-291: service_relationships table for the redesigned service sheet

Revision ID: d1a7b3c9e5f2
Revises: f2b8d31c5a67
Create Date: 2026-09-13 08:00:00.000000

SVC-291 (Phase 2a of the SVC-286-v3 services redesign umbrella): a curated
directed graph between services powering the three related-services surfaces
on the redesigned service sheet (hook / upsell / related). Rows are seeded
algorithmically by ``scripts/seed_service_relationships.py`` — SQL-editable,
no admin UI at beta scale.

The schema is straight from the ticket: one row per (source, related, type)
edge. Unique constraint makes the seed idempotent (re-runs skip existing
rows; hand-curated overrides survive), CHECK constraint blocks a service
appearing in its own related list, composite index makes the read path
`WHERE source_service_id=? AND relationship_type=? ORDER BY sort_order`
an index-only scan.

Downgrade drops the table + its constraints and index cleanly. No data
migration to reverse — the seed's rows will simply not exist after a
downgrade, matching how the pre-SVC-291 code worked (no related surfaces
on the service sheet at all).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1a7b3c9e5f2"
down_revision: str | None = "f2b8d31c5a67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `gen_random_uuid()` needs pgcrypto (Neon enables it by default; the CI
    # postgres image also has it). If a fresh env doesn't, the CREATE TABLE
    # succeeds but INSERTs against the default would fail — loud enough to
    # notice during the shadow-branch dry-run.
    op.create_table(
        "service_relationships",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "source_service_id",
            UUID(as_uuid=True),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "related_service_id",
            UUID(as_uuid=True),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relationship_type", sa.String(length=20), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source_service_id",
            "related_service_id",
            "relationship_type",
            name="uix_service_relationship",
        ),
        sa.CheckConstraint(
            "source_service_id <> related_service_id",
            name="no_self_reference",
        ),
    )
    op.create_index(
        "idx_service_relationships_source",
        "service_relationships",
        ["source_service_id", "relationship_type", "sort_order"],
    )


def downgrade() -> None:
    # Reverse construction order: index first, then table (drops its own
    # constraints when the table goes).
    op.drop_index("idx_service_relationships_source", table_name="service_relationships")
    op.drop_table("service_relationships")
