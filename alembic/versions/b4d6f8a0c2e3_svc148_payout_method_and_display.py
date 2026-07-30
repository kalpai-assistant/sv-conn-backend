"""SVC-148 provider_profiles.payout_method + payout_display

Providers may now keep both a UPI handle and a bank account on file — saving one no
longer erases the other — so "which columns are populated" stops answering "where
does the money go". ``payout_method`` becomes the single authority for that, and
``payout_display`` holds a safe label for the saved method (last-4 masked account, or
the VPA in full) so the profile read path never has to decrypt anything.

Backfill sets the active method for existing providers from whatever they already
have stored, **preferring UPI** — that is exactly what ``_decrypt_payout_details``
did before this change (``if vpa:`` won in ``create_payout``), so no existing
provider's destination moves as a result of this migration.

``payout_display`` is deliberately left NULL for backfilled rows: deriving it would
mean decrypting every provider's payout PII inside a migration, and it is only a
display nicety. It fills in the next time a provider saves or switches method.

Revision ID: b4d6f8a0c2e3
Revises: f2b3c4d5e6a7
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d6f8a0c2e3'
down_revision: Union[str, None] = 'f2b3c4d5e6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    payout_method = sa.Enum("UPI", "BANK", name="payoutmethod")
    payout_method.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "provider_profiles",
        sa.Column("payout_method", payout_method, nullable=True),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("payout_display", sa.String(length=120), nullable=True),
    )

    # Preserve today's routing exactly: UPI wins when both are stored.
    op.execute(
        """
        UPDATE provider_profiles
           SET payout_method = 'UPI'
         WHERE payout_vpa_encrypted IS NOT NULL
           AND payout_vpa_encrypted <> ''
        """
    )
    op.execute(
        """
        UPDATE provider_profiles
           SET payout_method = 'BANK'
         WHERE payout_method IS NULL
           AND payout_account_number_encrypted IS NOT NULL
           AND payout_account_number_encrypted <> ''
           AND payout_ifsc_encrypted IS NOT NULL
           AND payout_ifsc_encrypted <> ''
        """
    )


def downgrade() -> None:
    op.drop_column("provider_profiles", "payout_display")
    op.drop_column("provider_profiles", "payout_method")
    sa.Enum(name="payoutmethod").drop(op.get_bind(), checkfirst=True)
