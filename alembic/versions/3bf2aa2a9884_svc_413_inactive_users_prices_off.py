"""SVC-413: switch off the prices of every inactive user.

A data migration with no schema change. Until SVC-413, account deletion
(SVC-367) and admin deactivation (SVC-329) set ``users.is_active = false`` but
left that SP's ``sp_service_pricing`` rows on, and the SVC-418 backfill inserted
active prices for their subscriptions too. From SVC-413 on both flows switch the
prices off in the same transaction (``switch_off_all_prices``); this does the
same for the users who left before. Then ``is_active = true`` means "on offer
now" (SVC-412 comment 14918), and the market insights can count on it. The
amounts stay, as on an unsubscribe. A re-run updates nothing (``p.is_active IS
TRUE`` guard).

``updated_at`` is ``clock_timestamp()``, not ``now()``: ``alembic/env.py`` runs
a whole ``upgrade head`` in one transaction, so in a run that also applies
SVC-418, ``now()`` would equal the ``created_at`` SVC-418 gave its rows. With
the clock, every row this touches gets ``updated_at > created_at``, so SVC-418's
downgrade (which only removes rows whose ``updated_at = created_at``) keeps
them.

The downgrade does nothing: it can't tell which rows were on before, and a
price switched on for a user who has left is the bug this fixes.

Revision ID: 3bf2aa2a9884
Revises: 417438a429b2
Create Date: 2026-11-17 21:40:29.206097
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3bf2aa2a9884"
down_revision: str | None = "417438a429b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

SWITCH_OFF_INACTIVE_USERS_PRICES = (
    "UPDATE sp_service_pricing p "
    "SET is_active = false, updated_at = clock_timestamp() "
    "FROM users u "
    "WHERE u.id = p.sp_id AND u.is_active IS FALSE AND p.is_active IS TRUE"
)


def upgrade() -> None:
    updated = op.get_bind().execute(sa.text(SWITCH_OFF_INACTIVE_USERS_PRICES)).rowcount
    log.info("SVC-413: switched off %d prices of inactive users", updated)


def downgrade() -> None:
    # Nothing to undo: see the module docstring.
    pass
