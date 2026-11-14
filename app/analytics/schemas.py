"""Response shapes for ``/admin/analytics/*`` (SVC-432).

Money is rupees as JSON numbers; ratios are 0..1 floats; ``week_start`` is the
Monday (IST) of the week; ``null`` means "not enough history", never zero.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel

# --- Overview ---------------------------------------------------------------


class GmvWindow(BaseModel):
    days: int
    gmv: float
    completed: int
    # null when the platform's bookings don't cover the whole previous window.
    previous_gmv: float | None
    # (gmv - previous_gmv) / previous_gmv; null when previous is null or 0.
    change_pct: float | None


class OverviewWeek(BaseModel):
    week_start: date
    gmv: float
    completed: int
    commission: float
    take_rate: float | None
    aov: float | None


class BookingsDay(BaseModel):
    day: date
    requested: int
    accepted: int
    en_route: int
    in_progress: int
    completed: int
    cancelled: int
    expired: int
    # Not a status: payment refunded, or a non-FAILED dispute refund.
    refunded: int


class RetentionCell(BaseModel):
    # Members whose N-day window has fully elapsed (the denominator).
    matured: int
    rebooked: int
    rate: float | None


class ConsumerCohort(BaseModel):
    signup_week: date
    size: int
    d7: RetentionCell
    d30: RetentionCell
    d90: RetentionCell


class OverviewResponse(BaseModel):
    as_of: datetime
    gmv: list[GmvWindow]
    weekly: list[OverviewWeek]
    daily: list[BookingsDay]
    cohorts: list[ConsumerCohort]


# --- Supply -----------------------------------------------------------------


class ActiveWeek(BaseModel):
    week_start: date
    active_sps: int
    active_pros: int


class ActiveSupply(BaseModel):
    sps_7d: int
    sps_30d: int
    pros_7d: int
    pros_30d: int
    weekly: list[ActiveWeek]


class SpCohort(BaseModel):
    kyc_week: date
    size: int
    # Share with a booking in the 30 days ending at day 30 / 60 / 90; null until
    # every member's window has elapsed.
    d30: float | None
    d60: float | None
    d90: float | None


class ChurnMonth(BaseModel):
    signup_month: str  # "YYYY-MM", IST
    verified: int
    churned: int
    rate: float


class GapService(BaseModel):
    id: UUID
    name: str


class GapCell(BaseModel):
    pincode: str
    service_id: UUID
    demand: int
    unmet: int


class SupplyGap(BaseModel):
    pincodes: list[str]  # by demand, highest first; "Unknown" always kept
    services: list[GapService]
    cells: list[GapCell]
    truncated: bool
    total_pincodes: int


class CategoryPrices(BaseModel):
    category_id: UUID
    name: str
    count: int
    min: float
    p25: float
    median: float
    p75: float
    max: float


class AcceptWeek(BaseModel):
    week_start: date
    accepted: int
    p50_seconds: float | None
    p90_seconds: float | None


class SupplyResponse(BaseModel):
    as_of: datetime
    active: ActiveSupply
    sp_cohorts: list[SpCohort]
    churn: list[ChurnMonth]
    supply_gap: SupplyGap
    prices: list[CategoryPrices]
    time_to_accept: list[AcceptWeek]
