"""svc-192 g33: cache razorpayx contact + fund_account ids on provider_profiles

Revision ID: 5c902ec6a57e
Revises: a74102eb723f
Create Date: 2026-08-14 16:27:26.866715

SVC-192 (G33): the RazorpayX composite payout API creates a fresh contact +
fund account on every call. Every provider payout therefore leaves a duplicate
in RazorpayX's contact book — cosmetic, but real debt when triaging live
providers on the RazorpayX dashboard. The composite response returns both
ids, so we can cache them on the profile and switch to the fund-account-reference
payout shape from the second payout onwards.

Both columns are nullable — existing profiles predate the cache and populate
lazily on their first post-migration payout.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c902ec6a57e"
down_revision: Union[str, None] = "a74102eb723f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "provider_profiles",
        sa.Column("razorpayx_contact_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("razorpayx_fund_account_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_profiles", "razorpayx_fund_account_id")
    op.drop_column("provider_profiles", "razorpayx_contact_id")
