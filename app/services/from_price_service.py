"""
The service tile's "from ₹X" at the customer's point: ``GET /services/from-prices``
(SVC-415, decisions in Jira 14911 / 14939).

**from_price**: the lowest active price among the SPs the "pick your SP" sheet
(SVC-409) could list right now: every dispatch stage at the point, subscribed,
with an active ``sp_service_pricing`` row on an active variant of an active
service. In/out of the recommended range is decided per variant with the shared
helper (``app.services.price_range``), never a second copy of the rule. The
lowest in-range price across the service's variants wins; only if no variant
has an in-range price does the lowest out-of-range one count (14911), so the
tile never advertises a price that sits at the bottom of the sheet with a
warning while an in-range one exists. Nobody listable: NULL.

**served**: some SP covers the point with an active price on an active variant,
online or not (the ``SERVED_STAGES`` from ``provider_matching``). True whenever
``from_price`` is set, because listable SPs are a subset of serving ones.

**One statement** whatever the SP or service count: every active service, left
joined to its prices grouped per (service, variant). The grouped side keeps only
SPs passing the served stages; within it, ``array_agg ... FILTER`` collects the
sorted distinct prices of the SPs that also pass the remaining (listable-only)
stages. Python then picks the in-range minimum per variant. Both stage sets are
picked by name from dispatch's one stage list, so neither can drift from it.

**Cache**: the whole response per point, in Redis under
``service_from_prices:{lat:.4f}:{lng:.4f}`` (~11 m) for 10 minutes. It is
computed at the request's own point, and nothing clears it, so a new price or
an SP going online shows within 10 minutes. A Redis error on read or write is
logged and the request is computed uncached.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import and_, distinct, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.provider_profile import ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.models.user import User
from app.schemas.from_prices import ServiceFromPrice
from app.services.price_range import is_outside_range, recommended_range
from app.services.provider_matching import (
    listable_only_stages,
    serves_area_conditions,
    stage_conditions,
)
from app.utils.geo import build_point_wkt

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600
CACHE_PREFIX = "service_from_prices"


def _coordinate(value: float) -> str:
    """Four decimals (~11 m). ``+ 0.0`` folds -0.0 into 0.0, so a point just
    either side of the equator or meridian doesn't get two keys."""
    return f"{round(value, 4) + 0.0:.4f}"


def cache_key(lat: float, lng: float) -> str:
    return f"{CACHE_PREFIX}:{_coordinate(lat)}:{_coordinate(lng)}"


def from_prices_stmt(point_wkt: str):
    """Every active service, with one row per variant that a serving SP prices.

    A service no serving SP prices comes back once with NULL ``variant_id``.
    ``listable_prices`` is NULL when nobody passing every stage prices that
    variant. ``ProviderProfile`` and ``User`` stay un-aliased inside the grouped
    side: the ``has_someone_to_do_it`` EXISTS clauses correlate on them by name.
    """
    listable_now = and_(*stage_conditions(point_wkt, listable_only_stages()))
    priced = (
        select(
            SpServicePricing.service_id.label("service_id"),
            SpServicePricing.service_variant_id.label("variant_id"),
            ServiceVariant.base_price.label("base_price"),
            Category.id.label("category_id"),
            Category.is_sensitive.label("is_sensitive"),
            Category.bounds_pct.label("bounds_pct"),
            func.array_agg(
                aggregate_order_by(
                    distinct(SpServicePricing.price_amount), SpServicePricing.price_amount
                )
            )
            .filter(listable_now)
            .label("listable_prices"),
        )
        .select_from(SpServicePricing)
        .join(ServiceVariant, ServiceVariant.id == SpServicePricing.service_variant_id)
        .join(Service, Service.id == SpServicePricing.service_id)
        .join(Category, Category.id == Service.category_id)
        .join(ProviderProfile, ProviderProfile.user_id == SpServicePricing.sp_id)
        .join(User, User.id == SpServicePricing.sp_id)
        .join(ProviderServiceModel, ProviderServiceModel.provider_id == SpServicePricing.sp_id)
        .where(
            SpServicePricing.is_active.is_(True),
            ServiceVariant.is_active.is_(True),
            Service.is_active.is_(True),
            *serves_area_conditions(SpServicePricing.service_id, point_wkt),
        )
        .group_by(
            SpServicePricing.service_id,
            SpServicePricing.service_variant_id,
            ServiceVariant.base_price,
            Category.id,
            Category.is_sensitive,
            Category.bounds_pct,
        )
        .subquery("priced")
    )
    return (
        select(
            Service.id.label("service_id"),
            priced.c.variant_id,
            priced.c.base_price,
            priced.c.category_id,
            priced.c.is_sensitive,
            priced.c.bounds_pct,
            priced.c.listable_prices,
        )
        .outerjoin(priced, priced.c.service_id == Service.id)
        .where(Service.is_active.is_(True))
        .order_by(Service.id)
    )


@dataclass
class _Tile:
    served: bool = False
    lowest_in_range: Decimal | None = None
    lowest_any: Decimal | None = None

    def add_variant(self, prices: list[Decimal], price_range) -> None:
        """One variant's sorted listable prices, under its own range."""
        self.served = True
        if not prices:
            return
        inside = next((p for p in prices if not is_outside_range(p, price_range)), None)
        if inside is not None and (self.lowest_in_range is None or inside < self.lowest_in_range):
            self.lowest_in_range = inside
        if self.lowest_any is None or prices[0] < self.lowest_any:
            self.lowest_any = prices[0]

    @property
    def from_price(self) -> Decimal | None:
        # With nothing in range anywhere, every listable price is out of range,
        # so the lowest of all of them is the lowest out-of-range one (14911).
        return self.lowest_in_range if self.lowest_in_range is not None else self.lowest_any


class FromPriceService:
    def __init__(self, db: AsyncSession, redis: Redis):
        self.db = db
        self.redis = redis

    async def from_prices(self, lat: float, lng: float) -> list[ServiceFromPrice]:
        key = cache_key(lat, lng)
        try:
            cached = await self.redis.get(key)
            if cached is not None:
                return [ServiceFromPrice.model_validate(item) for item in json.loads(cached)]
        except Exception as exc:  # never fail the request on the cache
            logger.warning("from_prices.cache_read_failed key=%s error=%r", key, exc)

        result = await self._compute(lat, lng)
        try:
            payload = json.dumps([item.model_dump(mode="json") for item in result])
            await self.redis.set(key, payload, ex=CACHE_TTL_SECONDS)
        except Exception as exc:  # never fail the request on the cache
            logger.warning("from_prices.cache_write_failed key=%s error=%r", key, exc)
        return result

    async def _compute(self, lat: float, lng: float) -> list[ServiceFromPrice]:
        rows = await self.db.execute(from_prices_stmt(build_point_wkt(lat, lng)))
        tiles: dict[uuid.UUID, _Tile] = {}
        for row in rows:
            tile = tiles.setdefault(row.service_id, _Tile())
            if row.variant_id is None:
                continue  # no serving SP prices this service
            price_range = recommended_range(
                row.base_price, row.is_sensitive, row.bounds_pct, category_id=row.category_id
            )
            tile.add_variant(list(row.listable_prices or []), price_range)
        return [
            ServiceFromPrice(
                service_id=service_id,
                from_price=None if tile.from_price is None else float(tile.from_price),
                served=tile.served,
            )
            for service_id, tile in tiles.items()
        ]
