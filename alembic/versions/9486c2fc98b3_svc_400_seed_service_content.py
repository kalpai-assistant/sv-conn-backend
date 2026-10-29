"""SVC-400: seed service detail content for the 10 services

Revision ID: 9486c2fc98b3
Revises: 314fe1318dfa
Create Date: 2026-10-29 15:00:00.000000

Data only, no DDL. Re-runs ``UPDATE_SERVICE_DETAILS`` for the 10 services in
``app/seed_data/service_details.py``: steps move to the object shape
``{title, duration_minutes?, icon?}`` (titles unchanged), and included_items
and faqs (each service's own plus COMMON_FAQS) are filled in. Exclusions are
unchanged. hero_image_url and before_after_photos stay NULL (SVC-401).

Same (category slug, service name) keying and fixed values as SVC-340, so a
re-run is a no-op. On CI and any fresh database the services table is empty
here and the UPDATEs match nothing; the seed scripts apply the same content.

Downgrade puts back the SVC-340 string steps and NULLs included_items and faqs
for the same 10 services.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.seed_data.service_details import (
    UPDATE_SERVICE_DETAILS,
    UPDATE_SERVICE_DETAILS_SVC_340,
    service_details_params,
    svc_340_details_params,
)

# revision identifiers, used by Alembic.
revision: str = '9486c2fc98b3'
down_revision: Union[str, None] = '314fe1318dfa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CLEAR_CONTENT = """
    UPDATE services s
    SET included_items = NULL,
        faqs = NULL
    FROM categories c
    WHERE c.id = s.category_id
      AND c.slug = :category_slug
      AND s.name = :name
"""


def upgrade() -> None:
    bind = op.get_bind()
    for params in service_details_params():
        bind.execute(sa.text(UPDATE_SERVICE_DETAILS), params)


def downgrade() -> None:
    bind = op.get_bind()
    for params in svc_340_details_params():
        bind.execute(sa.text(UPDATE_SERVICE_DETAILS_SVC_340), params)
        bind.execute(
            sa.text(_CLEAR_CONTENT),
            {"category_slug": params["category_slug"], "name": params["name"]},
        )
