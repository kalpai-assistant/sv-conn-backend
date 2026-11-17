"""The SP's own price per service variant (SVC-412, epic SVC-406).

An SP prices every active variant of every active service it subscribes to,
and switches each one on or off. ``sp_service_pricing`` holds one row per
(SP, variant); an SP is listed for a variant on the customer's sheet only with
an active row (``ranking_service``).

- The range is ``price_range``'s and the take-home is ``commission.take_home``,
  the helpers the ranking and the booking use, so the three can't disagree.
- An out-of-range price is saved and flagged, never refused (Vipul, decision 4).
- The SP's role is checked by the route (``require_role("provider")``); team
  Pros sign in as consumers, so they never get here.
- ``unsubscribe_from_service`` switches the rows off and keeps the prices.
"""

import logging
import uuid
from decimal import Decimal

from fastapi import status
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, SubscriptionNotFound
from app.models.category import Category
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.schemas.sp_pricing import (
    RecommendedRange,
    SpPricingResponse,
    SpServicePricingGroup,
    SpVariantPricing,
    SpVariantPricingUpdateResponse,
    TakeHome,
)
from app.services.commission import commission_pct, take_home
from app.services.price_range import is_outside_range, recommended_range

logger = logging.getLogger(__name__)

PRICE_REQUIRED = "PRICE_REQUIRED"
VARIANT_NOT_FOUND = "VARIANT_NOT_FOUND"


def variant_pricing(
    variant: ServiceVariant,
    *,
    price_unit: str,
    category_id: uuid.UUID,
    is_sensitive: bool,
    bounds_pct: Decimal | None,
    gst_pct: Decimal,
    price_amount: Decimal | None,
    is_active: bool | None,
) -> dict:
    """One variant's row on the SP's screen, as ``SpVariantPricing`` fields."""
    price_range = recommended_range(
        variant.base_price, is_sensitive, bounds_pct, category_id=category_id
    )
    breakdown = None
    if price_amount is not None:
        commission, gst, receives = take_home(variant, gst_pct, price_amount)
        breakdown = TakeHome(
            commission=float(commission), commission_gst=float(gst), sp_receives=float(receives)
        )
    return {
        "variant_id": variant.id,
        "label": variant.label,
        "platform_price": float(variant.base_price),
        "price_unit": price_unit,
        "recommended_range": (
            None
            if price_range is None
            else RecommendedRange(low=float(price_range[0]), high=float(price_range[1]))
        ),
        "price_amount": None if price_amount is None else float(price_amount),
        "is_active": is_active,
        "outside_recommended_range": (
            price_amount is not None and is_outside_range(price_amount, price_range)
        ),
        "take_home": breakdown,
        "commission_pct": float(commission_pct(variant)),
        "gst_pct": float(gst_pct),
    }


async def switch_off_all_prices(db: AsyncSession, sp_id: uuid.UUID) -> None:
    """Switch every price of an SP who leaves off (SVC-413): account deletion
    and admin deactivation, in the caller's transaction. The amounts stay, as on
    an unsubscribe, so ``is_active = true`` keeps meaning "on offer now"."""
    await db.execute(
        update(SpServicePricing)
        .where(SpServicePricing.sp_id == sp_id, SpServicePricing.is_active.is_(True))
        .values(is_active=False, updated_at=func.now())
    )


class SpPricingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_pricing(self, sp_id: uuid.UUID) -> SpPricingResponse:
        """Every active service the SP subscribes to, by name, each with its
        active variants in ``sort_order`` and the SP's price for each. One
        statement. A service whose variants are all switched off is listed
        with none."""
        rows = (
            await self.db.execute(
                select(
                    Service.id.label("service_id"),
                    Service.name.label("service_name"),
                    Service.price_unit,
                    Category.id.label("category_id"),
                    Category.is_sensitive,
                    Category.bounds_pct,
                    Category.commission_gst_pct,
                    ServiceVariant,
                    SpServicePricing.price_amount,
                    SpServicePricing.is_active.label("price_active"),
                )
                .join(
                    ProviderServiceModel,
                    (ProviderServiceModel.service_id == Service.id)
                    & (ProviderServiceModel.provider_id == sp_id),
                )
                .join(Category, Category.id == Service.category_id)
                .outerjoin(
                    ServiceVariant,
                    (ServiceVariant.service_id == Service.id) & ServiceVariant.is_active.is_(True),
                )
                .outerjoin(
                    SpServicePricing,
                    (SpServicePricing.sp_id == sp_id)
                    & (SpServicePricing.service_variant_id == ServiceVariant.id),
                )
                .where(Service.is_active.is_(True))
                .order_by(Service.name, Service.id, ServiceVariant.sort_order, ServiceVariant.id)
            )
        ).all()

        groups: dict[uuid.UUID, SpServicePricingGroup] = {}
        for row in rows:
            group = groups.get(row.service_id)
            if group is None:
                group = groups[row.service_id] = SpServicePricingGroup(
                    service_id=row.service_id,
                    service_name=row.service_name,
                    price_unit=row.price_unit,
                    variants=[],
                )
            if row.ServiceVariant is None:
                continue
            group.variants.append(
                SpVariantPricing(
                    **variant_pricing(
                        row.ServiceVariant,
                        price_unit=row.price_unit,
                        category_id=row.category_id,
                        is_sensitive=row.is_sensitive,
                        bounds_pct=row.bounds_pct,
                        gst_pct=row.commission_gst_pct,
                        price_amount=row.price_amount,
                        is_active=row.price_active,
                    )
                )
            )
        return SpPricingResponse(services=list(groups.values()))

    async def active_variant(self, variant_id: uuid.UUID):
        """``variant_id`` with its service's unit and category pricing knobs,
        or 404 ``VARIANT_NOT_FOUND`` if it's unknown, switched off, or its
        service is. Shared by ``set_price`` and the market insights (SVC-413)."""
        found = (
            await self.db.execute(
                select(
                    ServiceVariant,
                    Service.price_unit,
                    Category.id.label("category_id"),
                    Category.is_sensitive,
                    Category.bounds_pct,
                    Category.commission_gst_pct,
                )
                .join(Service, Service.id == ServiceVariant.service_id)
                .join(Category, Category.id == Service.category_id)
                .where(
                    ServiceVariant.id == variant_id,
                    ServiceVariant.is_active.is_(True),
                    Service.is_active.is_(True),
                )
            )
        ).one_or_none()
        if found is None:
            raise AppException(
                f"Variant {variant_id} not found",
                status_code=status.HTTP_404_NOT_FOUND,
                code=VARIANT_NOT_FOUND,
            )
        return found

    async def require_subscription(
        self, sp_id: uuid.UUID, service_id: uuid.UUID, *, lock: bool
    ) -> None:
        """404 ``SUBSCRIPTION_NOT_FOUND`` unless the SP subscribes to
        ``service_id``. With ``lock``, the subscription is read ``FOR SHARE``
        (see ``set_price``); a read needs no lock."""
        stmt = select(ProviderServiceModel.id).where(
            ProviderServiceModel.provider_id == sp_id,
            ProviderServiceModel.service_id == service_id,
        )
        if lock:
            stmt = stmt.with_for_update(read=True)
        if await self.db.scalar(stmt) is None:
            raise SubscriptionNotFound(service_id)

    async def set_price(
        self,
        sp_id: uuid.UUID,
        variant_id: uuid.UUID,
        *,
        price_amount: Decimal | None,
        is_active: bool | None,
    ) -> SpVariantPricingUpdateResponse:
        """Set the SP's price for ``variant_id``, switch it on or off, or both.

        - 404 ``VARIANT_NOT_FOUND``: unknown, switched off, or its service is.
        - 404 ``SUBSCRIPTION_NOT_FOUND``: the SP doesn't subscribe to its service.
          The subscription is read ``FOR SHARE``, so an unsubscribe waits for this
          save to commit and then switches the row off, or this save waits for
          the unsubscribe and 404s. A row can't be left on for a service the SP
          left.
        - With a price: one insert-or-update on ``uq_sp_service_pricing_sp_variant``,
          so two quick saves can't make two rows. A new row starts on unless
          ``is_active`` says otherwise; an existing one keeps its on/off unless
          ``is_active`` is sent. ``price_unit`` is refreshed from the service.
        - ``is_active`` alone on a variant with no price: 422 ``PRICE_REQUIRED``.
        """
        found = await self.active_variant(variant_id)
        variant = found.ServiceVariant
        service_id = variant.service_id
        await self.require_subscription(sp_id, service_id, lock=True)

        returning = (SpServicePricing.price_amount, SpServicePricing.is_active)
        if price_amount is not None:
            insert = pg_insert(SpServicePricing).values(
                id=uuid.uuid4(),
                sp_id=sp_id,
                service_id=service_id,
                service_variant_id=variant_id,
                price_amount=price_amount,
                price_unit=found.price_unit,
                is_active=True if is_active is None else is_active,
            )
            changes = {
                "price_amount": insert.excluded.price_amount,
                "price_unit": insert.excluded.price_unit,
                "updated_at": func.now(),
            }
            if is_active is not None:
                changes["is_active"] = insert.excluded.is_active
            stmt = insert.on_conflict_do_update(
                constraint="uq_sp_service_pricing_sp_variant", set_=changes
            ).returning(*returning)
        else:
            stmt = (
                update(SpServicePricing)
                .where(
                    SpServicePricing.sp_id == sp_id,
                    SpServicePricing.service_variant_id == variant_id,
                )
                .values(is_active=is_active, updated_at=func.now())
                .returning(*returning)
                .execution_options(synchronize_session=False)
            )
        saved = (await self.db.execute(stmt)).one_or_none()
        if saved is None:
            raise AppException(
                "Set a price for this option before switching it on or off.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code=PRICE_REQUIRED,
            )

        fields = variant_pricing(
            variant,
            price_unit=found.price_unit,
            category_id=found.category_id,
            is_sensitive=found.is_sensitive,
            bounds_pct=found.bounds_pct,
            gst_pct=found.commission_gst_pct,
            price_amount=saved.price_amount,
            is_active=saved.is_active,
        )
        logger.info(
            "sp.price_set sp_id=%s service_id=%s variant_id=%s price_amount=%s "
            "is_active=%s out_of_bounds=%s",
            sp_id,
            service_id,
            variant_id,
            saved.price_amount,
            saved.is_active,
            fields["outside_recommended_range"],
        )
        return SpVariantPricingUpdateResponse(service_id=service_id, **fields)
