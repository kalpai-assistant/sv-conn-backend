"""SVC-320: team invites + professionals.is_active (Phase B, PR 1 of 3)

Revision ID: 17702cbd14a4
Revises: 4d1cca7bc41d
Create Date: 2026-10-02 21:37:01.773228

Schema:

- ``professionals.is_active`` — removing a Pro from a team is a soft delete
  (the LLD's "set is_active=false"; its DDL never had the column). Every
  existing row is a Pro-for-self and starts active.
- ``provider_team_invites`` — an SP's SMS invite for a phone to join its team.
  ``token`` holds a 6-digit code, so it is deliberately *not* unique: with 10^6
  codes and history rows keeping theirs, a global UNIQUE would turn into random
  insert failures. Two partial unique indexes over ``PENDING`` rows instead: one
  live invite per (SP, phone), and live codes unique per phone (accept-invite
  looks up by phone + code). Accepted / revoked / expired rows stay as history.

Data, same revision. SVC-339 backfilled every provider that existed when it ran,
but nothing created a Pro-for-self for providers who signed up afterwards, and
nothing wrote ``bookings.professional_id`` for bookings accepted afterwards.
This PR makes signup create the Pro-for-self; these statements catch up the gap
between the two deploys. They are SVC-339's statements, unchanged (copied, since
migration modules are not importable), so a re-run is a no-op:

1. Pro-for-self for every provider without one — ``ON CONFLICT DO NOTHING``.
2. ``professional_id`` for every accepted booking still missing it, matched to
   the provider's Pro-for-self.
3. The SVC-339 guard: fail the upgrade (App Runner rolls back) if any provider
   still has no Pro-for-self.

Downgrade drops the table and the column. The backfilled Pros and booking links
belong to SVC-339's schema and stay; they are correct data at that revision too.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '17702cbd14a4'
down_revision: Union[str, None] = '4d1cca7bc41d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Verbatim from b000e0a06330 (SVC-339). ``display_name`` falls back from the
# user's name to the business name to a fixed label, never the phone number.
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

# Verbatim from b000e0a06330. Matching on ``user_id`` too picks the
# Pro-for-self even once an SP has team members.
BACKFILL_BOOKINGS = """
    UPDATE bookings b
    SET professional_id = p.id
    FROM professionals p
    WHERE b.provider_id IS NOT NULL
      AND b.professional_id IS NULL
      AND p.service_provider_id = b.provider_id
      AND p.user_id = b.provider_id
"""

PROVIDERS_WITHOUT_SELF_PRO = """
    SELECT count(*)
    FROM provider_profiles pp
    LEFT JOIN professionals p
      ON p.service_provider_id = pp.user_id AND p.user_id = pp.user_id
    WHERE p.id IS NULL
"""


def upgrade() -> None:
    op.create_table('provider_team_invites',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('invited_by', sa.UUID(), nullable=False),
    sa.Column('invitee_phone', sa.String(length=20), nullable=False),
    sa.Column('token', sa.String(length=96), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='PENDING', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('accepted_by_user_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['accepted_by_user_id'], ['users.id'], ),
    # CASCADE: an SP's invites go with it, like its Pros.
    sa.ForeignKeyConstraint(['invited_by'], ['provider_profiles.user_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ux_provider_team_invites_pending', 'provider_team_invites', ['invited_by', 'invitee_phone'], unique=True, postgresql_where=sa.text("status = 'PENDING'"))
    op.create_index('ux_provider_team_invites_pending_code', 'provider_team_invites', ['invitee_phone', 'token'], unique=True, postgresql_where=sa.text("status = 'PENDING'"))
    op.add_column('professionals', sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False))

    op.execute(sa.text(BACKFILL_PROFESSIONALS))
    op.execute(sa.text(BACKFILL_BOOKINGS))

    missing = op.get_bind().execute(sa.text(PROVIDERS_WITHOUT_SELF_PRO)).scalar_one()
    if missing:
        raise RuntimeError(f"SVC-320: {missing} provider(s) have no Pro-for-self after backfill")


def downgrade() -> None:
    op.drop_column('professionals', 'is_active')
    op.drop_index('ux_provider_team_invites_pending_code', table_name='provider_team_invites', postgresql_where=sa.text("status = 'PENDING'"))
    op.drop_index('ux_provider_team_invites_pending', table_name='provider_team_invites', postgresql_where=sa.text("status = 'PENDING'"))
    op.drop_table('provider_team_invites')
