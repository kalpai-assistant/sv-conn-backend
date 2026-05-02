from uuid import UUID
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.user import User
from app.schemas.service import (
    CreateServiceRequest, UpdateServiceRequest,
    ServiceResponse, CategoryResponse,
)
from app.services.catalog_service import CatalogService

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


@router.post("/services", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    request: CreateServiceRequest,
    current_user: User = Depends(require_role("admin")),  # Fix #3: admin-only
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: add a service to the global catalog."""
    return await CatalogService(db).create_service(
        category_id=request.category_id,
        name=request.name,
        description=request.description,
        base_price=request.base_price,
        duration_minutes=request.duration_minutes,
    )


@router.patch("/services/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: UUID,
    request: UpdateServiceRequest,
    current_user: User = Depends(require_role("admin")),  # Fix #3: admin-only
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: update a catalog service."""
    return await CatalogService(db).update_service(
        service_id=service_id,
        **request.model_dump(exclude_none=True),
    )


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: UUID,
    current_user: User = Depends(require_role("admin")),  # Fix #3: admin-only
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: soft-delete a catalog service."""
    await CatalogService(db).delete_service(service_id)


# ---------------------------------------------------------------------------
# Provider subscriptions — Fix #2
# ---------------------------------------------------------------------------

@router.get("/providers/me/services", response_model=list[ServiceResponse])
async def list_my_services(
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Provider: list all catalog services they are subscribed to."""
    return await CatalogService(db).list_provider_subscriptions(current_user.id)


@router.post(
    "/providers/me/services/{service_id}",
    response_model=ServiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def subscribe_to_service(
    service_id: UUID,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Provider: subscribe to a catalog service so dispatch can route bookings to them."""
    from app.models.service import Service
    await CatalogService(db).subscribe_provider(current_user.id, service_id)
    service = await db.get(Service, service_id)
    return service


@router.delete(
    "/providers/me/services/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unsubscribe_from_service(
    service_id: UUID,
    current_user: User = Depends(require_role("provider")),
    db: AsyncSession = Depends(get_db),
):
    """Provider: unsubscribe from a catalog service."""
    await CatalogService(db).unsubscribe_provider(current_user.id, service_id)
