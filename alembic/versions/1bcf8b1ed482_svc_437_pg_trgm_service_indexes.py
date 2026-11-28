"""SVC-437: pg_trgm, and GIN trigram indexes on services.name / description.

Duplicate detection (``app/services/catalog_similarity.py``) scores proposals
with pg_trgm's ``similarity()``. GIN rather than GiST because the catalog is
read-mostly.

The downgrade drops the two indexes and deliberately leaves the extension:
``IF NOT EXISTS`` makes a re-upgrade safe, and dropping an extension that some
other object may come to depend on is riskier than leaving it installed.

Adding an index doesn't touch the SVC-432 analytics matviews.

Revision ID: 1bcf8b1ed482
Revises: 6d50518b92ec
Create Date: 2026-11-28 18:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "1bcf8b1ed482"
down_revision: str | None = "6d50518b92ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_services_name_trgm",
        "services",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_services_description_trgm",
        "services",
        ["description"],
        postgresql_using="gin",
        postgresql_ops={"description": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_services_description_trgm", table_name="services")
    op.drop_index("ix_services_name_trgm", table_name="services")
