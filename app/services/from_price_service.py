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

**variants** (SVC-496): every active variant of the service, in sort_order,
each with the same rule applied to it alone: its lowest in-range listable
price, else its lowest listable price, else NULL (the sheet greys it out), and
``outside_recommended_range`` when that value is the out-of-range fallback. Same
stages, same point, same range helper. The service's ``from_price`` is built
from the same per-variant values (``_Tile``), so the two can't disagree, but it
is not min(variants): one variant's out-of-range price can be lower than the
in-range price the service shows.

**served**: some SP covers the point with an active price on an active variant,
online or not (the ``SERVED_STAGES`` from ``provider_matching``). True whenever
``from_price`` is set, because listable SPs are a subset of serving ones.

**One statement** whatever the SP or service count: every active service, left
joined to its active variants, left joined to their prices grouped per variant,
so an unpriced variant still has a row. The grouped side keeps only
SPs passing the served stages; within it, ``array_agg ... FILTER`` collects the
sorted distinct prices of the SPs that also pass the remaining (listable-only)
stages. Python then picks the in-range minimum per variant. Both stage sets are
picked by name from dispatch's one stage list, so neither can drift from it.

**Cache**: the whole response per point, in Redis under
``service_from_prices:{lat:.4f}:{lng:.4f}`` (~11 m) for 10 minutes. It is
computed at the request's own point, and nothing clears it, so a new price or
an SP going online shows within 10 minutes. A Redis error on read or write is
logged and the request is computed uncached, and so is an entry that no longer
validates (one written before ``variants`` existed).
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import Select, and_, distinct, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.provider_profile import ProviderProfile
from app.models.provider_service import ProviderService as ProviderServiceModel
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.models.sp_service_pricing import SpServicePricing
from app.models.user import User
from app.schemas.from_prices import ServiceFromPrice, VariantFromPrice
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


def serving_prices(stmt: Select, point) -> Select:
    """``stmt`` read from the active prices, on active variants of active
    services, of the SPs that serve ``point`` (subscribed + ``SERVED_STAGES``).

    The joins and filters behind both the tile (``from_prices_stmt``) and the
    "Notify me" checks (SVC-484, ``service_alert_service``), so "served" can't
    mean two things. ``point`` is EWKT or a geography column. ``ProviderProfile``
    and ``User`` stay un-aliased: the ``has_someone_to_do_it`` EXISTS clauses
    correlate on them by name.
    """
    return (
        stmt.select_from(SpServicePricing)
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
            *serves_area_conditions(SpServicePricing.service_id, point),
        )
    )


def from_prices_stmt(point_wkt: str):
    """Every active service, with one row per ACTIVE variant (SVC-496).

    A service with no active variant comes back once with NULL ``variant_id``.
    ``priced`` is true when some serving SP prices the variant; then
    ``listable_prices`` holds the sorted distinct prices of the SPs that also
    pass every listable-only stage (NULL when none does). An unpriced variant
    still has its row, so the client can grey it out instead of guessing from
    a missing entry. ``ProviderProfile`` and ``User`` stay un-aliased inside the
    grouped side: the ``has_someone_to_do_it`` EXISTS clauses correlate on them
    by name.
    """
    listable_now = and_(*stage_conditions(point_wkt, listable_only_stages()))
    priced = (
        serving_prices(
            select(
                SpServicePricing.service_variant_id.label("variant_id"),
                func.array_agg(
                    aggregate_order_by(
                        distinct(SpServicePricing.price_amount), SpServicePricing.price_amount
                    )
                )
                .filter(listable_now)
                .label("listable_prices"),
            ),
            point_wkt,
        )
        .group_by(SpServicePricing.service_variant_id)
        .subquery("priced")
    )
    return (
        select(
            Service.id.label("service_id"),
            ServiceVariant.id.label("variant_id"),
            priced.c.variant_id.is_not(None).label("priced"),
            ServiceVariant.base_price,
            Category.id.label("category_id"),
            Category.is_sensitive,
            Category.bounds_pct,
            priced.c.listable_prices,
        )
        .join(Category, Category.id == Service.category_id)
        .outerjoin(
            ServiceVariant,
            and_(ServiceVariant.service_id == Service.id, ServiceVariant.is_active.is_(True)),
        )
        .outerjoin(priced, priced.c.variant_id == ServiceVariant.id)
        .where(Service.is_active.is_(True))
        .order_by(Service.id, ServiceVariant.sort_order, ServiceVariant.id)
    )


@dataclass
class _Variant:
    """One active variant's listable prices under its own range."""

    variant_id: uuid.UUID
    lowest_in_range: Decimal | None = None
    lowest_any: Decimal | None = None

    @classmethod
    def judge(cls, variant_id: uuid.UUID, prices: list[Decimal], price_range) -> "_Variant":
        """``prices`` sorted ascending."""
        return cls(
            variant_id=variant_id,
            lowest_in_range=next((p for p in prices if not is_outside_range(p, price_range)), None),
            lowest_any=prices[0] if prices else None,
        )

    @property
    def from_price(self) -> Decimal | None:
        # The service's rule on one variant (14911): in range first, else the
        # lowest listable price, which is then out of range. NULL: nobody listable.
        return self.lowest_in_range if self.lowest_in_range is not None else self.lowest_any

    @property
    def outside_recommended_range(self) -> bool:
        """``from_price`` is the out-of-range fallback."""
        return self.lowest_in_range is None and self.lowest_any is not None


@dataclass
class _Tile:
    served: bool = False
    variants: list[_Variant] = field(default_factory=list)

    @property
    def from_price(self) -> Decimal | None:
        """The lowest in-range price on any variant; only with none in range
        anywhere, the lowest of all (14911). Not min(variant from_price): an
        out-of-range variant can be cheaper than another's in-range price, and
        the service still shows the in-range one."""
        inside = [v.lowest_in_range for v in self.variants if v.lowest_in_range is not None]
        if inside:
            return min(inside)
        return min((v.lowest_any for v in self.variants if v.lowest_any is not None), default=None)


def _money(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


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
                continue  # no active variant
            if row.priced:
                tile.served = True  # a serving SP prices an active variant
            prices = list(row.listable_prices or [])
            price_range = (
                recommended_range(
                    row.base_price, row.is_sensitive, row.bounds_pct, category_id=row.category_id
                )
                if prices
                else None  # nothing to judge
            )
            tile.variants.append(_Variant.judge(row.variant_id, prices, price_range))
        return [
            ServiceFromPrice(
                service_id=service_id,
                from_price=_money(tile.from_price),
                served=tile.served,
                variants=[
                    VariantFromPrice(
                        variant_id=v.variant_id,
                        from_price=_money(v.from_price),
                        outside_recommended_range=v.outside_recommended_range,
                    )
                    for v in tile.variants
                ],
            )
            for service_id, tile in tiles.items()
        ]
