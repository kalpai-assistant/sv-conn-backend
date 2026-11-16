"""SVC-418: every SP's subscribed services start priced at today's platform price.

A data migration with no schema change. The marketplace lists an SP for a
variant only when there is an active ``sp_service_pricing`` row for it (the
SVC-409 ranking sheet), and nothing writes that table yet. Without a baseline,
launch day would show no SPs at all.

For every ``provider_services`` subscription to an active service, this inserts
one row per active variant of that service: ``price_amount`` = the variant's
``base_price``, ``price_unit`` = the service's, active. ``ON CONFLICT (sp_id,
service_variant_id) DO NOTHING`` keeps any row that is already there, so a
re-run inserts nothing. There is no SP filter: subscribing already needs KYC
VERIFIED, and the sheet checks eligibility. Nothing does this at runtime: a
service an SP enables later shows "Set your price" (SVC-412).

The SQL lives in ``app/seed_data/svc_418_default_prices.py``, frozen, so
``scripts/dry_run_default_sp_prices.py`` counts exactly what this inserts.

The downgrade removes only this migration's rows that nobody has edited. Each
backfilled row's id is ``md5('svc-418:' || sp_id || ':' || service_variant_id)``
rather than random, and an edit leaves ``updated_at`` later than ``created_at``.
A price an SP set after the upgrade has a random id, and an edited row has a
later ``updated_at``, so both stay. An edited row that stays keeps its derived
id, and a later upgrade skips it on the same conflict.

Revision ID: 471bb682ac1b
Revises: 77ae2d00d567
Create Date: 2026-11-16 17:33:53.355091
"""

from collections.abc import Sequence

from alembic import op
from app.seed_data.svc_418_default_prices import backfill_default_prices, remove_default_prices

revision: str = "471bb682ac1b"
down_revision: str | None = "77ae2d00d567"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    backfill_default_prices(op.get_bind())


def downgrade() -> None:
    remove_default_prices(op.get_bind())
