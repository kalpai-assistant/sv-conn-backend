"""The SP's market insights per variant (SVC-413, epic SVC-406).

Where the SP's price for one variant sits among the other SPs whose service
areas overlap its own: count, median, min, max, its percentile and third.
Aggregates only, so no competitor can be named (the ticket's anti-collusion
rule), and nothing at all below 3 SPs, where "the other one" would be named.

**Who counts** (14926 #1): an SP that is KYC VERIFIED, with an active account,
a service-area centre and an ACTIVE price for this variant. Online or not:
a price is the market price either way. Areas overlap (14925 #2) when the two
centres are within the two radii added together.

**The cache** (14933, which replaces the earlier "clear on save" design): only
the competitors' prices, the expensive geo query, are cached, in Redis under
``market_insights:{sp_id}:{service_variant_id}`` for an hour, as a JSON list of
prices with no SP ids. The SP's own price and on/off, the variant's platform
price and the SP's own area are read fresh on every request. So the SP's own
changes show at once and **nothing clears the key, anywhere**; the competitors
(and the competitor set for a moved area) can be up to an hour old. A Redis
error on read or write is logged and the request is computed uncached.
"""

import json
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_profile import KYCStatus, ProviderProfile
from app.models.service import Service
from app.models.sp_service_pricing import SpServicePricing
from app.models.user import User
from app.schemas.market_insights import AreaStats, Bucket, MarketInsightsResponse
from app.services.sp_pricing_service import SpPricingService
from app.utils.geo import st_dwithin_areas_overlap
from app.utils.money import to_decimal

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600
# Below this, stats would be nonsense and would name the other SP.
MIN_SP_COUNT = 3
LOWER_MAX, UPPER_MIN = 33, 67


def cache_key(sp_id: uuid.UUID, service_variant_id: uuid.UUID) -> str:
    return f"market_insights:{sp_id}:{service_variant_id}"


def bucket(percentile: int) -> Bucket:
    if percentile <= LOWER_MAX:
        return "lower"
    if percentile >= UPPER_MIN:
        return "upper"
    return "middle"


def median(prices: list[Decimal]) -> Decimal:
    """The middle of ``prices`` (sorted), or the average of the two middle
    ones, rounded to paise half up."""
    mid = len(prices) // 2
    if len(prices) % 2:
        return to_decimal(prices[mid])
    return to_decimal((prices[mid - 1] + prices[mid]) / 2)


def percentile(competitors: list[Decimal], my_price: Decimal) -> int:
    """(competitors below me + ½ × competitors at my price) ÷ their count × 100,
    rounded half up (not Python's banker's ``round``). Their count is
    ``sp_count - 1``. Cheapest 0, dearest 100, all equal 50."""
    below = sum(1 for p in competitors if p < my_price)
    same = sum(1 for p in competitors if p == my_price)
    value = (Decimal(below) + Decimal(same) / 2) / len(competitors) * 100
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def area_stats(competitors: list[Decimal], my_price: Decimal | None) -> AreaStats | None:
    """The stats over the competitors plus the SP's own price when it's on, or
    None below ``MIN_SP_COUNT``."""
    counted = sorted([*competitors, *([] if my_price is None else [my_price])])
    if len(counted) < MIN_SP_COUNT:
        return None
    mine = None if my_price is None else percentile(competitors, my_price)
    return AreaStats(
        sp_count=len(counted),
        median_price=float(median(counted)),
        min_price=float(counted[0]),
        max_price=float(counted[-1]),
        my_percentile=mine,
        my_bucket=None if mine is None else bucket(mine),
    )


class MarketInsightsService:
    def __init__(self, db: AsyncSession, redis: Redis):
        self.db = db
        self.redis = redis

    async def get_insights(
        self, sp_id: uuid.UUID, service_variant_id: uuid.UUID
    ) -> MarketInsightsResponse:
        """404 ``VARIANT_NOT_FOUND`` and ``SUBSCRIPTION_NOT_FOUND`` as
        ``set_price`` raises them, from the same checks, without its lock."""
        pricing = SpPricingService(self.db)
        variant = (await pricing.active_variant(service_variant_id)).ServiceVariant
        service_id = variant.service_id
        await pricing.require_subscription(sp_id, service_id, lock=False)

        service_name = await self.db.scalar(select(Service.name).where(Service.id == service_id))
        area = (
            await self.db.execute(
                select(
                    ProviderProfile.service_area_center, ProviderProfile.service_area_radius_km
                ).where(ProviderProfile.user_id == sp_id)
            )
        ).one_or_none()
        own = (
            await self.db.execute(
                select(SpServicePricing.price_amount, SpServicePricing.is_active).where(
                    SpServicePricing.sp_id == sp_id,
                    SpServicePricing.service_variant_id == service_variant_id,
                )
            )
        ).one_or_none()
        my_price = own.price_amount if own is not None and own.is_active else None

        stats = None
        if area is not None and area.service_area_center is not None:
            competitors = await self._competitors(
                sp_id, service_variant_id, area.service_area_center, area.service_area_radius_km
            )
            stats = area_stats(competitors, my_price)

        return MarketInsightsResponse(
            service_variant_id=service_variant_id,
            service_id=service_id,
            service_name=service_name,
            my_price=None if my_price is None else float(my_price),
            reference_price=float(variant.base_price),
            insufficient_competition=stats is None,
            area_stats=stats,
        )

    async def _competitors(
        self, sp_id: uuid.UUID, service_variant_id: uuid.UUID, center, radius_km: int
    ) -> list[Decimal]:
        """The other counted SPs' prices, from the cache or the DB."""
        key = cache_key(sp_id, service_variant_id)
        try:
            cached = await self.redis.get(key)
            if cached is not None:
                return [Decimal(p) for p in json.loads(cached)]
        except Exception as exc:  # never fail the request on the cache
            logger.warning("market_insights.cache_read_failed key=%s error=%r", key, exc)

        prices = await self._query_competitors(sp_id, service_variant_id, center, radius_km)
        try:
            await self.redis.set(key, json.dumps([str(p) for p in prices]), ex=CACHE_TTL_SECONDS)
        except Exception as exc:  # never fail the request on the cache
            logger.warning("market_insights.cache_write_failed key=%s error=%r", key, exc)
        return prices

    async def _query_competitors(
        self, sp_id: uuid.UUID, service_variant_id: uuid.UUID, center, radius_km: int
    ) -> list[Decimal]:
        """Every counted SP but ``sp_id`` whose area overlaps ``sp_id``'s."""
        return list(
            (
                await self.db.scalars(
                    select(SpServicePricing.price_amount)
                    .join(User, User.id == SpServicePricing.sp_id)
                    .join(ProviderProfile, ProviderProfile.user_id == SpServicePricing.sp_id)
                    .where(
                        SpServicePricing.service_variant_id == service_variant_id,
                        SpServicePricing.is_active.is_(True),
                        SpServicePricing.sp_id != sp_id,
                        User.is_active.is_(True),
                        ProviderProfile.kyc_status == KYCStatus.VERIFIED,
                        ProviderProfile.service_area_center.is_not(None),
                        st_dwithin_areas_overlap(
                            ProviderProfile.service_area_center,
                            ProviderProfile.service_area_radius_km,
                            center,
                            radius_km,
                        ),
                    )
                    .order_by(SpServicePricing.price_amount)
                )
            ).all()
        )
