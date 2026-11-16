"""SVC-480: sp_service_pricing holds one SP price per service VARIANT.

A booking is billed per variant (``create_booking`` prices each cart line from
its variant, or the service's first active one), but SVC-408 kept one price per
(SP, service), which can't cover "1 BHK ₹2,499 … 4 BHK ₹3,999". Tushar,
2026-11-15: one SP price per variant. Vipul's review: every service has at
least one variant, and every price names one.

- Every service with no variant row at all (only admin-created ones; every
  seeded service has variants) gets a "Standard" variant: the service's
  ``base_price`` and ``duration_minutes``, GST 0, sort_order 0, active. Its
  split is ``create_booking``'s legacy one for such a service: commission =
  ``PLATFORM_COMMISSION_PERCENT`` of the price rounded to paise (Postgres
  ``round`` is half away from zero, the same as ROUND_HALF_UP on a price that
  can't be negative), payout = the rest. The percent is **the deployment's
  setting at upgrade time** (``get_settings()``, as ``create_booking`` reads it),
  so a one-unit booking costs and pays what it did before.
- ``sp_service_pricing.service_variant_id`` uuid NOT NULL. A row written before
  this (nothing in app/ writes the table yet) is pointed at its service's first
  variant: active first, then sort_order.
- FK ``(service_variant_id, service_id)`` → ``service_variants (id,
  service_id)`` ON DELETE RESTRICT, so a row can't name another service's
  variant. Its target is the new UNIQUE ``(id, service_id)`` on
  ``service_variants`` (``id`` is already unique, so it refuses nothing).
- ``uq_sp_service_pricing_sp_service`` becomes ``uq_sp_service_pricing_sp_variant
  (sp_id, service_variant_id)``.
- ``ix_sp_service_pricing_variant_active`` for the sheet, which starts from a
  variant.

Neither table is read by the SVC-432 analytics views.

The downgrade restores the SVC-408 shape, which only fits one row per (SP,
service): it refuses while any SP has more than one. It leaves the Standard
variants in place, because ``booking_items`` may reference them by then.

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

# Frozen here rather than imported from app.services.standard_variant, so a later
# edit to app code can't change what this revision did.
STANDARD_VARIANT_LABEL = "Standard"


def _commission_rate():
    """``create_booking``'s legacy rate, read the way it reads it."""
    from app.config import get_settings
    from app.utils.money import to_decimal

    return to_decimal(get_settings().PLATFORM_COMMISSION_PERCENT) / 100


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_service_variants_id_service_id", "service_variants", ["id", "service_id"]
    )
    op.execute(
        sa.text(
            "INSERT INTO service_variants (id, service_id, label, subcategory, base_price, "
            "provider_payout, platform_commission, gst_percent, duration_minutes, sort_order, "
            "is_active) "
            "SELECT gen_random_uuid(), s.id, :label, NULL, s.base_price, "
            "s.base_price - round(s.base_price * :rate, 2), round(s.base_price * :rate, 2), "
            "0, s.duration_minutes, 0, true "
            "FROM services s "
            "WHERE NOT EXISTS (SELECT 1 FROM service_variants v WHERE v.service_id = s.id)"
        ).bindparams(label=STANDARD_VARIANT_LABEL, rate=_commission_rate())
    )

    op.add_column("sp_service_pricing", sa.Column("service_variant_id", sa.UUID(), nullable=True))
    op.execute(
        "UPDATE sp_service_pricing p SET service_variant_id = ("
        "SELECT v.id FROM service_variants v WHERE v.service_id = p.service_id "
        "ORDER BY v.is_active DESC, v.sort_order, v.id LIMIT 1"
        ") WHERE p.service_variant_id IS NULL"
    )
    op.alter_column("sp_service_pricing", "service_variant_id", nullable=False)
    op.create_foreign_key(
        "fk_sp_service_pricing_variant",
        "sp_service_pricing",
        "service_variants",
        ["service_variant_id", "service_id"],
        ["id", "service_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_sp_service_pricing_sp_service", "sp_service_pricing", type_="unique")
    op.create_unique_constraint(
        "uq_sp_service_pricing_sp_variant", "sp_service_pricing", ["sp_id", "service_variant_id"]
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
    op.drop_constraint("uq_sp_service_pricing_sp_variant", "sp_service_pricing", type_="unique")
    op.drop_constraint("fk_sp_service_pricing_variant", "sp_service_pricing", type_="foreignkey")
    op.drop_column("sp_service_pricing", "service_variant_id")
    op.create_unique_constraint(
        "uq_sp_service_pricing_sp_service", "sp_service_pricing", ["sp_id", "service_id"]
    )
    op.drop_constraint("uq_service_variants_id_service_id", "service_variants", type_="unique")
    # The Standard variants stay: booking_items may reference them by now.
