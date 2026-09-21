"""SVC-307: AI-based Aadhaar KYC verifier — schema + verifier-bot seed

Adds the columns the KYCVerifier writes to on each submission, plus the system
user rows that the auto-approve path stamps into ``provider_profiles.kyc_reviewed_by``.

* ``provider_profiles.kyc_ai_verdict`` (JSONB, nullable) — full audit payload
  returned by ``app.services.kyc_verifier.KYCVerifier.verify``. Nullable because
  pre-SVC-307 submissions were never scored and shouldn't be back-filled with a
  placeholder that the admin UI would misread as "the AI looked at it".
* ``provider_profiles.kyc_reviewed_by`` (UUID FK to users.id, nullable) — who
  moved the profile out of SUBMITTED. Populated by the auto-approve path with
  the verifier-bot's id (and manual admin approvals can be stamped later without
  a follow-up schema change). Nullable to keep the SVC-298 rejected-view rows,
  which pre-date reviewer tracking.
* ``users`` gets an idempotent seed of the verifier-bot row. The bot is a real
  ``users`` row so ``kyc_reviewed_by`` can be a genuine FK (rather than a
  nullable string), which keeps referential joins on the admin queue honest.
  ``is_active=False`` so it can never sign in through the OTP flow; role stays
  ``consumer`` (least-privileged existing value — a SYSTEM enum variant was
  considered and rejected as scope creep, since the existing role guards are
  strong enough).

Revision ID: a1c9d3f28bcf
Revises: 2990f7fbc5fd
Create Date: 2026-09-21
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a1c9d3f28bcf"
down_revision = "2990f7fbc5fd"
branch_labels = None
depends_on = None


# Deterministic UUID exposed via ``app.config.Settings.KYC_VERIFIER_BOT_USER_ID``
# so the service layer references it symbolically. Keep in sync there.
KYC_VERIFIER_BOT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000307")
KYC_VERIFIER_BOT_PHONE = "+910000000307"
KYC_VERIFIER_BOT_EMAIL = "verifier-bot@eazyserv.internal"
KYC_VERIFIER_BOT_NAME = "AI KYC Verifier"


def upgrade() -> None:
    op.add_column(
        "provider_profiles",
        sa.Column("kyc_ai_verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("kyc_reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "provider_profiles_kyc_reviewed_by_fkey",
        source_table="provider_profiles",
        referent_table="users",
        local_cols=["kyc_reviewed_by"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    # Idempotent seed — safe to re-run and safe against a prior manual insert
    # of the same email/phone from a rehearsal DB. Explicit ``::uuid`` cast on
    # the bindparam because bindparam-through-sqlalchemy binds it as text and
    # asyncpg refuses ``uuid = varchar`` without an explicit cast.
    op.execute(
        sa.text(
            """
            INSERT INTO users (id, phone, name, email, role, is_active, created_at, updated_at)
            VALUES (
                CAST(:id AS uuid),
                :phone,
                :name,
                :email,
                'CONSUMER',
                FALSE,
                NOW(),
                NOW()
            )
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(
            id=str(KYC_VERIFIER_BOT_USER_ID),
            phone=KYC_VERIFIER_BOT_PHONE,
            name=KYC_VERIFIER_BOT_NAME,
            email=KYC_VERIFIER_BOT_EMAIL,
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM users WHERE id = CAST(:id AS uuid)").bindparams(
            id=str(KYC_VERIFIER_BOT_USER_ID)
        )
    )
    op.drop_constraint(
        "provider_profiles_kyc_reviewed_by_fkey", "provider_profiles", type_="foreignkey"
    )
    op.drop_column("provider_profiles", "kyc_reviewed_by")
    op.drop_column("provider_profiles", "kyc_ai_verdict")
