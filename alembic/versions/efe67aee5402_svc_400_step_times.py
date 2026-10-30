"""SVC-400 follow-up: step times for 3 services, a one-day total for painting

Revision ID: efe67aee5402
Revises: b3f1c7a9d388
Create Date: 2026-10-30 12:00:00.000000

Data only, no DDL. Re-runs ``UPDATE_SERVICE_DETAILS`` from
``app/seed_data/service_details.py``, which now times every step of the AC,
cockroach-control and washing-machine services (each adds up to 60) and gives
Full room painting a service-level ``duration_minutes`` of 480. Painting's steps
stay untimed. The other services' content is unchanged, so their UPDATE is a
no-op.

On CI and any fresh database the services table is empty here and the UPDATEs
match nothing; the seed scripts apply the same content.

Downgrade strips ``duration_minutes`` from the steps of the 3 timed services and
puts painting back to 60.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.seed_data.service_details import UPDATE_SERVICE_DETAILS, service_details_params

# revision identifiers, used by Alembic.
revision: str = 'efe67aee5402'
down_revision: Union[str, None] = 'b3f1c7a9d388'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The services this revision times. Downgrade relies on this PR leaving their
# step titles and icons unchanged: dropping the key gives back exactly the
# untimed steps 9486c2fc98b3 wrote.
_TIMED = [
    ("ac", "Split AC foam-jet service"),
    ("pest-control", "Cockroach control"),
    ("appliances", "Washing machine repair"),
]
_PAINTING = ("painting", "Full room painting")

_UNTIME_STEPS = """
    UPDATE services s
    SET steps = (
        SELECT jsonb_agg(step - 'duration_minutes' ORDER BY ord)
        FROM jsonb_array_elements(s.steps) WITH ORDINALITY AS t(step, ord)
    )
    FROM categories c
    WHERE c.id = s.category_id
      AND c.slug = :category_slug
      AND s.name = :name
      AND jsonb_typeof(s.steps) = 'array'
"""

_RESET_DURATION = """
    UPDATE services s
    SET duration_minutes = 60
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
    for category_slug, name in _TIMED:
        bind.execute(sa.text(_UNTIME_STEPS), {"category_slug": category_slug, "name": name})
    category_slug, name = _PAINTING
    bind.execute(sa.text(_RESET_DURATION), {"category_slug": category_slug, "name": name})
