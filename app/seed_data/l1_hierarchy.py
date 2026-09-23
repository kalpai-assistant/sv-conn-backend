"""SVC-286 category hierarchy — the canonical L1 tile list + L2→L1 mapping.

Both the alembic migration (``f2b8d31c5a67_svc_286_category_hierarchy``) and
``scripts/seed_categories.py`` import from here so a fresh DB and an already-
migrated DB always land on the same shape. Editing this file is how you
change the home-page tile set going forward; do NOT re-declare either list
inside a migration or a seed script (SVC-312).

Layout:

* ``L1_TILES`` — six illustrated tiles the consumer home renders, in the
  order they appear on screen (sort_order 0..5). ``miscellaneous`` is the
  seventh L1 but is intentionally NOT in this list: it already exists from
  the pre-SVC-286 catalogue and is promoted in place (its id/slug/services
  are preserved so historical bookings + provider subscriptions survive).
  Its slug + sort_order slot are named below so both sides can enforce
  them.

* ``L2_TO_L1`` — each of the 17 non-misc Level-2 slugs mapped to its L1
  parent slug. This is the FK backfill the migration ran; seed_categories
  re-applies it idempotently every run.

L1 slugs match the WebP asset filenames shipped in frontend PR #135 exactly
— editing the slugs would orphan any frontend code that looks them up (see
``category-images.ts``).
"""

from __future__ import annotations

# (slug, display_name, sort_order). The tuple form matches the migration's
# original _L1_ROWS so the alembic INSERT can iterate over it unchanged.
L1_TILES: list[tuple[str, str, int]] = [
    ("salon-spa-women", "Salon & Spa for Women", 0),
    ("salon-massage-men", "Salon & Massage for Men", 1),
    ("cleaning-pest-control", "Cleaning & Pest Control", 2),
    ("ac-appliance-repair", "AC & Appliance Repair", 3),
    ("electrician-plumber-carpenter", "Electrician, Plumber & Carpenter", 4),
    ("painting-home-improvement", "Painting & Home Improvement", 5),
]

# The 7th L1: existed pre-SVC-286, promoted in place. Not in L1_TILES so the
# migration downgrade path doesn't try to delete it (would blow up any
# booking referencing a service under it).
MISCELLANEOUS_L1_SLUG = "miscellaneous"
MISCELLANEOUS_L1_NAME = "Miscellaneous"
MISCELLANEOUS_L1_SORT_ORDER = 6

# Every L2 sub-category the app expects to render, in home-page display
# order under its parent L1. Tuples are (slug, display_name, parent_slug).
# ``seed_categories.py`` walks this list and creates any row that's
# missing, then re-asserts the ``parent_id`` FK on every row against
# ``parent_slug`` here. The display names match the workbook's category
# sheet names (see ``scripts/seed_pricing_from_xlsx.py``) so pricing rows
# link up by slug on the first run.
L2_TILES: list[tuple[str, str, str]] = [
    ("salon-women", "Salon - Women", "salon-spa-women"),
    ("nail-art", "Nail Art", "salon-spa-women"),
    ("salon-men", "Salon - Men", "salon-massage-men"),
    ("massage", "Massage", "salon-massage-men"),
    ("cleaning", "Cleaning", "cleaning-pest-control"),
    ("pest-control", "Pest Control", "cleaning-pest-control"),
    ("toilet-cleaning-commercial", "Toilet Cleaning (Commercial)", "cleaning-pest-control"),
    ("vehicle-wash-cleaning", "Vehicle Wash & Cleaning", "cleaning-pest-control"),
    ("ac", "AC", "ac-appliance-repair"),
    ("appliances", "Appliances", "ac-appliance-repair"),
    ("ro-purifier", "RO Purifier", "ac-appliance-repair"),
    ("electrician", "Electrician", "electrician-plumber-carpenter"),
    ("plumber", "Plumber", "electrician-plumber-carpenter"),
    ("carpenter", "Carpenter", "electrician-plumber-carpenter"),
    ("furniture", "Furniture", "electrician-plumber-carpenter"),
    ("painting", "Painting", "painting-home-improvement"),
    ("cctv-light-event", "CCTV & Light Event", "painting-home-improvement"),
]

# Convenience map — same content as L2_TILES, shape the alembic migration
# already iterates (slug → parent_slug). Kept as a public alias so both
# sides can pick whichever shape reads cleaner.
L2_TO_L1: dict[str, str] = {slug: parent for slug, _name, parent in L2_TILES}

# The full set of L2 slugs the app expects to exist. seed_pricing_from_xlsx
# checks this before touching anything so a re-run against a half-seeded DB
# fails loudly rather than wiping the hierarchy on the way in (SVC-312).
L2_SLUGS: frozenset[str] = frozenset(L2_TO_L1.keys())
