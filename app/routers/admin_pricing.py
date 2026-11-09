"""SVC-428: the admin pricing config surface. Roles in ADMIN.md.

Admin and ops edit, support reads. Every save that changes something is audited
(``pricing.category_update`` / ``pricing.config_update``). The values are inert
until the marketplace stories read them (SVC-410 commission, SVC-414 boost,
SVC-411 sheet TTL).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_roles
from app.models.user import User
from app.schemas.admin_pricing import (
    CategoryPricingRow,
    CategoryPricingUpdate,
    PricingConfigRow,
    PricingConfigUpdate,
)
from app.services.admin_audit import audit_actor
from app.services.admin_pricing_service import AdminPricingService

router = APIRouter(prefix="/admin/pricing", tags=["Admin"])

_any_admin_role_dep = Depends(require_roles("admin", "ops", "support"))


@router.get(
    "/categories", response_model=list[CategoryPricingRow], dependencies=[_any_admin_role_dep]
)
async def list_category_pricing(db: AsyncSession = Depends(get_db)):
    """Every category with its pricing config, each L1 followed by its L2s."""
    return await AdminPricingService(db).list_categories()


@router.patch("/categories/{category_id}", response_model=CategoryPricingRow)
async def update_category_pricing(
    category_id: UUID,
    body: CategoryPricingUpdate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Change any of the six fields. A refused value is a 422 ``PRICING_INVALID``
    naming the field in ``details.field``. Turning ``is_sensitive`` off clears
    ``bounds_pct``."""
    return await AdminPricingService(db).update_category(
        audit_actor(request, admin), category_id, body
    )


@router.get("/config", response_model=list[PricingConfigRow], dependencies=[_any_admin_role_dep])
async def list_pricing_config(db: AsyncSession = Depends(get_db)):
    """The ranking weights, boost window and sheet TTL."""
    return await AdminPricingService(db).list_config()


@router.patch("/config/{key}", response_model=PricingConfigRow)
async def update_pricing_config(
    key: str,
    body: PricingConfigUpdate,
    request: Request,
    admin: User = Depends(require_roles("admin", "ops")),
    db: AsyncSession = Depends(get_db),
):
    """Replace one key's value. 404 for an unknown key."""
    return await AdminPricingService(db).update_config(audit_actor(request, admin), key, body.value)
