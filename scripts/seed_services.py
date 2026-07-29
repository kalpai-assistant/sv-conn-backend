"""
Seed initial active catalog services under existing categories.
Run AFTER seed_categories.py:
    python scripts/seed_categories.py
    python scripts/seed_services.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.category import Category
from app.models.service import Service

# Catalog data lives in JSON (keyed by category slug, must match
# seed_categories.py) so it can be edited without touching the seeding logic.
_DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "initial_services.json")

with open(_DATA_FILE, encoding="utf-8") as f:
    INITIAL_SERVICES = json.load(f)


async def seed():
    async with AsyncSessionLocal() as db:
        for slug, services in INITIAL_SERVICES.items():
            cat = (
                await db.execute(select(Category).where(Category.slug == slug))
            ).scalar_one_or_none()
            if cat is None:
                print(f"  ! Category missing: {slug} — run seed_categories.py first. Skipping.")
                continue

            for svc in services:
                existing = (
                    await db.execute(
                        select(Service).where(
                            Service.category_id == cat.id, Service.name == svc["name"]
                        )
                    )
                ).scalar_one_or_none()
                if existing:
                    print(f"  ~ Skipped (exists): {svc['name']}")
                    continue
                db.add(Service(category_id=cat.id, is_active=True, **svc))
                print(f"  + Created service: {svc['name']} ({slug})")
        await db.commit()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
