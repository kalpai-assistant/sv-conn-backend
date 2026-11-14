"""SVC-428: the admin pricing config surface (/admin/pricing/*).

Money and percentages go out as JSON numbers, never Decimal strings. Requests
are partial: the console saves one cell at a time.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool


class CategoryPricingRow(BaseModel):
    """One category with its pricing config. L1 rows have no parent."""

    id: uuid.UUID
    name: str
    slug: str
    parent_id: uuid.UUID | None
    parent_name: str | None
    sort_order: int
    is_active: bool
    is_sensitive: bool
    reference_price: float | None
    bounds_pct: float | None
    platform_commission_pct: float
    platform_commission_min: float
    commission_gst_pct: float


class CategoryPricingUpdate(BaseModel):
    """Any subset of the six fields. ``null`` clears ``reference_price`` or
    ``bounds_pct`` and is refused on the others. Turning ``is_sensitive`` on
    needs ``bounds_pct`` in the same body unless the row already has bounds."""

    model_config = ConfigDict(extra="forbid")

    is_sensitive: StrictBool | None = None
    reference_price: Decimal | None = None
    bounds_pct: Decimal | None = None
    platform_commission_pct: Decimal | None = None
    platform_commission_min: Decimal | None = None
    commission_gst_pct: Decimal | None = None


class PricingConfigRow(BaseModel):
    key: str
    value: Any
    updated_at: datetime
    # NULL on the migration's seed rows.
    updated_by: uuid.UUID | None


class PricingConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
