"""svc-286: category hierarchy — add parent_id + seed 6 L1 rows + link 17 L2 rows

Revision ID: f2b8d31c5a67
Revises: e8f21c4a9b73
Create Date: 2026-09-12 06:00:00.000000

SVC-286: restructure the flat 18-category list into 6 illustrated Level-1 tiles
+ the existing `miscellaneous` promoted in-place as the 7th L1 (its id/slug/
services stay untouched so historical bookings and provider subscriptions all
survive). The 17 non-misc categories become Level-2 sub-categories, each
pointing at its L1 via a new `categories.parent_id` self-FK.

Nothing else in the schema changes. `services.category_id` still points at the
L2 rows (unchanged ids), so `booking_items`, `provider_services`, and every
past booking keep working. Home + category-page rendering shifts on the
frontend (separate PR); the backend just exposes the new shape.

Downgrade is fully reversible: deletes the 6 seeded L1 rows (safe — nothing
references them yet), restores `miscellaneous.sort_order`, and drops the FK +
index + column. If any service ever gets attached to an L1 row before a
downgrade, the DELETE will error on the FK — which is the correct loud
failure, not a silent orphan.

L1 slugs match the WebP asset filenames shipped in frontend PR #135 exactly:
- salon-spa-women
- salon-massage-men
- cleaning-pest-control
- ac-appliance-repair
- electrician-plumber-carpenter
- painting-home-improvement
- miscellaneous (already existed; promoted in place)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b8d31c5a67"
down_revision: str | None = "e8f21c4a9b73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Explicit list so downgrade knows exactly which rows it inserted. Editing the
# names here after ship is fine; editing the slugs would orphan any frontend
# code that references them by slug (currently: category-images.ts).
_L1_ROWS = [
    ("salon-spa-women", "Salon & Spa for Women", 0),
    ("salon-massage-men", "Salon & Massage for Men", 1),
    ("cleaning-pest-control", "Cleaning & Pest Control", 2),
    ("ac-appliance-repair", "AC & Appliance Repair", 3),
    ("electrician-plumber-carpenter", "Electrician, Plumber & Carpenter", 4),
    ("painting-home-improvement", "Painting & Home Improvement", 5),
    # 'miscellaneous' is intentionally NOT in this list — it exists already
    # and gets promoted (sort_order bumped to 6). Deleting it in downgrade
    # would blow up any booking that ever referenced a service inside it.
]

# Mapping of existing L2 slug → its new L1 parent slug. See SVC-286
# "Proposed mapping". Two open questions in the ticket were resolved to:
#   - Toilet Cleaning (Commercial) + Vehicle Wash → under Cleaning & Pest
#     Control (no separate B2B tile yet).
#   - Miscellaneous stays L1, not a bucket-with-sub-cat.
_L2_PARENT = {
    "salon-women": "salon-spa-women",
    "nail-art": "salon-spa-women",
    "salon-men": "salon-massage-men",
    "massage": "salon-massage-men",
    "cleaning": "cleaning-pest-control",
    "pest-control": "cleaning-pest-control",
    "toilet-cleaning-commercial": "cleaning-pest-control",
    "vehicle-wash-cleaning": "cleaning-pest-control",
    "ac": "ac-appliance-repair",
    "appliances": "ac-appliance-repair",
    "ro-purifier": "ac-appliance-repair",
    "electrician": "electrician-plumber-carpenter",
    "plumber": "electrician-plumber-carpenter",
    "carpenter": "electrician-plumber-carpenter",
    "furniture": "electrician-plumber-carpenter",
    "painting": "painting-home-improvement",
    "cctv-light-event": "painting-home-improvement",
}


def upgrade() -> None:
    # 1. Schema — self-referential FK. ON DELETE SET NULL so removing an L1
    #    later (via admin, not this migration) demotes its children to
    #    orphans rather than nuking them.
    op.add_column(
        "categories",
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_categories_parent_id",
        "categories",
        "categories",
        ["parent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_categories_parent_id",
        "categories",
        ["parent_id"],
    )

    # 2. Data — seed the 6 new L1 rows. `gen_random_uuid()` requires pgcrypto,
    #    which Neon has enabled by default; if this ever runs against a raw
    #    Postgres without it, the migration will error loudly rather than
    #    silently produce NULLs.
    for slug, name, sort_order in _L1_ROWS:
        op.execute(
            sa.text(
                "INSERT INTO categories (id, slug, name, sort_order, is_active, created_at) "
                "VALUES (gen_random_uuid(), :slug, :name, :sort_order, true, NOW())"
            ).bindparams(slug=slug, name=name, sort_order=sort_order)
        )

    # 3. Bump miscellaneous sort_order so it lands after the 6 new L1s. Its id
    #    and slug are untouched — any provider_services / bookings that
    #    referenced services under it keep working.
    op.execute("UPDATE categories SET sort_order = 6 WHERE slug = 'miscellaneous'")

    # 4. Link the 17 existing L2 rows to their new L1 parent by slug. Wrapped
    #    as one statement per row rather than a CASE expression — Postgres
    #    query planner handles either the same, and this diff is easier to
    #    read at review time.
    for l2_slug, l1_slug in _L2_PARENT.items():
        op.execute(
            sa.text(
                "UPDATE categories SET parent_id = "
                "(SELECT id FROM categories WHERE slug = :l1_slug) "
                "WHERE slug = :l2_slug"
            ).bindparams(l1_slug=l1_slug, l2_slug=l2_slug)
        )


def downgrade() -> None:
    # Reverse order. Data first, then schema, so the FK doesn't complain when
    # the column disappears out from under any lingering references.

    # 1. Restore miscellaneous sort_order to its pre-migration slot.
    op.execute("UPDATE categories SET sort_order = 11 WHERE slug = 'miscellaneous'")

    # 2. Delete the 6 seeded L1 rows. If any service or category was linked to
    #    them between upgrade and downgrade, the FK ON services.category_id
    #    (and the SET NULL on categories.parent_id) fires — the SET NULL
    #    handles the parent_id case; a service pointing at an L1 row would
    #    error and abort the downgrade, which is the correct loud failure.
    slugs_to_delete = tuple(row[0] for row in _L1_ROWS)
    op.execute(
        sa.text("DELETE FROM categories WHERE slug IN :slugs").bindparams(
            sa.bindparam("slugs", slugs_to_delete, expanding=True)
        )
    )

    # 3. Drop the schema in reverse construction order.
    op.drop_index("idx_categories_parent_id", table_name="categories")
    op.drop_constraint("fk_categories_parent_id", "categories", type_="foreignkey")
    op.drop_column("categories", "parent_id")
