"""SVC-430: how a service is priced, in the format Vipul fixed on SVC-436.

Stored as ``services.price_unit`` varchar(16) with a CHECK on these four values,
not a Postgres ENUM, so adding a unit is a one-line CHECK change. This module
imports nothing from the app, so the marketplace stories (SVC-408, SVC-416,
SVC-441) can copy it as-is.

Nothing prices by unit yet: a booking still uses the variant price, or
``services.base_price`` when the service has no variants.
"""

import enum


class PriceUnit(str, enum.Enum):
    PER_VISIT = "PER_VISIT"
    PER_SQFT = "PER_SQFT"
    PER_HOUR = "PER_HOUR"
    PER_ITEM = "PER_ITEM"


# These units need ``services.min_qty`` (>= 1); the others must leave it NULL.
UNITS_WITH_MIN_QTY: frozenset[PriceUnit] = frozenset({PriceUnit.PER_SQFT, PriceUnit.PER_HOUR})


def _sql_list(units) -> str:
    return ", ".join(f"'{u.value}'" for u in sorted(units, key=lambda u: u.value))


# The CHECK bodies, shared by the model and the migration's test.
PRICE_UNIT_CHECK = f"price_unit IN ({_sql_list(PriceUnit)})"
MIN_QTY_CHECK = (
    f"(price_unit IN ({_sql_list(UNITS_WITH_MIN_QTY)}) AND min_qty IS NOT NULL AND min_qty >= 1)"
    f" OR (price_unit NOT IN ({_sql_list(UNITS_WITH_MIN_QTY)}) AND min_qty IS NULL)"
)
