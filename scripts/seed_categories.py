"""
Seed initial categories with default icons and sort order.
Run: python scripts/seed_categories.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.category import Category

INITIAL_CATEGORIES = [
    {"name": "Home Cleaning", "slug": "home-cleaning", "icon_url": None, "sort_order": 1},
    {"name": "Plumbing", "slug": "plumbing", "icon_url": None, "sort_order": 2},
    {"name": "Electrical", "slug": "electrical", "icon_url": None, "sort_order": 3},
    {"name": "Salon & Beauty", "slug": "salon-beauty", "icon_url": None, "sort_order": 4},
    {"name": "Carpentry", "slug": "carpentry", "icon_url": None, "sort_order": 5},
    {"name": "Pest Control", "slug": "pest-control", "icon_url": None, "sort_order": 6},
    {"name": "AC Repair", "slug": "ac-repair", "icon_url": None, "sort_order": 7},
    {"name": "Appliance Repair", "slug": "appliance-repair", "icon_url": None, "sort_order": 8},
]


async def seed():
    async with AsyncSessionLocal() as db:
        for cat_data in INITIAL_CATEGORIES:
            existing = await db.execute(
                select(Category).where(Category.slug == cat_data["slug"])
            )
            if not existing.scalar_one_or_none():
                db.add(Category(**cat_data))
                print(f"  + Created category: {cat_data['name']}")
            else:
                print(f"  ~ Skipped (exists): {cat_data['name']}")
        await db.commit()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
