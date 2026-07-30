"""
Create a super admin user.
Run: python scripts/create_admin.py +91XXXXXXXXXX
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.user import User, UserRole


async def create_admin(phone: str):
    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(User).where(User.phone == phone))
        user = existing.scalar_one_or_none()

        if user:
            user.role = UserRole.ADMIN
            user.is_active = True
            print(f"Updated existing user {phone} → role=admin")
        else:
            user = User(phone=phone, role=UserRole.ADMIN, name="Super Admin")
            db.add(user)
            print(f"Created admin user: {phone}")

        await db.commit()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/create_admin.py +91XXXXXXXXXX")
        sys.exit(1)
    asyncio.run(create_admin(sys.argv[1]))
