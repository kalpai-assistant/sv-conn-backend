"""The SP's own pricing screen (SVC-412): ``GET /providers/me/pricing`` and
``PATCH /providers/me/pricing/{variant_id}``.

Money goes out as plain floats, never Decimal (a Decimal serialises as a JSON
string), as on ``RankedProvider``. Response fields have no defaults, so every
one stays ``required`` in the OpenAPI schema; "not set" is an explicit null.
"""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class RecommendedRange(BaseModel):
    """The sensitive category's range around the platform price, inclusive at
    both ends (``app/services/price_range.py``). Exact, not rounded: round the
    low bound up and the high one down for display."""

    low: float
    high: float


class TakeHome(BaseModel):
    """What the SP gets from one unit at its price (``commission.take_home``)."""

    commission: float
    # The GST on the platform's commission, at the category's commission_gst_pct.
    commission_gst: float
    sp_receives: float


class SpVariantPricing(BaseModel):
    variant_id: UUID
    label: str
    # The variant's base_price: the reference the range is built on, and what
    # "Use platform price" sets.
    platform_price: float
    # The service's unit; every SP prices in it.
    price_unit: str
    # NULL outside a sensitive category: no range, never a warning.
    recommended_range: RecommendedRange | None
    # Both NULL until the SP sets a price for this variant.
    price_amount: float | None
    is_active: bool | None
    # False without a price or without a range.
    outside_recommended_range: bool
    # NULL without a price.
    take_home: TakeHome | None
    # For the live "You receive" preview while the SP types: the variant's
    # commission percent, unrounded, and the GST percent on that commission.
    # The saved breakdown is always take_home, from the server.
    commission_pct: float
    gst_pct: float


class SpServicePricingGroup(BaseModel):
    service_id: UUID
    service_name: str
    price_unit: str
    # Active variants only, in sort_order.
    variants: list[SpVariantPricing]


class SpPricingResponse(BaseModel):
    # The services the SP subscribes to that are active, by name.
    services: list[SpServicePricingGroup]


class SpVariantPricingUpdateResponse(SpVariantPricing):
    service_id: UUID


class SpPriceUpdate(BaseModel):
    """Either field or both. A price change leaves on/off as it is, except on a
    variant priced for the first time, which starts on unless ``is_active``
    says otherwise. ``is_active`` alone needs a price already set
    (422 ``PRICE_REQUIRED``)."""

    price_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> "SpPriceUpdate":
        if self.price_amount is None and self.is_active is None:
            raise ValueError("Send 'price_amount', 'is_active' or both")
        return self
