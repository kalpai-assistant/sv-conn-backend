"""SVC-339: add professionals table + booking professional_id (two-entity model)

Revision ID: b000e0a06330
Revises: f43a6075cfbb
Create Date: 2026-10-01 13:10:56.088059

Corrective Phase A of the provider team model (SVC-318), superseding SVC-319's
single-entity design. Additive: SVC-319's ``provider_profiles.parent_provider_id``
is left alone as dormant legacy.

- ``professionals`` — the person at the door. Belongs to exactly one Service
  Provider, which is still the ``provider_profiles`` table (not renamed).
- ``bookings.professional_id`` — the Pro who does the job.
- ``reviews.professional_id`` — the Pro being rated. The design docs call this
  table ``booking_ratings``; ``reviews`` is the real one. Not backfilled.

Data migration, same revision:

1. Every provider gets a Pro-for-self: same ``user_id`` on both rows, KYC and
   ``is_online`` copied across. ``ON CONFLICT DO NOTHING`` makes a re-run a no-op.
2. Every booking with a provider gets that provider's Pro-for-self.

There is no ``last_location`` on ``provider_profiles`` to copy (live location
lives in Redis), so the Pro rows start with it NULL.

Downgrade drops the two columns and the table; the backfilled values go with
them, and a re-upgrade recreates them identically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import geoalchemy2
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b000e0a06330'
down_revision: Union[str, None] = 'f43a6075cfbb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The ``kycstatus`` type already exists (``provider_profiles.kyc_status``). A
# plain ``sa.Enum`` here would try to CREATE TYPE it again and fail the upgrade.
KYC_STATUS = postgresql.ENUM(
    'PENDING', 'SUBMITTED', 'VERIFIED', 'REJECTED', name='kycstatus', create_type=False
)

# ``users.name`` is nullable, ``display_name`` is not: a provider who never gave
# a name falls back to their business name, then to a fixed label. Never the
# phone number, which would later be shown to consumers.
BACKFILL_PROFESSIONALS = """
    INSERT INTO professionals (
        service_provider_id, user_id, display_name, profile_photo_url,
        kyc_status, kyc_docs, kyc_ai_verdict, kyc_submitted_at, kyc_reviewed_at,
        kyc_reviewed_by, kyc_rejection_reason, is_online
    )
    SELECT
        pp.user_id,
        pp.user_id,
        COALESCE(NULLIF(btrim(u.name), ''), NULLIF(btrim(pp.business_name), ''), 'Professional'),
        pp.profile_photo_url,
        pp.kyc_status,
        pp.kyc_docs,
        pp.kyc_ai_verdict,
        pp.kyc_submitted_at,
        pp.kyc_reviewed_at,
        pp.kyc_reviewed_by,
        pp.kyc_rejection_reason,
        pp.is_online
    FROM provider_profiles pp
    JOIN users u ON u.id = pp.user_id
    ON CONFLICT (user_id, service_provider_id) DO NOTHING
"""

# Matches on ``user_id`` as well as the SP so it picks the Pro-for-self even if
# this ever re-runs after Phase B has given an SP more than one Pro. A booking
# whose provider has no ``provider_profiles`` row has no Pro and stays NULL.
BACKFILL_BOOKINGS = """
    UPDATE bookings b
    SET professional_id = p.id
    FROM professionals p
    WHERE b.provider_id IS NOT NULL
      AND b.professional_id IS NULL
      AND p.service_provider_id = b.provider_id
      AND p.user_id = b.provider_id
"""

# The ticket's guard: every SP has its Pro-for-self. Failing here fails the deploy
# (App Runner rolls back) rather than shipping a half-migrated table.
PROVIDERS_WITHOUT_SELF_PRO = """
    SELECT count(*)
    FROM provider_profiles pp
    LEFT JOIN professionals p
      ON p.service_provider_id = pp.user_id AND p.user_id = pp.user_id
    WHERE p.id IS NULL
"""


def upgrade() -> None:
    op.create_table('professionals',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('service_provider_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('display_name', sa.String(length=120), nullable=False),
    sa.Column('profile_photo_url', sa.String(length=512), nullable=True),
    sa.Column('kyc_status', KYC_STATUS, server_default='PENDING', nullable=False),
    sa.Column('kyc_docs', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('kyc_ai_verdict', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('kyc_submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('kyc_reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('kyc_reviewed_by', sa.UUID(), nullable=True),
    sa.Column('kyc_rejection_reason', sa.Text(), nullable=True),
    sa.Column('is_online', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('last_location', geoalchemy2.types.Geography(geometry_type='POINT', srid=4326, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('last_location_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['kyc_reviewed_by'], ['users.id'], ),
    # CASCADE: an SP's team goes with it.
    sa.ForeignKeyConstraint(['service_provider_id'], ['provider_profiles.user_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'service_provider_id', name='uq_professionals_user_sp')
    )
    op.create_index('ix_professionals_location', 'professionals', ['last_location'], unique=False, postgresql_using='gist')
    op.create_index('ix_professionals_online', 'professionals', ['is_online'], unique=False, postgresql_where=sa.text('is_online = TRUE'))
    op.create_index('ix_professionals_sp_id', 'professionals', ['service_provider_id'], unique=False)
    op.add_column('bookings', sa.Column('professional_id', sa.UUID(), nullable=True))
    op.create_index('ix_bookings_professional_id', 'bookings', ['professional_id'], unique=False, postgresql_where=sa.text('professional_id IS NOT NULL'))
    op.create_foreign_key('fk_bookings_professional_id', 'bookings', 'professionals', ['professional_id'], ['id'])
    op.add_column('reviews', sa.Column('professional_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_reviews_professional_id', 'reviews', 'professionals', ['professional_id'], ['id'])

    op.execute(sa.text(BACKFILL_PROFESSIONALS))
    op.execute(sa.text(BACKFILL_BOOKINGS))

    missing = op.get_bind().execute(sa.text(PROVIDERS_WITHOUT_SELF_PRO)).scalar_one()
    if missing:
        raise RuntimeError(f"SVC-339: {missing} provider(s) have no Pro-for-self after backfill")


def downgrade() -> None:
    op.drop_constraint('fk_reviews_professional_id', 'reviews', type_='foreignkey')
    op.drop_column('reviews', 'professional_id')
    op.drop_constraint('fk_bookings_professional_id', 'bookings', type_='foreignkey')
    op.drop_index('ix_bookings_professional_id', table_name='bookings', postgresql_where=sa.text('professional_id IS NOT NULL'))
    op.drop_column('bookings', 'professional_id')
    op.drop_index('ix_professionals_sp_id', table_name='professionals')
    op.drop_index('ix_professionals_online', table_name='professionals', postgresql_where=sa.text('is_online = TRUE'))
    op.drop_index('ix_professionals_location', table_name='professionals', postgresql_using='gist')
    op.drop_table('professionals')
