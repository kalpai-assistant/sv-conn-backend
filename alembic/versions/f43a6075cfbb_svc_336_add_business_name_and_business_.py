"""SVC-336: add business_name and business_image_url to provider_profiles

Revision ID: f43a6075cfbb
Revises: fb1430a83256
Create Date: 2026-09-30 12:49:19.947552

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f43a6075cfbb'
down_revision: Union[str, None] = 'fb1430a83256'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable: existing providers have neither, and there is no backfill (no real
    # providers yet). Onboarding requires the name; the image stays optional.
    op.add_column(
        "provider_profiles",
        sa.Column("business_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("business_image_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_profiles", "business_image_url")
    op.drop_column("provider_profiles", "business_name")
