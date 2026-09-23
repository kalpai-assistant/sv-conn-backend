"""Seed the SVC-286 category hierarchy.

Idempotent — re-runs are a no-op for categories that already exist and
just re-assert ``parent_id`` links on the 17 L2 slugs. It never deletes.
Safe to run against a freshly-migrated DB (populates from empty), a DB
recovering from the SVC-312 wipe (fills in the L1 tiles), or a healthy
production DB (touches nothing).

Ritual (SVC-312):
    uv run alembic upgrade head
    uv run python scripts/seed_categories.py       # this file
    uv run python scripts/seed_services.py         # curated names
    uv run python scripts/seed_pricing_from_xlsx.py  # pricing + variants

Shape produced:
    7 L1 tiles (parent_id NULL): the 6 from L1_TILES + 'miscellaneous'
    17 L2 sub-categories (parent_id → L1 by slug per L2_TO_L1)

The L1 slugs, L2 slugs and mappings all live in
``app.seed_data.l1_hierarchy`` so this script and the alembic migration
``f2b8d31c5a67_svc_286_category_hierarchy`` cannot drift apart.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.category import Category
from app.seed_data.l1_hierarchy import (
    L1_TILES,
    L2_TILES,
    MISCELLANEOUS_L1_NAME,
    MISCELLANEOUS_L1_SLUG,
    MISCELLANEOUS_L1_SORT_ORDER,
)


async def _upsert_l1(db, slug: str, name: str, sort_order: int) -> Category:
    """Return the L1 row for ``slug``, creating it if it's missing."""
    existing = (
        await db.execute(select(Category).where(Category.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        print(f"  ~ L1 exists: {slug}")
        return existing
    cat = Category(
        slug=slug,
        name=name,
        sort_order=sort_order,
        is_active=True,
        parent_id=None,
    )
    db.add(cat)
    await db.flush()
    print(f"  + L1 created: {slug}")
    return cat


async def _upsert_l2(db, slug: str, name: str, parent_id, sort_order: int) -> None:
    """Create an L2 row if missing, otherwise re-assert its parent link.

    The row keeps its existing id + display name if it already exists (so
    ``services.category_id`` references don't change under running
    workloads); only ``parent_id`` and ``sort_order`` are touched. If the
    row is missing entirely we create it with the canonical name from
    ``L2_TILES``.
    """
    row = (await db.execute(select(Category).where(Category.slug == slug))).scalar_one_or_none()
    if row is None:
        db.add(
            Category(
                slug=slug,
                name=name,
                sort_order=sort_order,
                is_active=True,
                parent_id=parent_id,
            )
        )
        print(f"  + L2 created: {slug} → parent {parent_id}")
        return
    if row.parent_id != parent_id:
        row.parent_id = parent_id
        print(f"  ~ L2 relinked: {slug} → parent {parent_id}")
    else:
        print(f"  ~ L2 exists:  {slug}")


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        # 1. Miscellaneous L1 — pre-existing in prod. Create if missing (fresh
        #    dev DB), otherwise re-assert the SVC-286 sort_order slot so any
        #    later reseed can't accidentally push it back to its old flat slot.
        misc = (
            await db.execute(select(Category).where(Category.slug == MISCELLANEOUS_L1_SLUG))
        ).scalar_one_or_none()
        if misc is None:
            misc = Category(
                slug=MISCELLANEOUS_L1_SLUG,
                name=MISCELLANEOUS_L1_NAME,
                sort_order=MISCELLANEOUS_L1_SORT_ORDER,
                is_active=True,
                parent_id=None,
            )
            db.add(misc)
            await db.flush()
            print(f"  + L1 created: {MISCELLANEOUS_L1_SLUG}")
        else:
            misc.sort_order = MISCELLANEOUS_L1_SORT_ORDER
            misc.parent_id = None
            print(f"  ~ L1 exists: {MISCELLANEOUS_L1_SLUG}")

        # 2. The 6 illustrated L1 tiles from L1_TILES.
        l1_by_slug: dict[str, Category] = {MISCELLANEOUS_L1_SLUG: misc}
        for slug, name, sort_order in L1_TILES:
            l1_by_slug[slug] = await _upsert_l1(db, slug, name, sort_order)

        # 3. The 17 Level-2 sub-categories. Create any that are missing (fresh
        #    dev DB) and re-assert the parent_id link on any that already
        #    exist. sort_order matches the position of the L2 within
        #    ``L2_TILES`` so the sub-category rail renders in a stable order.
        for idx, (slug, name, parent_slug) in enumerate(L2_TILES):
            await _upsert_l2(db, slug, name, l1_by_slug[parent_slug].id, sort_order=idx)

        await db.commit()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
