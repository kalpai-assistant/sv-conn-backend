"""merge SVC-134 and SVC-137 alembic heads

Pure merge point. The SVC-134 (``e5f6a7b8c9d0``, add payment.disputed_at) and
SVC-137 (``a1b2c3d4e5f6``, payout reconciliation schema) migrations both branched
off ``c1d2e3f4a5b6``, leaving two open heads so ``alembic upgrade head`` failed with
"Multiple head revisions". This migration rejoins them into a single head; no schema
change of its own. The SVC-141 column is added in the follow-on revision.

Revision ID: f1a2b3c4d5e6
Revises: a1b2c3d4e5f6, e5f6a7b8c9d0
Create Date: 2026-07-25 00:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = ('a1b2c3d4e5f6', 'e5f6a7b8c9d0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
