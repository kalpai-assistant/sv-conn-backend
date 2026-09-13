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
from app.models.service_relationship import RelationshipType, ServiceRelationship
from app.models.service_variant import ServiceVariant
from app.schemas.service import (
    RelatedServiceEntry,
    RelatedServicesBucket,
    ServiceDetailResponse,
    ServiceVariantResponse,
)


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

    # ------------------------------------------------------------------
    # SVC-291: single-service fetch with related-services rails prefetched
    # ------------------------------------------------------------------

    async def get_service_detail(self, service_id: uuid.UUID) -> ServiceDetailResponse:
        """One service + its variants + the three related-services buckets.

        Sits alongside ``list_services`` rather than replacing it because the
        list route (called on every home page load) doesn't need the related
        payload, and paying for it there would multiply the query count on a
        surface that's already the app's biggest single request. Fetching it
        only here keeps the list lean and the detail complete.

        The related rails are pulled in one round-trip via a join against
        ``services`` — three per-type buckets are grouped in Python from the
        single result. Inactive related services are excluded to match how
        ``list_services`` treats the catalogue: a hidden service must not
        surface anywhere the app renders it as bookable.
        """
        stmt = (
            select(Service)
            .where(Service.id == service_id, Service.is_active.is_(True))
            .options(selectinload(Service.variants.and_(ServiceVariant.is_active.is_(True))))
        )
        service = (await self.db.execute(stmt)).scalar_one_or_none()
        if not service:
            raise ServiceNotFound(service_id)

        related = await self._load_related(service_id)
        return ServiceDetailResponse(
            id=service.id,
            category_id=service.category_id,
            name=service.name,
            description=service.description,
            base_price=float(service.base_price),
            duration_minutes=service.duration_minutes,
            is_active=service.is_active,
            created_at=service.created_at,
            variants=[ServiceVariantResponse.model_validate(v) for v in service.variants],
            related=related,
            banner_image_url=service.banner_image_url,
            banner_video_url=service.banner_video_url,
        )

    async def _load_related(self, service_id: uuid.UUID) -> RelatedServicesBucket:
        """Fetch every edge out of `service_id` and bucket by relationship_type.

        One query, joined to ``services`` so we get name + base_price in the
        same round trip — the frontend renders the rails without a second
        request. Rows pointing at a now-inactive service are dropped in the
        WHERE clause so a soft-deleted catalogue entry never surfaces on a
        rail. Sort order comes from the seed / hand-curated overrides.
        """
        rows = await self.db.execute(
            select(ServiceRelationship, Service)
            .join(Service, Service.id == ServiceRelationship.related_service_id)
            .where(
                ServiceRelationship.source_service_id == service_id,
                Service.is_active.is_(True),
            )
            .order_by(
                ServiceRelationship.relationship_type,
                ServiceRelationship.sort_order,
                Service.name,
            )
        )

        buckets: dict[str, list[RelatedServiceEntry]] = {
            RelationshipType.HOOK.value: [],
            RelationshipType.UPSELL.value: [],
            RelationshipType.RELATED.value: [],
        }
        for edge, related_service in rows.all():
            bucket = buckets.get(edge.relationship_type)
            # Unknown relationship_type (e.g. a future 4th surface not yet in
            # the enum): drop silently rather than surfacing it on a rail the
            # frontend can't render. The DB is the source of truth for what
            # the model knows about.
            if bucket is None:
                continue
            bucket.append(
                RelatedServiceEntry(
                    id=related_service.id,
                    name=related_service.name,
                    base_price=float(related_service.base_price),
                    sort_order=edge.sort_order,
                )
            )

        return RelatedServicesBucket(
            hook=buckets[RelationshipType.HOOK.value],
            upsell=buckets[RelationshipType.UPSELL.value],
            related=buckets[RelationshipType.RELATED.value],
        )
