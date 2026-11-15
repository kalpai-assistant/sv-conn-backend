"""
The customer's "pick your SP" sheet: ``GET /services/{id}/providers`` (SVC-409).

Who is listed: an SP that dispatch would offer this service to at the
customer's point (``eligible_providers_multi_stmt``, composed, not restated)
and that has an active ``sp_service_pricing`` row for it. Booking with any
listed ``sp_id`` therefore won't 409 ``SP_UNAVAILABLE`` (SVC-479), barring a
race between the sheet and the booking.

How they are ordered: the ticket's weighted score, with weights from
``pricing_config.ranking_weights`` cached in-process for 60 s.

Two statements per request whatever the SP count (the service, then one
statement for candidates plus every per-SP signal), and a third on a config
cache miss.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException, ServiceNotFound
from app.models.booking import Booking, BookingStatus
from app.models.booking_assignment_event import BookingAssignmentEvent
from app.models.booking_status_log import BookingStatusLog
from app.models.pricing_config import PricingConfig
from app.models.professional import Professional
from app.models.provider_profile import ProviderProfile
from app.models.review import Review
from app.models.service import Service
from app.models.sp_service_pricing import SpServicePricing
from app.models.user import User
from app.schemas.ranking import RankedProvider, ServiceProvidersResponse
from app.services.admin_pricing_service import _validate_config
from app.services.provider_matching import eligible_providers_multi_stmt
from app.utils.geo import build_point_wkt, distance_km_to_point

logger = logging.getLogger(__name__)

# The SVC-428 migration's seed values, used for any key whose row is missing or
# fails the admin write validation. The endpoint never 500s on config.
DEFAULT_CONFIG: dict[str, Any] = {
    "ranking_weights": {
        "rating": 0.35,
        "price_vs_ref": 0.25,
        "distance": 0.20,
        "reliability": 0.15,
        "activity": 0.05,
    },
    "sheet_ttl_seconds": 600,
    "boost_window_days": 14,
    "boost_window_jobs": 10,
}
CONFIG_CACHE_SECONDS = 60

RELIABILITY_WINDOW = timedelta(days=30)
# Cold-start floors: a new SP must not rank last forever for having no history.
RATING_FLOOR_BELOW_COUNT = 5
RATING_FLOOR = 0.6
RELIABILITY_FLOOR_BELOW_ASSIGNED = 3
RELIABILITY_FLOOR = 0.8
BOOST_MULTIPLIER = 1.5
PRICE_COMPETITIVENESS_MAX = 1.5
DISTANCE_DECAY_KM = 5.0
RECENCY_DECAY_DAYS = 14.0
BELOW_MARKET_RATIO = 0.9
MAX_WHY_CHIPS = 3


@dataclass(frozen=True)
class RankingConfig:
    weights: dict[str, float]
    ttl_seconds: int
    boost_window_days: int
    boost_window_jobs: int


_config_cache: tuple[float, RankingConfig] | None = None


def reset_ranking_config_cache() -> None:
    """Forget the cached config, so the next request reads the table (tests)."""
    global _config_cache
    _config_cache = None


def _config_value(key: str, rows: dict[str, Any]) -> Any:
    """``rows[key]`` if it passes the same check an admin write does, else the
    seed value, logged. Reusing ``_validate_config`` keeps one definition of a
    valid value."""
    if key not in rows:
        logger.warning("ranking.config_fallback key=%s reason=missing", key)
        return DEFAULT_CONFIG[key]
    try:
        return _validate_config(key, rows[key])
    except AppException:
        logger.warning("ranking.config_fallback key=%s reason=invalid", key)
        return DEFAULT_CONFIG[key]


async def load_ranking_config(db: AsyncSession) -> RankingConfig:
    """The ranking knobs, read at most once per ``CONFIG_CACHE_SECONDS`` per
    process: an admin's edit shows within a minute, and the table isn't hit on
    every sheet."""
    global _config_cache
    now = monotonic()
    if _config_cache is not None and now - _config_cache[0] < CONFIG_CACHE_SECONDS:
        return _config_cache[1]
    rows = dict(
        (
            await db.execute(
                select(PricingConfig.key, PricingConfig.value_json).where(
                    PricingConfig.key.in_(DEFAULT_CONFIG)
                )
            )
        ).all()
    )
    config = RankingConfig(
        weights=_config_value("ranking_weights", rows),
        ttl_seconds=_config_value("sheet_ttl_seconds", rows),
        boost_window_days=_config_value("boost_window_days", rows),
        boost_window_jobs=_config_value("boost_window_jobs", rows),
    )
    _config_cache = (now, config)
    return config


# --- the formula -------------------------------------------------------------


def price_competitiveness(price: float, reference: float) -> float:
    """``clamp(1 - (price - ref) / ref, 0, 1.5)``. A reference of 0 can't be
    compared against, so it scores neutral."""
    if reference <= 0:
        return 1.0
    return min(max(1 - (price - reference) / reference, 0.0), PRICE_COMPETITIVENESS_MAX)


def distance_decay(km: float) -> float:
    return math.exp(-km / DISTANCE_DECAY_KM)


def recency_decay(last_completed_at: datetime | None, now: datetime) -> float:
    """``exp(-days_since / 14)``; 0 for an SP that has never completed a job
    (the boost, not this, is the cold-start lift)."""
    if last_completed_at is None:
        return 0.0
    days = max((now - last_completed_at).total_seconds(), 0.0) / 86400
    return math.exp(-days / RECENCY_DECAY_DAYS)


def rating_component(rating_avg: float | None, rating_count: int) -> float:
    if rating_count < RATING_FLOOR_BELOW_COUNT or rating_avg is None:
        return RATING_FLOOR
    return rating_avg / 5.0


def reliability_score(completed_30d: int, assigned_30d: int) -> float:
    """Completed over assigned in the last 30 days, capped at 1: a job claimed
    on day 31 and completed on day 29 counts on top but not underneath."""
    if assigned_30d < RELIABILITY_FLOOR_BELOW_ASSIGNED:
        return RELIABILITY_FLOOR
    return min(completed_30d / max(assigned_30d, 1), 1.0)


def is_boosted(
    boost_eligible_at: datetime | None,
    boost_completed_jobs: int,
    config: RankingConfig,
    now: datetime,
) -> bool:
    """Inside the new-SP window: started, fewer than ``boost_window_days`` ago,
    and fewer than ``boost_window_jobs`` jobs done. Whichever runs out first
    ends it."""
    if boost_eligible_at is None:
        return False
    window_end = boost_eligible_at + timedelta(days=config.boost_window_days)
    return boost_eligible_at <= now < window_end and boost_completed_jobs < config.boost_window_jobs


def why_chips(
    *,
    rating_avg: float | None,
    rating_count: int,
    distance_km: float,
    boosted: bool,
    reliability: float,
    price: float,
    reference: float,
    activity: float,
) -> list[str]:
    """The first three rules that fit, in the ticket's order."""
    chips = []
    if rating_avg is not None and rating_avg >= 4.5 and rating_count >= 20:
        chips.append("Highly rated")
    if distance_km < 2:
        chips.append(f"{distance_km:.1f} km away")
    if boosted:
        chips.append("New on platform")
    if reliability >= 0.95:
        chips.append("Reliable")
    if reference > 0 and price <= reference * BELOW_MARKET_RATIO:
        chips.append("Below market")
    if activity > 0.9:
        chips.append("Active today")
    return chips[:MAX_WHY_CHIPS]


