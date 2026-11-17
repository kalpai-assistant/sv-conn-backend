"""The SP's market insights per variant (SVC-413):
``GET /providers/me/market-insights?service_variant_id=``.

Aggregates only: no competitor ids or names, and no copy text, since the SP app
is translated and writes the sentence from ``my_bucket``. Money goes out as
plain floats, never Decimal (a Decimal serialises as a JSON string), and no
field has a default, so every one stays ``required`` in the OpenAPI schema; "not
set" is an explicit null, as in ``sp_pricing.py``.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

Bucket = Literal["lower", "middle", "upper"]


class AreaStats(BaseModel):
    # Every counted SP whose area overlaps the asking SP's, the asking SP
    # included only while its own price for the variant is on.
    sp_count: int
    # The middle price, or the average of the two middle ones, to paise.
    median_price: float
    min_price: float
    max_price: float
    # 0 = the cheapest, 100 = the most expensive, 50 = the median or all equal.
    # NULL while the SP's own price is off or not set.
    my_percentile: int | None
    # Third of the market: <= 33 lower, >= 67 upper, else middle. NULL with
    # my_percentile.
    my_bucket: Bucket | None


class MarketInsightsResponse(BaseModel):
    service_variant_id: UUID
    service_id: UUID
    service_name: str
    # The SP's own price, read fresh; NULL while it's off or not set.
    my_price: float | None
    # The variant's platform price (base_price).
    reference_price: float
    # Fewer than 3 SPs counted, or the SP has no service-area centre.
    insufficient_competition: bool
    # NULL exactly when insufficient_competition.
    area_stats: AreaStats | None
