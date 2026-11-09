"""SVC-428: pricing config columns on categories, and the pricing_config table.

SVC-428 (admin pricing surface, A8) creates the schema it edits, per Tushar's
2026-11-09 decision, so SVC-408 and SVC-410 skip these parts. Types and
defaults are theirs:

- ``categories``, on every row (L1 and L2): ``is_sensitive``, ``reference_price``,
  ``bounds_pct`` (SVC-408) and ``platform_commission_pct``,
  ``platform_commission_min``, ``commission_gst_pct`` (SVC-410). A sensitive
  category must have bounds: ``ck_categories_sensitive_needs_bounds``.
- ``pricing_config``: key/value JSONB, seeded with SVC-408's four rows.

Data, in this revision:

- The four config rows, ``ON CONFLICT DO NOTHING`` so a re-run keeps edits.
- SVC-428's launch defaults: AC, plumber, electrician and pest control are
  sensitive with the ticket's bounds. Matched by slug, so a DB without one of
  them skips it. ``reference_price`` stays NULL: the ticket's rupee figures are
  illustrative.

Nothing reads any of this yet. Booking commission still comes from
``service_variants.platform_commission`` / ``PLATFORM_COMMISSION_PERCENT``.

Revision ID: d762e3df6aa5
Revises: 9d31d9715e0e
Create Date: 2026-11-09 17:51:27.951535
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d762e3df6aa5"
down_revision: str | None = "9d31d9715e0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here rather than imported, so a later edit to app code can't change
# what this revision did.
CONFIG_SEED: dict[str, object] = {
    "ranking_weights": {
        "rating": 0.35,
        "price_vs_ref": 0.25,
        "distance": 0.20,
        "reliability": 0.15,
        "activity": 0.05,
    },
    "boost_window_days": 14,
    "boost_window_jobs": 10,
    "sheet_ttl_seconds": 600,
}

# slug -> bounds_pct, from the SVC-428 ticket's table. Deep cleaning and salon
# stay at the default (not sensitive).
SENSITIVE_SEED: dict[str, str] = {
    "ac": "25.00",
    "plumber": "20.00",
    "electrician": "20.00",
    "pest-control": "25.00",
}


def upgrade() -> None:
    op.create_table(
        "pricing_config",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.add_column(
        "categories",
        sa.Column("is_sensitive", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "categories",
        sa.Column("reference_price", sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        "categories", sa.Column("bounds_pct", sa.Numeric(precision=5, scale=2), nullable=True)
    )
    op.add_column(
        "categories",
        sa.Column(
            "platform_commission_pct",
            sa.Numeric(precision=5, scale=2),
            server_default=sa.text("20.00"),
            nullable=False,
        ),
    )
    op.add_column(
        "categories",
        sa.Column(
            "platform_commission_min",
            sa.Numeric(precision=10, scale=2),
            server_default=sa.text("30.00"),
            nullable=False,
        ),
    )
    op.add_column(
        "categories",
        sa.Column(
            "commission_gst_pct",
            sa.Numeric(precision=5, scale=2),
            server_default=sa.text("18.00"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_categories_sensitive_needs_bounds",
        "categories",
        "is_sensitive = false OR bounds_pct IS NOT NULL",
    )

    for key, value in CONFIG_SEED.items():
        op.execute(
            sa.text(
                "INSERT INTO pricing_config (key, value_json) "
                "VALUES (:key, CAST(:value AS jsonb)) ON CONFLICT (key) DO NOTHING"
            ).bindparams(key=key, value=json.dumps(value))
        )
    for slug, bounds in SENSITIVE_SEED.items():
        op.execute(
            sa.text(
                "UPDATE categories SET is_sensitive = true, bounds_pct = CAST(:bounds AS numeric) "
                "WHERE slug = :slug"
            ).bindparams(slug=slug, bounds=bounds)
        )


def downgrade() -> None:
    op.drop_constraint("ck_categories_sensitive_needs_bounds", "categories", type_="check")
    op.drop_column("categories", "commission_gst_pct")
    op.drop_column("categories", "platform_commission_min")
    op.drop_column("categories", "platform_commission_pct")
    op.drop_column("categories", "bounds_pct")
    op.drop_column("categories", "reference_price")
    op.drop_column("categories", "is_sensitive")
    op.drop_table("pricing_config")
