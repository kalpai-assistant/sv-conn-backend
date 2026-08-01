"""
User service: profile management and address CRUD.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import UserNotFound
from app.models.address import Address
from app.models.user import User
from app.utils.geo import build_point_wkt


class UserService:
    """Manages user profile and address data."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    async def get_profile(self, user_id: uuid.UUID) -> User:
        user = await self.db.get(User, user_id)
        if not user:
            raise UserNotFound(user_id)
        return user

    async def update_profile(self, user_id: uuid.UUID, name: str | None, email: str | None) -> User:
        user = await self.get_profile(user_id)
        if name is not None:
            user.name = name
        if email is not None:
            user.email = email
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def update_fcm_token(self, user_id: uuid.UUID, token: str) -> None:
        user = await self.get_profile(user_id)
        user.fcm_token = token
        await self.db.flush()

    # ------------------------------------------------------------------
    # Addresses
    # ------------------------------------------------------------------

    async def list_addresses(self, user_id: uuid.UUID) -> list[Address]:
        result = await self.db.execute(select(Address).where(Address.user_id == user_id))
        return list(result.scalars().all())

    async def add_address(
        self,
        user_id: uuid.UUID,
        label: str,
        full_address: str,
        lat: float | None,
        lng: float | None,
        is_default: bool,
    ) -> Address:
        if is_default:
            await self._clear_default(user_id)

        location = build_point_wkt(lat, lng) if lat is not None and lng is not None else None
        address = Address(
            user_id=user_id,
            label=label,
            full_address=full_address,
            location=location,
            is_default=is_default,
        )
        self.db.add(address)
        await self.db.flush()
        return address

    async def delete_address(self, user_id: uuid.UUID, address_id: uuid.UUID) -> None:
        result = await self.db.execute(
            select(Address).where(Address.id == address_id, Address.user_id == user_id)
        )
        address = result.scalar_one_or_none()
        if address:
            await self.db.delete(address)
            await self.db.flush()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _clear_default(self, user_id: uuid.UUID) -> None:
        result = await self.db.execute(
            select(Address).where(Address.user_id == user_id, Address.is_default.is_(True))
        )
        for addr in result.scalars().all():
            addr.is_default = False
