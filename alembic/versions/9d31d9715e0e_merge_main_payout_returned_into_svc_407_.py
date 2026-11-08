"""Merge main (payout_returned ledger) into the SVC-407 admin console branch.

Joins the two heads, with no schema change of its own:

- d7e3b5a9c1f2 (main, PR #232): PAYOUT_RETURNED / LEDGER_ENTRY enum values and
  the partial unique index on provider_ledger_entries. Beta is already here.
- c4e81d07a3f6 (SVC-423, after SVC-422's a7c31e90d2b4): admin login on users and
  the audit log on admin_actions.

The two touch different tables, so either order works: a DB at either head
upgrades through the other branch and then this no-op.

Revision ID: 9d31d9715e0e
Revises: c4e81d07a3f6, d7e3b5a9c1f2
Create Date: 2026-11-09 00:43:46.719329
"""

from collections.abc import Sequence

revision: str = "9d31d9715e0e"
down_revision: tuple[str, str] = ("c4e81d07a3f6", "d7e3b5a9c1f2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
