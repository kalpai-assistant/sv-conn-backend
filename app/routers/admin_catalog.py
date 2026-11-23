"""SVC-430: the admin catalog surface. Roles in ADMIN.md.

Admin and ops write, support reads. Every write that changes something is
audited (``category.create`` / ``update`` / ``deactivate`` / ``activate``,
``service.create`` / ``update`` / ``deactivate`` / ``activate``). Nothing is
deleted, a slug never changes, and only deactivate / activate switch a row.

SVC-481 adds the variant editor (``variant.create`` / ``update`` / ``deactivate``
/ ``activate``): rules in ``app/services/admin_variant_service.py``. SVC-486 adds
scheduled commission changes (``commission_change.schedule`` / ``cancel``).

The old ``POST/PATCH/DELETE /services`` stay as they were, except that neither
PATCH can change the price of a service with 2+ variants (SVC-481).
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_roles
from app.models.user import User
from app.schemas.admin_catalog import (
    AdminCatalogTree,
    AdminCategoryCreate,
    AdminCategoryDetail,
    AdminCategoryUpdate,
    AdminCommissionChange,
    AdminCommissionChangeCreate,
    AdminServiceCreate,
    AdminServiceDeactivate,
    AdminServiceDetail,
    AdminServiceUpdate,
    AdminVariantCreate,
    AdminVariantPriceImpact,
    AdminVariantUpdate,
)
from app.services.admin_audit import audit_actor
from app.services.admin_catalog_service import AdminCatalogService
from app.services.admin_variant_service import AdminVariantService

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


# -- variants (SVC-481) ---------------------------------------------------------


@router.post(
    "/services/{service_id}/variants",
    response_model=AdminServiceDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_variant(
    service_id: uuid.UUID,
    body: AdminVariantCreate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """A new active variant: label, price and commission percent, optionally
    duration and sort order. GST is copied from the service's first variant.
    409 ``VARIANT_LABEL_TAKEN`` if the service has that label in any case. The
    service's base_price then follows its cheapest active variant."""
    return await AdminVariantService(db).create(audit_actor(request, admin), service_id, body)


@router.patch("/services/{service_id}/variants/{variant_id}", response_model=AdminServiceDetail)
async def update_variant(
    service_id: uuid.UUID,
    variant_id: uuid.UUID,
    body: AdminVariantUpdate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Change label, price, commission percent, duration or sort order. A price
    on its own keeps the commission percent. Raising the percent is always a 409
    ``COMMISSION_INCREASE_NEEDS_NOTICE``: schedule it with ``POST
    .../commission-changes`` (SVC-486; ``confirm`` is ignored). A lower percent
    applies at once and the SPs pricing the option get a notice. Any change of
    percent while a change is scheduled is a 409 ``COMMISSION_CHANGE_SCHEDULED``.
    SP prices and existing bookings are not touched. A save that changes nothing
    is a 200 with no audit row."""
    return await AdminVariantService(db).update(
        audit_actor(request, admin), service_id, variant_id, body
    )


@router.post(
    "/services/{service_id}/variants/{variant_id}/commission-changes",
    response_model=AdminCommissionChange,
    status_code=status.HTTP_201_CREATED,
)
async def schedule_commission_change(
    service_id: uuid.UUID,
    variant_id: uuid.UUID,
    body: AdminCommissionChangeCreate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Schedule a new commission percent from ``effective_from`` (00:00 IST)
    (SVC-486). An increase needs that date at least 14 days after today (IST),
    else 422 ``CATALOG_INVALID`` on ``effective_from``. A decrease dated today
    applies at once. 409 ``COMMISSION_CHANGE_SCHEDULED`` while another one is
    scheduled. Every SP with an active price on the option gets a push, an
    email (when they have one) and an SMS. Each one is recorded."""
    return await AdminVariantService(db).schedule_commission_change(
        audit_actor(request, admin), service_id, variant_id, body
    )


@router.post(
    "/services/{service_id}/variants/{variant_id}/commission-changes/{change_id}/cancel",
    response_model=AdminCommissionChange,
)
async def cancel_commission_change(
    service_id: uuid.UUID,
    variant_id: uuid.UUID,
    change_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a scheduled commission change (SVC-486). 409
    ``COMMISSION_CHANGE_NOT_SCHEDULED`` once it has applied or been cancelled.
    The SPs who were told get a cancellation push and email."""
    return await AdminVariantService(db).cancel_commission_change(
        audit_actor(request, admin), service_id, variant_id, change_id
    )


@router.post(
    "/services/{service_id}/variants/{variant_id}/deactivate", response_model=AdminServiceDetail
)
async def deactivate_variant(
    service_id: uuid.UUID,
    variant_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Switch a variant off: it can't be booked and drops out of the SP lists.
    409 ``LAST_ACTIVE_VARIANT`` for the service's last active one. Already off:
    200, no audit row."""
    return await AdminVariantService(db).set_active(
        audit_actor(request, admin), service_id, variant_id, False
    )


@router.post(
    "/services/{service_id}/variants/{variant_id}/activate", response_model=AdminServiceDetail
)
async def activate_variant(
    service_id: uuid.UUID,
    variant_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Switch a variant back on, with the SP prices it had. Already on: 200, no
    audit row."""
    return await AdminVariantService(db).set_active(
        audit_actor(request, admin), service_id, variant_id, True
    )


@router.get(
    "/services/{service_id}/variants/{variant_id}/price-impact",
    response_model=AdminVariantPriceImpact,
    dependencies=[Depends(require_roles("admin", "ops"))],
)
async def variant_price_impact(
    service_id: uuid.UUID,
    variant_id: uuid.UUID,
    base_price: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """For the confirm dialog: how many SPs price this variant, and how many sit
    outside the recommended range now and at ``base_price``. Read-only."""
    return await AdminVariantService(db).price_impact(service_id, variant_id, base_price)
