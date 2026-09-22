"""Verify the auto-migrate deploy step — deliberately does nothing

A no-op revision whose only purpose is to prove the `db-migrate` gate added in
PR #176 actually runs `alembic upgrade head` against beta on merge.

The negative half is already proven: merging #176 itself (no label) logged
"skipping migrations" and skipped both steps. This is the positive half, and it
needs a revision that exists — the gate runs alembic unconditionally once the
label is present, so without a pending revision the step would pass whether or
not it was wired correctly, and prove nothing.

**Why an empty migration rather than a real one.** The first exercise of a path
that writes to a production database should not also be the first exercise of a
schema change. If the step is misconfigured, the worst outcome here is a row
appended to ``alembic_version``; nothing else in the database is touched, and
``downgrade`` puts it back.

Safe to keep once merged: it is a legitimate revision in the chain, and removing
it later would orphan any environment that had already applied it. It costs one
row and one line of history.

Revision ID: b4e0a7c19d52
Revises: a1c9d3f28bcf
Create Date: 2026-09-22
"""

from alembic import op  # noqa: F401  (kept so the module matches every other revision)

revision = "b4e0a7c19d52"
down_revision = "a1c9d3f28bcf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Nothing. Applying this advances ``alembic_version`` and that is the point.

    `pass` rather than a harmless statement (``SELECT 1``, a comment DDL) so
    there is no chance of it locking a table or appearing in a slow-query log.
    The observable effect is the version row alone.
    """


def downgrade() -> None:
    """Also nothing — an empty upgrade has nothing to undo."""
