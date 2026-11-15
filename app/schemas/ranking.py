from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class RankedProvider(BaseModel):
    """One SP on the customer's "pick your SP" sheet (SVC-409).

    Plain floats, never Decimal: a Decimal serialises as a JSON string. No field
    has a default, so every one stays ``required`` in the OpenAPI schema.
    """

    sp_id: UUID
    # The business name, else the SP's own name. NULL only for an SP with neither.
    name: str | None
    price_amount: float
    price_unit: str
    # NULL when the SP has no consumer review yet.
    rating_avg: float | None
    rating_count: int
    # From the SP's service-area centre, which its radius is measured from.
    distance_km: float
    reliability_score: float
    score: float
    why: list[str]
    is_boosted: bool


class ServiceProvidersResponse(BaseModel):
    service_id: UUID
    quoted_at: datetime
    ttl_seconds: int
    providers: list[RankedProvider]
