"""
Catalog service: admin-managed global service catalog.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.exceptions import ServiceNotFound
from app.models.category import Category
from app.models.service import Service
from app.models.service_variant import ServiceVariant


class CatalogService:
    """Manages categories and provider service listings."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def list_categories(self) -> list[Category]:
        result = await self.db.execute(
            select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------

    async def list_services(
        self,
        category_id: uuid.UUID | None = None,
    ) -> list[Service]:
        # Eager-load variants because ServiceResponse embeds them + the
        # relationship is `lazy="raise_on_sql"` — pydantic would otherwise
        # fault on serialisation, and even if it didn't, the read path would
        # fire one query per service (N+1) on a listing that can return
        # dozens of rows. `.options(selectinload(...))` fetches all variants
        # in one round-trip keyed by service_id.
        stmt = (
            select(Service)
            .where(Service.is_active.is_(True))
            .options(selectinload(Service.variants.and_(ServiceVariant.is_active.is_(True))))
        )
        if category_id:
            stmt = stmt.where(Service.category_id == category_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def create_service(
        self,
        category_id: uuid.UUID,
        name: str,
        description: str | None,
        base_price: float,
        duration_minutes: int,
    ) -> Service:
        """Admin-only: create a global catalog service."""
        service = Service(
            category_id=category_id,
            name=name,
            description=description,
            base_price=base_price,
            duration_minutes=duration_minutes,
        )
        self.db.add(service)
        await self.db.flush()
        # ServiceResponse embeds .variants, and the relationship is
        # `raise_on_sql`. A brand-new service has no variants — mark the
        # collection as loaded-empty so pydantic can serialise it without
        # tripping the sentinel. (Admin adds variants via a separate flow.)
        set_committed_value(service, "variants", [])
        return service

    async def update_service(
        self,
        service_id: uuid.UUID,
        **fields,
    ) -> Service:
        """Admin-only: update a global catalog service."""
        # Load with variants eagerly so the response can render them
        # without a raise_on_sql fault at serialisation time.
        stmt = (
            select(Service).where(Service.id == service_id).options(selectinload(Service.variants))
        )
        service = (await self.db.execute(stmt)).scalar_one_or_none()
        if not service:
            raise ServiceNotFound(service_id)
        for key, value in fields.items():
            if value is not None and hasattr(service, key):
                setattr(service, key, value)
        await self.db.flush()
        return service

    async def delete_service(self, service_id: uuid.UUID) -> None:
        """Admin-only: soft-delete by marking inactive."""
        service = await self.db.get(Service, service_id)
        if not service:
            raise ServiceNotFound(service_id)
        service.is_active = False
        await self.db.flush()
