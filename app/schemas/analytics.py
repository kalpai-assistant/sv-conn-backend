"""``POST /analytics/events``: one marketplace event from the customer app (SVC-419).

One envelope model per event, told apart by ``name``. Only the five client names
parse; the server names (``sp.price_set``, ``booking.marketplace_created``) and
any other name are a 422. Extra fields are refused at both levels, so nothing
the app didn't declare here (a phone, an email, lat/lng, a ``user_id``) can be
stored. Every property is required; the ones that may be null say so.

Money arrives as a JSON number. A JS float such as 2199.9999999998 is accepted
and rounded half up to 2 decimals, never refused, so a real event isn't lost.
"""

import math
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
)

from app.utils.money import to_decimal

# The largest Numeric(12, 2) amount.
MONEY_MAX = Decimal("9999999999.99")
DAY_MS = 86_400_000

Money = Annotated[
    Decimal,
    Field(ge=0, le=MONEY_MAX, allow_inf_nan=False),
    AfterValidator(to_decimal),
]
# ASCII digits only: \d would also take other scripts' digits.
Pincode = Annotated[str, Field(pattern=r"^[0-9]{6}$")] | None
Rank = Annotated[int, Field(ge=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SheetOpened(_Strict):
    service_id: UUID
    service_variant_id: UUID
    sp_count_in_area: Annotated[int, Field(ge=0)]
    has_boosted: bool
    user_pincode: Pincode


class SheetAbandoned(_Strict):
    service_id: UUID
    service_variant_id: UUID
    time_spent_ms: Annotated[int, Field(ge=0, le=DAY_MS)]
    scroll_depth_pct: Annotated[int, Field(ge=0, le=100)]


class SpSelected(_Strict):
    service_id: UUID
    service_variant_id: UUID
    sp_id: UUID
    sp_rank_in_sheet: Rank
    sp_score: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    price_amount: Money
    is_boosted_sp: bool
    is_outside_range: bool


class PriceChangeAtCheckout(_Strict):
    service_id: UUID
    service_variant_id: UUID
    sp_id: UUID
    old_price: Money
    new_price: Money
    user_action: Literal["confirmed", "abandoned"]


class TileComingSoonTapped(_Strict):
    # No variant: the tile is per service.
    service_id: UUID
    state: Literal["coming_soon", "no_pros_online"]
    user_pincode: Pincode


class _Envelope(_Strict):
    # The app's own UUID for the event; a retry with the same one is ignored.
    event_id: UUID
    # When it happened on the phone. Stored, but reports use created_at.
    occurred_at: AwareDatetime | None = None


class SheetOpenedEvent(_Envelope):
    name: Literal["marketplace.sheet_opened"]
    properties: SheetOpened


class SheetAbandonedEvent(_Envelope):
    name: Literal["marketplace.sheet_abandoned"]
    properties: SheetAbandoned


class SpSelectedEvent(_Envelope):
    name: Literal["marketplace.sp_selected"]
    properties: SpSelected


class PriceChangeAtCheckoutEvent(_Envelope):
    name: Literal["marketplace.price_change_at_checkout"]
    properties: PriceChangeAtCheckout


class TileComingSoonTappedEvent(_Envelope):
    name: Literal["marketplace.tile_coming_soon_tapped"]
    properties: TileComingSoonTapped


def _non_finite_as_text(value: Any) -> Any:
    """NaN / Infinity as the strings "nan" / "inf", anywhere in the body.

    Python's JSON parser accepts them, and FastAPI's 422 echoes the bad input,
    which JSON can't encode: the request would be a 500. As text, every field
    still refuses them (no field takes a string a number can't parse from, and
    none allows inf/nan), and the 422 can say so."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _non_finite_as_text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_non_finite_as_text(v) for v in value]
    return value


ClientEvent = Annotated[
    SheetOpenedEvent
    | SheetAbandonedEvent
    | SpSelectedEvent
    | PriceChangeAtCheckoutEvent
    | TileComingSoonTappedEvent,
    Field(discriminator="name"),
    BeforeValidator(_non_finite_as_text),
]


class ClientEventBody(RootModel[ClientEvent]):
    """The request body. A RootModel, because FastAPI rebuilds a bare annotated
    union from ``Body(...)`` and drops its discriminator and validator."""


# These properties go in their own columns, not the payload.
TYPED_ID_FIELDS = ("service_id", "service_variant_id", "sp_id")
