from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_roles
from app.models.service import Service
from app.models.user import User
from app.schemas.ranking import ServiceProvidersResponse
from app.schemas.service import (
    CategoryResponse,
    CreateServiceRequest,
    ServiceDetailResponse,
    ServiceResponse,
    UpdateServiceRequest,
)
from app.services.admin_audit import audit_actor, audit_admin_action, snapshot
from app.services.catalog_service import CatalogService
from app.services.ranking_service import RankingService

router = APIRouter(tags=["Catalog"])


# ---------------------------------------------------------------------------
# Categories (public)
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=list[CategoryResponse])
async def list_categories(db: AsyncSession = Depends(get_db)):
    return await CatalogService(db).list_categories()


# ---------------------------------------------------------------------------
# Services — public listing, admin CRUD
# ---------------------------------------------------------------------------


@router.get("/services", response_model=list[ServiceResponse])
async def list_services(
    category_id: UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List global catalog services, optionally filtered by category."""
    return await CatalogService(db).list_services(category_id=category_id)


@router.get("/services/{service_id}", response_model=ServiceDetailResponse)
async def get_service(
    service_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """One service + variants + the three related-services buckets (SVC-291).

    Distinct from the list route because the related payload is only worth
    the extra joins on a detail page — a home-page listing that renders
    just name / price / duration does not need the rails, and joining for
    them there would multiply the query cost of the app's biggest single
    request.
    """
    return await CatalogService(db).get_service_detail(service_id)


@router.get("/services/{service_id}/providers", response_model=ServiceProvidersResponse)
async def rank_service_providers(
    service_id: UUID,
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    limit: int = Query(50, ge=1, le=50),
    variant_id: UUID | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The SPs a customer at (lat, lng) can book this service with, best first
    (SVC-409): the ones dispatch would offer it to that have an active price
    for the variant, each with its price, signals, score and up to three "why"
    chips.

    Prices are per variant (SVC-480): ``variant_id``, else the service's first
    active variant, the one a booking without a variant is billed for. A service
    whose variants are all switched off lists nobody.

    Needs a login, unlike the catalog: each row's distance is measured from the
    SP's service-area centre. 404 for an unknown or inactive service, and for a
    ``variant_id`` that isn't an active variant of this service.
    """
    return await RankingService(db).rank_providers(service_id, lat, lng, limit, variant_id)


@router.post("/services", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    request: CreateServiceRequest,
    http_request: Request,
    current_user: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Admin or ops: add a service to the global catalog."""
    service = await CatalogService(db).create_service(
        category_id=request.category_id,
        name=request.name,
        description=request.description,
        base_price=request.base_price,
        duration_minutes=request.duration_minutes,
    )
    await audit_admin_action(
        db,
        audit_actor(http_request, current_user),
        action="service.create",
        target_type="service",
        target_id=service.id,
        after=snapshot("service", service),
    )
    return service


@router.patch("/services/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: UUID,
    request: UpdateServiceRequest,
    http_request: Request,
    current_user: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Admin or ops: update a catalog service."""
    before = snapshot("service", await db.get(Service, service_id))
    service, synced_variant = await CatalogService(db).update_service(
        service_id=service_id,
        **request.model_dump(exclude_none=True),
    )
    after = snapshot("service", service)
    if synced_variant is not None:
        # SVC-480: the single variant followed the price, in this same row.
        before["variant"], after["variant"] = synced_variant
    await audit_admin_action(
        db,
        audit_actor(http_request, current_user),
        action="service.update",
        target_type="service",
        target_id=service_id,
        before=before,
        after=after,
    )
    return service


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: UUID,
    http_request: Request,
    current_user: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Admin or ops: soft-delete a catalog service."""
    before = snapshot("service", await db.get(Service, service_id))
    await CatalogService(db).delete_service(service_id)
    await audit_admin_action(
        db,
        audit_actor(http_request, current_user),
        action="service.delete",
        target_type="service",
        target_id=service_id,
        before=before,
    )
