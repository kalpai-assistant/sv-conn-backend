"""The platform's commission on a price the customer pays (SVC-411, epic SVC-406).

One rule, used wherever an amount is split between the platform and the SP:
the commission is the variant's own share of its catalogue price
(``platform_commission / base_price``) applied to the amount actually charged,
rounded to paise, and the SP gets the rest. At the variant's own price this
gives back its fixed amounts.

Used by ``create_booking`` for a chosen SP's price and by
``standard_variant.repriced_split`` when an admin reprices a variant.

``take_home`` (SVC-412) adds the GST on that commission, at the category's
``commission_gst_pct``: what the SP's pricing screen shows as "You receive".
Bookings don't store that GST yet; that is SVC-410, which reuses this helper.
"""

from decimal import Decimal

from app.config import get_settings
from app.models.service_variant import ServiceVariant
from app.utils.money import to_decimal

HUNDRED = Decimal(100)


def legacy_split(price: Decimal | float | int | str) -> tuple[Decimal, Decimal]:
    """``(platform_commission, provider_payout)`` at ``PLATFORM_COMMISSION_PERCENT``
    of ``price``, rounded to paise: ``create_booking``'s split for a service with
    no active variant, and the fallback for a variant priced 0."""
    rate = to_decimal(get_settings().PLATFORM_COMMISSION_PERCENT) / 100
    price = to_decimal(price)
    commission = to_decimal(price * rate)
    return commission, price - commission


def split_at_price(
    variant: ServiceVariant, amount: Decimal | float | int | str
) -> tuple[Decimal, Decimal]:
    """``(platform_commission, provider_payout)`` for ``amount`` charged on
    ``variant``, at the variant's own commission-to-price ratio. A variant
    priced 0 has no ratio, so it gets the legacy split."""
    amount = to_decimal(amount)
    base_price = to_decimal(variant.base_price)
    if base_price == 0:
        return legacy_split(amount)
    commission = to_decimal(amount * to_decimal(variant.platform_commission) / base_price)
    return commission, amount - commission


def commission_pct(variant: ServiceVariant) -> Decimal:
    """The commission percent ``split_at_price`` applies to ``variant``,
    unrounded: ``platform_commission / base_price × 100``, or
    ``PLATFORM_COMMISSION_PERCENT`` for a variant priced 0. For a client's live
    preview only; amounts always come from ``split_at_price``."""
    base_price = to_decimal(variant.base_price)
    if base_price == 0:
        return to_decimal(get_settings().PLATFORM_COMMISSION_PERCENT)
    return to_decimal(variant.platform_commission) * HUNDRED / base_price


def take_home(
    variant: ServiceVariant,
    gst_pct: Decimal | float | int | str,
    price: Decimal | float | int | str,
) -> tuple[Decimal, Decimal, Decimal]:
    """``(commission, commission_gst, sp_receives)`` for ``price`` charged on
    ``variant``. The commission is ``split_at_price``'s; the GST is ``gst_pct``
    percent of that rounded commission, rounded to paise; the SP receives the
    rest. ``gst_pct`` is the category's ``commission_gst_pct``, the GST on the
    commission, never the variant's ``gst_percent`` (the customer's GST)."""
    price = to_decimal(price)
    commission, _ = split_at_price(variant, price)
    gst = to_decimal(commission * to_decimal(gst_pct) / HUNDRED)
    return commission, gst, price - commission - gst
