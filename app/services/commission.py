"""The platform's commission on a price the customer pays (SVC-411, epic SVC-406).

One rule, used wherever an amount is split between the platform and the SP:
the commission is the variant's own share of its catalogue price
(``platform_commission / base_price``) applied to the amount actually charged,
rounded to paise, and the SP gets the rest. At the variant's own price this
gives back its fixed amounts.

Used by ``create_booking`` for a chosen SP's price and by
``standard_variant.repriced_split`` when an admin reprices a variant.

The platform charges 18% GST on its commission (SVC-410), at the category's
``commission_gst_pct``, and it comes out of the SP's share: ``commission_gst``
is that one rule. ``take_home`` (SVC-412) uses it for what the SP's pricing
screen shows as "You receive", and ``create_booking`` uses it on every booking,
with or without a chosen SP, storing the result in ``commission_gst`` on the
booking and its items. Payouts, refunds and statements read the stored amounts
and never recompute them.
"""

from decimal import ROUND_HALF_UP, Decimal

from app.config import get_settings
from app.models.service_variant import ServiceVariant
from app.utils.money import to_decimal

HUNDRED = Decimal(100)
PCT_QUANTUM = Decimal("0.01")


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


def commission_gst(
    commission: Decimal | float | int | str, gst_pct: Decimal | float | int | str
) -> Decimal:
    """The GST on ``commission`` at ``gst_pct`` percent, rounded half up to paise.

    ``gst_pct`` is the category's ``commission_gst_pct``, never the variant's
    ``gst_percent`` (that one is the customer's GST on the service). The only
    place this rule lives: ``take_home`` and ``create_booking`` both call it.
    """
    return to_decimal(to_decimal(commission) * to_decimal(gst_pct) / HUNDRED)


def commission_pct(variant: ServiceVariant) -> Decimal:
    """The commission percent ``split_at_price`` applies to ``variant``,
    unrounded: ``platform_commission / base_price × 100``, or
    ``PLATFORM_COMMISSION_PERCENT`` for a variant priced 0. For a client's live
    preview only; amounts always come from ``split_at_price``."""
    base_price = to_decimal(variant.base_price)
    if base_price == 0:
        return to_decimal(get_settings().PLATFORM_COMMISSION_PERCENT)
    return to_decimal(variant.platform_commission) * HUNDRED / base_price


def rounded_commission_pct(variant: ServiceVariant) -> Decimal:
    """``commission_pct(variant)`` rounded half up to 2 dp: the "current %" the
    admin variant editor shows (SVC-481). Most catalogue variants have whole-rupee
    amounts, so their exact percent isn't a 2 dp number (₹240 on ₹799 is
    30.0375%, shown as 30.04). An edit that sends this value back is treated as
    no change of percent, so the stored ratio is kept."""
    return commission_pct(variant).quantize(PCT_QUANTUM, rounding=ROUND_HALF_UP)


def split_at_pct(
    price: Decimal | float | int | str, pct: Decimal | float | int | str
) -> tuple[Decimal, Decimal]:
    """``(platform_commission, provider_payout)`` for ``price`` at ``pct`` percent
    (SVC-481): the commission is ``price × pct / 100`` rounded half up to paise and
    the payout is the rest, so the two always add up to the price. Used only when an
    admin sets a variant's commission percent; every other split keeps the
    variant's stored ratio (``split_at_price``)."""
    price = to_decimal(price)
    commission = to_decimal(price * Decimal(pct) / HUNDRED)
    return commission, price - commission


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
    gst = commission_gst(commission, gst_pct)
    return commission, gst, price - commission - gst
