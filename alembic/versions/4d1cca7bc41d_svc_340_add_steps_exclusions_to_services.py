"""SVC-340: add steps + exclusions to services

Revision ID: 4d1cca7bc41d
Revises: b000e0a06330
Create Date: 2026-10-01 16:24:53.360405

Two nullable JSONB columns on ``services``, rendered on the service detail page:

- ``steps`` — what the Pro does, in order.
- ``exclusions`` — what the service does not cover.

NULL means "no data" and the frontend hides the section. No index (never
queried by these columns), no server default. Edited by SQL only; no admin UI.

Data migration, same revision: placeholder-but-plausible text for 10 services,
one per sub-category. Vipul supplies the final wording later. The text lives in
``app/seed_data/service_details.py``, shared with the seed scripts.

``services`` has no ``slug`` column, so a service is identified the way
``scripts/seed_services.py`` keys it: (category slug, service name). Ids differ
between environments and are never used. Each UPDATE sets fixed values, so a
re-run is a no-op.

On CI and any fresh database the services table is empty at migration time
(services are seeded by scripts after ``alembic upgrade head``), so the UPDATEs
match nothing there. That is expected and must not fail the upgrade; the seed
scripts apply the same text once they have created the services.

Downgrade drops both columns; the seeded text goes with them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# The text and the UPDATE are shared with scripts/seed_services.py and
# scripts/seed_pricing_from_xlsx.py, so a DB seeded by the scripts lands on the
# same text as one this migration ran on (same single-source pattern as
# SVC-312's l1_hierarchy). The migration's own behaviour is unchanged.
from app.seed_data.service_details import UPDATE_SERVICE_DETAILS, service_details_params

# revision identifiers, used by Alembic.
revision: str = '4d1cca7bc41d'
down_revision: Union[str, None] = 'b000e0a06330'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('services', sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('services', sa.Column('exclusions', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    bind = op.get_bind()
    for params in service_details_params():
        bind.execute(sa.text(UPDATE_SERVICE_DETAILS), params)


def downgrade() -> None:
    op.drop_column('services', 'exclusions')
    op.drop_column('services', 'steps')
