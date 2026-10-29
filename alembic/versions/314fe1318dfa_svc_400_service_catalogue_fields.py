"""SVC-400: add catalogue fields to services

Revision ID: 314fe1318dfa
Revises: 1c0b3408bdb6
Create Date: 2026-10-29 12:00:00.000000

Four nullable columns on ``services`` for the SVC-392 service detail page:

- ``hero_image_url`` — String(512), the pre-sized hero image.
- ``included_items`` — JSONB ``[str]``, the "What's included" list.
- ``faqs`` — JSONB ``[{question, answer}]``.
- ``before_after_photos`` — JSONB ``[{before_url, after_url}]`` (SVC-401).

NULL means "no data" and the frontend hides the section or shows its fallback.
No index (never queried by these columns), no server default, and no data:
the content is a separate follow-up, so every row stays NULL here.

``steps`` (SVC-340) is unchanged as a column; it may now also hold the richer
``[{title, duration_minutes?, icon?}]`` shape, which needs no DDL.

The computed fields on the response (rating_average, rating_count,
bookings_this_week) are derived on read and have no columns.

Downgrade drops all four columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '314fe1318dfa'
down_revision: Union[str, None] = '1c0b3408bdb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('services', sa.Column('hero_image_url', sa.String(length=512), nullable=True))
    op.add_column('services', sa.Column('included_items', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('services', sa.Column('faqs', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('services', sa.Column('before_after_photos', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('services', 'before_after_photos')
    op.drop_column('services', 'faqs')
    op.drop_column('services', 'included_items')
    op.drop_column('services', 'hero_image_url')
