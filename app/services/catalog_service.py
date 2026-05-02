"""
Catalog service: admin-managed global service catalog + provider subscriptions.
"""
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.service import Service
from app.models.provider_service import ProviderService
from app.exceptions import ServiceNotFound, UnauthorizedAction


class CatalogService:
    """Manages categories and provider service listings."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def list_categories(self) -> list[Category]:
        result = await self.db.execute(
            select(Category)
            .where(Category.is_active.is_(True))
            .order_by(Category.sort_order)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------

    async def list_services(
        self,
        category_id: uuid.UUID | None = None,
    ) -> list[Service]:
        stmt = select(Service).where(Service.is_active.is_(True))
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
        return service

    async def update_service(
        self,
        service_id: uuid.UUID,
        **fields,
    ) -> Service:
        """Admin-only: update a global catalog service."""
        service = await self.db.get(Service, service_id)
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

    # ------------------------------------------------------------------
    # Provider subscriptions
    # ------------------------------------------------------------------

    async def subscribe_provider(
        self, provider_user_id: uuid.UUID, service_id: uuid.UUID
    ) -> ProviderService:
        """Subscribe a provider to a global service. Idempotent."""
        existing = await self.db.execute(
            select(ProviderService).where(
                ProviderService.provider_id == provider_user_id,
                ProviderService.service_id == service_id,
            )
        )
        if existing.scalar_one_or_none():
            return existing.scalar_one_or_none()

        service = await self.db.get(Service, service_id)
        if not service or not service.is_active:
            raise ServiceNotFound(service_id)

        sub = ProviderService(provider_id=provider_user_id, service_id=service_id)
        self.db.add(sub)
        await self.db.flush()
        return sub

    async def unsubscribe_provider(
        self, provider_user_id: uuid.UUID, service_id: uuid.UUID
    ) -> None:
        """Remove a provider's subscription to a service."""
        result = await self.db.execute(
            select(ProviderService).where(
                ProviderService.provider_id == provider_user_id,
                ProviderService.service_id == service_id,
            )
        )
        sub = result.scalar_one_or_none()
        if sub:
            await self.db.delete(sub)
            await self.db.flush()

    async def list_provider_subscriptions(
        self, provider_user_id: uuid.UUID
    ) -> list[Service]:
        """Return all global services a provider is subscribed to."""
        stmt = (
            select(Service)
            .join(ProviderService, ProviderService.service_id == Service.id)
            .where(ProviderService.provider_id == provider_user_id)
            .where(Service.is_active.is_(True))
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
