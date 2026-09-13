"""SVC-287: structured address fields — flat, building, landmark, PIN, contact

An address was one free-text line. A pin locates a building; it does not say
which flat, which floor, or that the entrance is behind the chemist — and when
the address came from "use my current location" none of that was asked for at
all, so a provider got a geocoded street and no door.

Six nullable columns, and `full_address` stays. That is deliberate rather than
transitional: every consumer renders the string — the provider's job card, the
offer payload, the admin booking view — and rows written before today have
nothing to compose one from. New and edited addresses compose it from the
fields; existing rows keep the string they already have, untouched.

Nullable throughout, so this is additive in the strict sense: Postgres fills
existing rows with NULL, no table rewrite, no backfill, no lock of consequence.
`house_floor` is required by `CreateAddressRequest`, not by the column — the
API can tighten later without touching the schema, and the eight existing test
call sites that post only `full_address` keep working until the form ships.

`pincode` is typed by the customer and never taken from the geocoder.
Nominatim answers 411001 for Kharadi, which is 411014 — about 8km out, at
every zoom level from 16 to 19 (SVC-284). The customer knows theirs.

Revision ID: b7e4d2a81f36
Revises: e5b8f1c92d47
Create Date: 2026-09-14

"""

import sqlalchemy as sa
from alembic import op

revision = "b7e4d2a81f36"
# Taken from `alembic heads`, not from the newest address migration: the most
# recent change to *this table* (e8f21c4a9b73, SVC-277) is several revisions
# behind the actual head.
#
# Re-parented once already. This first pointed at d1a7b3c9e5f2 (SVC-291), which
# was the head when the branch was cut — then SVC-293 merged onto the same
# parent and CI failed with "Multiple head revisions are present". That is the
# ordinary cost of two migrations in flight at once, and the fix is to rebase
# and re-point rather than to merge the heads: a linear chain is worth keeping
# while it still can be.
down_revision = "e5b8f1c92d47"
branch_labels = None
depends_on = None


# (column, length). Lengths are generous rather than tight: an Indian address
# line like "Flat 302, 3rd floor, B Wing" is longer than it first looks, and a
# truncation here is data loss the user cannot see.
_COLUMNS = (
    ("house_floor", 120),
    ("building", 120),
    ("landmark_area", 200),
    ("pincode", 6),
    ("contact_name", 120),
    ("contact_phone", 13),
)


def upgrade() -> None:
    for name, length in _COLUMNS:
        op.add_column("addresses", sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("addresses", name)