# --- the query ---------------------------------------------------------------


def ranking_rows_stmt(service_id: uuid.UUID, point_wkt: str, now: datetime):
    """One statement: every listable SP with its price and every signal.

    ``candidates`` is dispatch's own eligibility (no booking yet, so no
    ``booking_id``) joined to the SP's active price for this service. Each
    signal is a grouped aggregate narrowed to the candidates, so the work is
    one pass per table whatever the SP count. Every SP key is
    ``provider_profiles.user_id``.
    """
    eligible = eligible_providers_multi_stmt([service_id], point_wkt).subquery("eligible")
    candidates = (
        select(
            SpServicePricing.sp_id,
            SpServicePricing.price_amount,
            SpServicePricing.price_unit,
        )
        .join(eligible, eligible.c.id == SpServicePricing.sp_id)
        .where(
            SpServicePricing.service_id == service_id,
            SpServicePricing.is_active.is_(True),
        )
        .cte("candidates")
    )
    candidate_ids = select(candidates.c.sp_id)
    since = now - RELIABILITY_WINDOW

    # The SP's rating: consumer reviews of bookings it took, any service.
    ratings = (
        select(
            Booking.provider_id.label("sp_id"),
            func.avg(Review.rating).label("avg"),
            func.count(Review.id).label("n"),
        )
        .join(Booking, Booking.id == Review.booking_id)
        .where(Booking.provider_id.in_(candidate_ids), Review.reviewer_id == Booking.consumer_id)
        .group_by(Booking.provider_id)
        .subquery("ratings")
    )
    # Jobs the SP claimed (accept or /assign, SVC-333), counted from the event
    # rather than bookings.provider_id, so a claim it later gave back still
    # counts against it.
    assigned = (
        select(
            Professional.service_provider_id.label("sp_id"),
            func.count(func.distinct(BookingAssignmentEvent.booking_id)).label("n"),
        )
        .join(Professional, Professional.id == BookingAssignmentEvent.to_professional_id)
        .where(
            BookingAssignmentEvent.kind == "claimed",
            BookingAssignmentEvent.created_at >= since,
            Professional.service_provider_id.in_(candidate_ids),
        )
        .group_by(Professional.service_provider_id)
        .subquery("assigned")
    )
    # A completed booking keeps provider_id = the SP that did it. bookings has
    # no completed_at, so the time is the status log row's.
    completed = (
        select(
            Booking.provider_id.label("sp_id"),
            func.count(func.distinct(BookingStatusLog.booking_id))
            .filter(BookingStatusLog.created_at >= since)
            .label("n_30d"),
            func.max(BookingStatusLog.created_at).label("last_at"),
        )
        .join(Booking, Booking.id == BookingStatusLog.booking_id)
        .where(
            BookingStatusLog.to_status == BookingStatus.COMPLETED.value,
            Booking.provider_id.in_(candidate_ids),
        )
        .group_by(Booking.provider_id)
        .subquery("completed")
    )

    return (
        select(
            candidates.c.sp_id,
            candidates.c.price_amount,
            candidates.c.price_unit,
            ProviderProfile.business_name,
            User.name,
            ProviderProfile.boost_eligible_at,
            ProviderProfile.boost_completed_jobs_count,
            distance_km_to_point(ProviderProfile.service_area_center, point_wkt).label(
                "distance_km"
            ),
            ratings.c.avg.label("rating_avg"),
            ratings.c.n.label("rating_count"),
            assigned.c.n.label("assigned_30d"),
            completed.c.n_30d.label("completed_30d"),
            completed.c.last_at.label("last_completed_at"),
        )
        .select_from(candidates)
        .join(ProviderProfile, ProviderProfile.user_id == candidates.c.sp_id)
        .join(User, User.id == candidates.c.sp_id)
        .outerjoin(ratings, ratings.c.sp_id == candidates.c.sp_id)
        .outerjoin(assigned, assigned.c.sp_id == candidates.c.sp_id)
        .outerjoin(completed, completed.c.sp_id == candidates.c.sp_id)
    )


