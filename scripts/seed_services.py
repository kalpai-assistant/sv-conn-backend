"""
Seed initial active catalog services under existing categories.
Run AFTER seed_categories.py:
    python scripts/seed_categories.py
    python scripts/seed_services.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.category import Category
from app.models.service import Service

# Keyed by category slug (must match seed_categories.py).
INITIAL_SERVICES = {
    "home-cleaning": [
        {"name": "Full Home Deep Cleaning", "description": "Top-to-bottom deep clean", "base_price": 1499, "duration_minutes": 180},
        {"name": "Bathroom Cleaning", "description": "Sanitised bathroom cleaning", "base_price": 499, "duration_minutes": 60},
    ],
    "plumbing": [
        {"name": "Tap & Pipe Repair", "description": "Leak and fitting repair", "base_price": 299, "duration_minutes": 45},
    ],
    "electrical": [
        {"name": "Switch & Socket Repair", "description": "Wiring and socket fixes", "base_price": 249, "duration_minutes": 30},
    ],
    "ac-repair": [
        {"name": "AC Service", "description": "Standard split/window AC service", "base_price": 599, "duration_minutes": 60},
    ],
    "salon-beauty": [
        {"name": "Salon at Home", "description": "Haircut, grooming & styling", "base_price": 799, "duration_minutes": 90},
    ],
}


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
