"""SVC-414: start the new-SP boost for SPs approved before the hook shipped.

A data migration with no schema change. From SVC-414 on, an SP's first KYC
approval sets ``boost_eligible_at`` (``ProviderProfile.start_boost``), and the
ranking sheet boosts it for ``boost_window_days`` or ``boost_window_jobs``,
whichever runs out first. SPs already VERIFIED at cutover have NULL there, so
without this they would never be boosted, including ones approved a few days
ago who are still new.

Every VERIFIED SP with a NULL ``boost_eligible_at`` gets the start its approval
would have given it: ``kyc_reviewed_at``. The count starts at the jobs it has
already completed, as if the hook had been counting since then. An SP approved
inside the window keeps the rest of it; an older one gets a start in the past,
so ``is_boosted`` says no. A VERIFIED SP with no ``kyc_reviewed_at`` is skipped
and logged: there is no approval time to start from. Rows that already have a
start, and SPs that aren't VERIFIED, are left alone, and a re-run updates
nothing through the IS NULL guard.

The downgrade does nothing. The columns are SVC-408's and stay, and a
backfilled row looks exactly like one the hook wrote after the upgrade, so
clearing them would also take the boost away from SPs approved since.

Revision ID: ac2f13dad2a1
Revises: 471bb682ac1b
Create Date: 2026-11-17 14:16:49.917538
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ac2f13dad2a1"
down_revision: str | None = "471bb682ac1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BACKFILL_BOOST = (
    "UPDATE provider_profiles "
    "SET boost_eligible_at = kyc_reviewed_at, "
    "boost_completed_jobs_count = COALESCE(total_completed_jobs, 0) "
    "WHERE kyc_status = 'VERIFIED' AND boost_eligible_at IS NULL "
    "AND kyc_reviewed_at IS NOT NULL"
)

COUNT_SKIPPED = (
    "SELECT count(*) FROM provider_profiles "
    "WHERE kyc_status = 'VERIFIED' AND boost_eligible_at IS NULL "
    "AND kyc_reviewed_at IS NULL"
)


def upgrade() -> None:
    conn = op.get_bind()
    updated = conn.execute(sa.text(BACKFILL_BOOST)).rowcount
    skipped = conn.execute(sa.text(COUNT_SKIPPED)).scalar_one()
    log.info(
        "SVC-414 boost backfill: %d VERIFIED SPs started at kyc_reviewed_at, "
        "%d skipped (VERIFIED, no kyc_reviewed_at)",
        updated,
        skipped,
    )


def downgrade() -> None:
    # Nothing to undo: see the module docstring.
    pass
