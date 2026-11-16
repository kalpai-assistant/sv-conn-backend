"""SVC-480: sp_service_pricing holds one SP price per service VARIANT.

A booking is billed per variant (``create_booking`` prices each cart line from
its variant, or the service's first active one), but SVC-408 kept one price per
(SP, service), which can't cover "1 BHK ₹2,499 … 4 BHK ₹3,999". Tushar,
2026-11-15: one SP price per variant.

- ``sp_service_pricing.service_variant_id`` uuid NULL. NULL = the price for a
  service with no active variant, which ``create_booking`` bills from
  ``services.base_price``.
- FK ``(service_variant_id, service_id)`` → ``service_variants (id,
  service_id)`` ON DELETE RESTRICT, so a row can't name another service's
  variant. Its target is the new UNIQUE ``(id, service_id)`` on
  ``service_variants`` (``id`` is already unique, so it refuses nothing).
  MATCH SIMPLE: a NULL variant skips the check.
- ``uq_sp_service_pricing_sp_service`` is replaced by two partial unique
  indexes: one row per (SP, variant), and one variant-less row per (SP,
  service).
- ``ix_sp_service_pricing_variant_active`` for the sheet, which starts from a
  variant.

Nothing writes the table yet (the SP pricing surface is SVC-412), so there is no
backfill. Neither table is read by the SVC-432 analytics views.

The downgrade restores the SVC-408 shape, which only fits one row per (SP,
service): it refuses while any SP has more than one.

Revision ID: 77ae2d00d567
Revises: d9bbf36bb62a
Create Date: 2026-11-16 10:43:34.280323
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "77ae2d00d567"
down_revision: str | None = "d9bbf36bb62a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_service_variants_id_service_id", "service_variants", ["id", "service_id"]
    )
    op.add_column("sp_service_pricing", sa.Column("service_variant_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_sp_service_pricing_variant",
        "sp_service_pricing",
        "service_variants",
        ["service_variant_id", "service_id"],
        ["id", "service_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_sp_service_pricing_sp_service", "sp_service_pricing", type_="unique")
    op.create_index(
        "uq_sp_service_pricing_sp_variant",
        "sp_service_pricing",
        ["sp_id", "service_variant_id"],
        unique=True,
        postgresql_where=sa.text("service_variant_id IS NOT NULL"),
    )
    op.create_index(
        "uq_sp_service_pricing_sp_service_no_variant",
        "sp_service_pricing",
        ["sp_id", "service_id"],
        unique=True,
        postgresql_where=sa.text("service_variant_id IS NULL"),
    )
    op.create_index(
        "ix_sp_service_pricing_variant_active",
        "sp_service_pricing",
        ["service_variant_id", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT sp_id, service_id FROM sp_service_pricing "
                "GROUP BY sp_id, service_id HAVING count(*) > 1 "
                "ORDER BY sp_id, service_id"
            )
        )
        .all()
    )
    if duplicates:
        examples = ", ".join(f"(sp_id={sp}, service_id={svc})" for sp, svc in duplicates[:5])
        raise RuntimeError(
            f"SVC-480 downgrade refused: {len(duplicates)} (sp_id, service_id) pair(s) have "
            f"more than one sp_service_pricing row, e.g. {examples}. The SVC-408 shape allows "
            "one price per (SP, service); delete the extra per-variant rows first."
        )

    op.drop_index("ix_sp_service_pricing_variant_active", table_name="sp_service_pricing")
    op.drop_index("uq_sp_service_pricing_sp_service_no_variant", table_name="sp_service_pricing")
    op.drop_index("uq_sp_service_pricing_sp_variant", table_name="sp_service_pricing")
    op.drop_constraint("fk_sp_service_pricing_variant", "sp_service_pricing", type_="foreignkey")
    op.drop_column("sp_service_pricing", "service_variant_id")
    op.create_unique_constraint(
        "uq_sp_service_pricing_sp_service", "sp_service_pricing", ["sp_id", "service_id"]
    )
    op.drop_constraint("uq_service_variants_id_service_id", "service_variants", type_="unique")
