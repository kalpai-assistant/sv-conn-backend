"""add provider payout fields

Revision ID: 3195ab5985c8
Revises: bf4fa2e92254
Create Date: 2026-06-18 18:28:33.791925

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import geoalchemy2


# revision identifiers, used by Alembic.
revision: str = '3195ab5985c8'
down_revision: Union[str, None] = 'bf4fa2e92254'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "provider_profiles",
        sa.Column("payout_vpa_encrypted", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("payout_account_number_encrypted", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("payout_ifsc_encrypted", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("settlement_payout_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bookings", "settlement_payout_id")
    op.drop_column("provider_profiles", "payout_ifsc_encrypted")
    op.drop_column("provider_profiles", "payout_account_number_encrypted")
    op.drop_column("provider_profiles", "payout_vpa_encrypted")
