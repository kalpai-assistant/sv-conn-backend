"""
The recommended price range of a sensitive category (SVC-482). The only copy of
the rule: the customer's ranking (SVC-409/482), the SP's pricing screen
(SVC-412) and the service tile's "from ₹X" (SVC-415) all import it.

- The reference is the variant's own platform price, ``service_variants.base_price``.
  ``categories.reference_price`` is not used.
- The range is that reference ± the category's ``bounds_pct`` percent, where the
  category is the one on ``services.category_id``.
- Only a sensitive category has a range. Without one, every price is in range.
- Bounds are inclusive and exact: no rounding, so a price sitting exactly on a
  bound can't be pushed to the wrong side. Round for display, never for the test.
- Computed per request, never stored: an admin's change to ``bounds_pct`` or to
  the variant's price moves SPs between groups on the next request.

Decimal only. A float would put 1874.25 on the wrong side of itself.
"""

import logging
import uuid
from decimal import Decimal

logger = logging.getLogger(__name__)

HUNDRED = Decimal(100)

PriceRange = tuple[Decimal, Decimal]


def recommended_range(
    base_price: Decimal,
    is_sensitive: bool,
    bounds_pct: Decimal | None,
    *,
    category_id: uuid.UUID | None = None,
) -> PriceRange | None:
    """``(low, high)`` = ``base_price × (1 ∓ bounds_pct / 100)``, or None when
    there is no range (every price is then in range):

    - the category isn't sensitive;
    - the reference is 0 or less: a range of [0, 0] would flag every SP, so it
      counts as no reference, as ``price_competitiveness`` already does;
    - a sensitive category without ``bounds_pct``, which
      ``ck_categories_sensitive_needs_bounds`` forbids. Logged, never raised, so
      a broken row can't take a page down. ``category_id`` only names the row
      in that log line.
    """
    if not is_sensitive or base_price <= 0:
        return None
    if bounds_pct is None:
        logger.warning(
            "price_range.no_bounds category_id=%s reason=sensitive_without_bounds", category_id
        )
        return None
    spread = base_price * bounds_pct / HUNDRED
    return base_price - spread, base_price + spread


def is_outside_range(price: Decimal, price_range: PriceRange | None) -> bool:
    """True when ``price`` is below ``low`` or above ``high``. Both bounds are
    in range. No range (None): never outside."""
    if price_range is None:
        return False
    low, high = price_range
    return price < low or price > high