class RankingService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def rank_providers(
        self, service_id: uuid.UUID, lat: float, lng: float, limit: int
    ) -> ServiceProvidersResponse:
        reference_raw = await self.db.scalar(
            select(Service.base_price).where(Service.id == service_id, Service.is_active.is_(True))
        )
        if reference_raw is None:
            raise ServiceNotFound(service_id)
        reference = float(reference_raw)

        config = await load_ranking_config(self.db)
        weights = config.weights
        now = datetime.now(UTC)
        rows = await self.db.execute(ranking_rows_stmt(service_id, build_point_wkt(lat, lng), now))

        scored: list[tuple[float, uuid.UUID, RankedProvider]] = []
        for row in rows:
            price = float(row.price_amount)
            distance = float(row.distance_km)
            # Rounded once, so the chip says what the row shows.
            shown_km = round(distance, 1)
            rating_avg = float(row.rating_avg) if row.rating_avg is not None else None
            rating_count = row.rating_count or 0
            reliability = reliability_score(row.completed_30d or 0, row.assigned_30d or 0)
            activity = recency_decay(row.last_completed_at, now)
            boosted = is_boosted(row.boost_eligible_at, row.boost_completed_jobs_count, config, now)
            score = (
                weights["rating"] * rating_component(rating_avg, rating_count)
                + weights["price_vs_ref"] * price_competitiveness(price, reference)
                + weights["distance"] * distance_decay(distance)
                + weights["reliability"] * reliability
                + weights["activity"] * activity
            )
            if boosted:
                score *= BOOST_MULTIPLIER
            provider = RankedProvider(
                sp_id=row.sp_id,
                name=row.business_name or row.name,
                price_amount=price,
                price_unit=row.price_unit,
                rating_avg=round(rating_avg, 1) if rating_avg is not None else None,
                rating_count=rating_count,
                distance_km=shown_km,
                reliability_score=round(reliability, 2),
                score=round(score, 4),
                why=why_chips(
                    rating_avg=rating_avg,
                    rating_count=rating_count,
                    distance_km=shown_km,
                    boosted=boosted,
                    reliability=reliability,
                    price=price,
                    reference=reference,
                    activity=activity,
                ),
                is_boosted=boosted,
            )
            scored.append((score, row.sp_id, provider))

        # Unrounded score, then sp_id, so equal scores come back in a stable order.
        scored.sort(key=lambda item: (-item[0], str(item[1])))
        return ServiceProvidersResponse(
            service_id=service_id,
            quoted_at=now,
            ttl_seconds=config.ttl_seconds,
            providers=[provider for _, _, provider in scored[:limit]],
        )
