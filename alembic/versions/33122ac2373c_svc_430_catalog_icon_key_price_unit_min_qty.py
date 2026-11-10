"""SVC-430: icon_key on categories and services; price_unit and min_qty on services.

Admin catalog management (A10), per Tushar's 2026-11-10 decisions:

- ``icon_key`` varchar(40) NULL on both tables: a key from the preset Lucide list
  (``app.seed_data.catalog_icons``). No DB CHECK, so the list can grow without a
  migration; the admin service layer enforces it.
- ``services.price_unit`` varchar(16) NOT NULL, in the format Vipul fixed on
  SVC-436: a CHECK on the four values, not a Postgres ENUM. The server default
  ``PER_VISIT`` backfills every existing row.
- ``services.min_qty`` int NULL: >= 1 for PER_SQFT / PER_HOUR, NULL otherwise.
  Every existing row is PER_VISIT with NULL, so the CHECK holds on upgrade.

Nothing reads price_unit or min_qty yet; the marketplace stories will.

Revision ID: 33122ac2373c
Revises: d762e3df6aa5
Create Date: 2026-11-10 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "33122ac2373c"
down_revision: str | None = "d762e3df6aa5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here rather than imported from app.models.price_unit, so a later edit
# to app code can't change what this revision did.
PRICE_UNIT_CHECK = "price_unit IN ('PER_HOUR', 'PER_ITEM', 'PER_SQFT', 'PER_VISIT')"
MIN_QTY_CHECK = (
    "(price_unit IN ('PER_HOUR', 'PER_SQFT') AND min_qty IS NOT NULL AND min_qty >= 1)"
    " OR (price_unit NOT IN ('PER_HOUR', 'PER_SQFT') AND min_qty IS NULL)"
)


def upgrade() -> None:
    op.add_column("categories", sa.Column("icon_key", sa.String(length=40), nullable=True))
    op.add_column("services", sa.Column("icon_key", sa.String(length=40), nullable=True))
    op.add_column(
        "services",
        sa.Column(
            "price_unit",
            sa.String(length=16),
            server_default=sa.text("'PER_VISIT'"),
            nullable=False,
        ),
    )
    op.add_column("services", sa.Column("min_qty", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_services_price_unit", "services", PRICE_UNIT_CHECK)
    op.create_check_constraint("ck_services_min_qty_unit", "services", MIN_QTY_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_services_min_qty_unit", "services", type_="check")
    op.drop_constraint("ck_services_price_unit", "services", type_="check")
    op.drop_column("services", "min_qty")
    op.drop_column("services", "price_unit")
    op.drop_column("services", "icon_key")
    op.drop_column("categories", "icon_key")
