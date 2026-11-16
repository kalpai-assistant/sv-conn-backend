"""Every service has at least one variant (SVC-480).

An SP's price points at a variant (``sp_service_pricing.service_variant_id`` is
NOT NULL), so a service with none couldn't be priced at all. Both admin create
paths add a "Standard" variant with the service's price, and the SVC-480
migration added one to every service that had none.

The split is ``create_booking``'s legacy one, for a service with no active
variant: commission = ``PLATFORM_COMMISSION_PERCENT`` of the price, rounded to
paise, and the SP gets the rest. A booking on the new variant therefore costs
and pays exactly what the legacy branch would have charged for one unit.

``create_booking`` itself is unchanged: its legacy branch stays as a safety net
for a service whose variants are all switched off.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.utils.money import to_decimal

STANDARD_VARIANT_LABEL = "Standard"


def legacy_split(price: Decimal | float | int | str) -> tuple[Decimal, Decimal]:
    """``(platform_commission, provider_payout)`` for one unit at ``price``, as
    ``create_booking``'s legacy branch computes them."""
    rate = to_decimal(get_settings().PLATFORM_COMMISSION_PERCENT) / 100
    price = to_decimal(price)
    commission = to_decimal(price * rate)
    return commission, price - commission


def standard_variant(service: Service) -> ServiceVariant:
    """The default variant for a new service: its price, duration and the legacy
    split. Add it in the same flush as the service."""
    commission, payout = legacy_split(service.base_price)
    return ServiceVariant(
        id=uuid.uuid4(),
        service_id=service.id,
        label=STANDARD_VARIANT_LABEL,
        subcategory=None,
        base_price=to_decimal(service.base_price),
        provider_payout=payout,
        platform_commission=commission,
        gst_percent=Decimal("0"),
        duration_minutes=service.duration_minutes,
        sort_order=0,
        is_active=True,
    )


def repriced_split(variant: ServiceVariant, new_price: Decimal) -> tuple[Decimal, Decimal]:
    """``(platform_commission, provider_payout)`` for ``variant`` at
    ``new_price``, keeping its commission-to-price ratio. A variant priced 0 has
    no ratio, so it gets the legacy split."""
    new_price = to_decimal(new_price)
    old_price = to_decimal(variant.base_price)
    if old_price == 0:
        return legacy_split(new_price)
    commission = to_decimal(new_price * to_decimal(variant.platform_commission) / old_price)
    return commission, new_price - commission


async def sync_single_variant_price(
    db: AsyncSession, service_id: uuid.UUID, new_price: Decimal
) -> tuple[dict, dict] | None:
    """When a service's price changes and it has exactly one variant row
    (active or not), move that variant to the new price too, so a booking
    charges what the admin just set. Returns the variant's ``(before, after)``
    for the audit row, or None when nothing changed: 0 or 2+ variants (a variant
    editor is a separate ticket), or the variant already at that price."""
    variants = (
        await db.scalars(
            select(ServiceVariant)
            .where(ServiceVariant.service_id == service_id)
            .with_for_update()
            .limit(2)
        )
    ).all()
    if len(variants) != 1:
        return None
    [variant] = variants
    new_price = to_decimal(new_price)
    if to_decimal(variant.base_price) == new_price:
        return None
    commission, payout = repriced_split(variant, new_price)
    before = _money(variant)
    variant.base_price = new_price
    variant.platform_commission = commission
    variant.provider_payout = payout
    await db.flush()
    return before, _money(variant)


def _money(variant: ServiceVariant) -> dict:
    return {
        "id": str(variant.id),
        "base_price": to_decimal(variant.base_price),
        "platform_commission": to_decimal(variant.platform_commission),
        "provider_payout": to_decimal(variant.provider_payout),
    }
