"""SVC-430: the admin catalog surface. Roles in ADMIN.md.

Admin and ops write, support reads. Every write that changes something is
audited (``category.create`` / ``update`` / ``deactivate`` / ``activate``,
``service.create`` / ``update`` / ``deactivate`` / ``activate``). Nothing is
deleted, a slug never changes, and only deactivate / activate switch a row.

The old ``POST/PATCH/DELETE /services`` stay as they were.
"""

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_roles
from app.models.user import User
from app.schemas.admin_catalog import (
    AdminCatalogTree,
    AdminCategoryCreate,
    AdminCategoryDetail,
    AdminCategoryUpdate,
    AdminServiceCreate,
    AdminServiceDeactivate,
    AdminServiceDetail,
    AdminServiceUpdate,
)
from app.services.admin_audit import audit_actor
from app.services.admin_catalog_service import AdminCatalogService

router = APIRouter(prefix="/admin/catalog", tags=["Admin"])

_any_admin_role_dep = Depends(require_roles("admin", "ops", "support"))


@router.get("", response_model=AdminCatalogTree, dependencies=[_any_admin_role_dep])
async def get_catalog(db: AsyncSession = Depends(get_db)):
    """Every category and service, active or not: L1 → its L2s → their services."""
    return await AdminCatalogService(db).tree()


# -- categories -----------------------------------------------------------------


@router.post("/categories", response_model=AdminCategoryDetail, status_code=status.HTTP_201_CREATED)
async def create_category(
    body: AdminCategoryCreate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """A new L1 (no ``parent_id``) or L2. The slug is made from the name. 409
    ``CATALOG_NAME_TAKEN`` if the name or that slug is taken; a refused value is a
    422 ``CATALOG_INVALID`` naming ``details.field``."""
    return await AdminCatalogService(db).create_category(audit_actor(request, admin), body)


@router.get(
    "/categories/{category_id}",
    response_model=AdminCategoryDetail,
    dependencies=[_any_admin_role_dep],
)
async def get_category(category_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """One category with its sub-category and service counts. 404
    ``CATEGORY_NOT_FOUND``."""
    return await AdminCatalogService(db).category_detail(category_id)


@router.patch("/categories/{category_id}", response_model=AdminCategoryDetail)
async def update_category(
    category_id: uuid.UUID,
    body: AdminCategoryUpdate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Rename, move, re-sort or change the icon. The slug stays. A save that
    changes nothing is a 200 with no audit row."""
    return await AdminCatalogService(db).update_category(
        audit_actor(request, admin), category_id, body
    )


@router.post("/categories/{category_id}/deactivate", response_model=AdminCategoryDetail)
async def deactivate_category(
    category_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """409 ``CATEGORY_NOT_EMPTY`` with ``details.active_services`` and
    ``details.active_subcategories`` while either is above 0. Already off: 200,
    no audit row."""
    return await AdminCatalogService(db).set_category_active(
        audit_actor(request, admin), category_id, False
    )


@router.post("/categories/{category_id}/activate", response_model=AdminCategoryDetail)
async def activate_category(
    category_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Refused (422 on ``parent_id``) while its parent is inactive. Already on:
    200, no audit row."""
    return await AdminCatalogService(db).set_category_active(
        audit_actor(request, admin), category_id, True
    )


# -- services -------------------------------------------------------------------


@router.post("/services", response_model=AdminServiceDetail, status_code=status.HTTP_201_CREATED)
async def create_service(
    body: AdminServiceCreate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """A new service in a category that holds no sub-categories. 409
    ``CATALOG_NAME_TAKEN`` if the category already has that name in any case."""
    return await AdminCatalogService(db).create_service(audit_actor(request, admin), body)


@router.get(
    "/services/{service_id}", response_model=AdminServiceDetail, dependencies=[_any_admin_role_dep]
)
async def get_service(service_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """One service with every variant, active or not (read-only here). 404
    ``SERVICE_NOT_FOUND``."""
    return await AdminCatalogService(db).service_detail(service_id)


@router.patch("/services/{service_id}", response_model=AdminServiceDetail)
async def update_service(
    service_id: uuid.UUID,
    body: AdminServiceUpdate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Change any editable field, or move it to another category. A save that
    changes nothing is a 200 with no audit row."""
    return await AdminCatalogService(db).update_service(
        audit_actor(request, admin), service_id, body
    )


@router.post("/services/{service_id}/deactivate", response_model=AdminServiceDetail)
async def deactivate_service(
    service_id: uuid.UUID,
    request: Request,
    body: AdminServiceDeactivate | None = None,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """409 ``SERVICE_HAS_ACTIVE_BOOKINGS`` with ``details.active_bookings`` while
    bookings are in flight, unless the body is ``{"confirm": true}``. Those
    bookings carry on. Already off: 200, no audit row."""
    confirm = body.confirm if body is not None else False
    return await AdminCatalogService(db).deactivate_service(
        audit_actor(request, admin), service_id, confirm=confirm
    )


@router.post("/services/{service_id}/activate", response_model=AdminServiceDetail)
async def activate_service(
    service_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Refused (422 on ``category_id``) while its category is inactive. Already
    on: 200, no audit row."""
    return await AdminCatalogService(db).activate_service(audit_actor(request, admin), service_id)
