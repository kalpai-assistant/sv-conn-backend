"""Money handling: exact rupee arithmetic and the rupees→paise boundary.

Money is stored as ``Numeric(12, 2)`` and handled as ``Decimal`` everywhere inside
the app. Floats are not safe for money: 80.07 has no exact binary form (it is really
80.0699999…), so ``int(80.07 * 100)`` truncates to 8006 and the provider is paid
₹80.06. Roughly 1 in 22 realistic prices hits that, always short, never over.

Every conversion to paise goes through :func:`to_paise` so the rounding rule lives in
one place — the same reasoning as ``PayoutService._owed_filter``. Two copies of a
money rule will eventually disagree, and the amount an admin approves must equal the
amount that leaves the bank.
"""

from decimal import ROUND_HALF_UP, Decimal

# Money is rupees with exactly two decimal places, matching Numeric(12, 2).
RUPEE_QUANTUM = Decimal("0.01")


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Coerce an incoming amount to an exact 2dp ``Decimal``.

    Floats are routed through ``str()`` deliberately. ``Decimal(100.09)`` yields
    100.09000000000000341…, because it faithfully expands the binary float, whereas
    ``Decimal(str(100.09))`` yields exactly 100.09 — ``repr`` gives the shortest
    string that round-trips, which is the value the caller meant.
    """
    if isinstance(value, float):
        value = str(value)
    return Decimal(value).quantize(RUPEE_QUANTUM, rounding=ROUND_HALF_UP)


def to_paise(amount: Decimal | int | float | str) -> int:
    """Convert a rupee amount to whole paise for Razorpay / RazorpayX.

    Rounds half up rather than truncating, so ₹80.07 becomes 8007 paise and not
    8006. Both the customer charge and the provider payout go through here.
    """
    return int((to_decimal(amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))
