"""
SVC-482: ``app.services.price_range``, the one copy of the recommended-range
rule. Standalone: no DB, no Redis. The endpoint side is in
``tests/test_service_ranking.py``.
"""

import logging
import uuid
from decimal import Decimal

import pytest

from app.services.price_range import is_outside_range, recommended_range

D = Decimal


def test_the_tickets_example_is_exact():
    """AC gas charging: ₹2,499 ± 25% = ₹1,874.25 – ₹3,123.75, not rounded."""
    assert recommended_range(D("2499.00"), True, D("25.00")) == (D("1874.25"), D("3123.75"))


def test_bounds_are_not_quantized():
    """An odd reference keeps every digit, so a price on a bound stays on it."""
    low, high = recommended_range(D("1999.99"), True, D("25.00"))
    assert (low, high) == (D("1499.9925"), D("2499.9875"))


@pytest.mark.parametrize(
    ("price", "outside"),
    [
        ("1874.24", True),
        ("1874.25", False),
        ("2499.00", False),
        ("3123.75", False),
        ("3123.76", True),
        ("0.00", True),
    ],
)
def test_the_bounds_are_inclusive(price, outside):
    price_range = recommended_range(D("2499.00"), True, D("25.00"))
    assert is_outside_range(D(price), price_range) is outside


@pytest.mark.parametrize("bounds_pct", [None, D("20.00")])
def test_a_category_that_isnt_sensitive_has_no_range(bounds_pct):
    assert recommended_range(D("1000.00"), False, bounds_pct) is None
    assert is_outside_range(D("1.00"), None) is False
    assert is_outside_range(D("99999.00"), None) is False


@pytest.mark.parametrize("base_price", [D("0"), D("0.00"), D("-1.00")])
def test_a_reference_of_zero_or_less_has_no_range(base_price):
    """A [0, 0] range would flag every SP on the variant; like
    price_competitiveness, no reference means neutral."""
    assert recommended_range(base_price, True, D("25.00")) is None


def test_a_zero_percent_range_is_the_reference_alone():
    price_range = recommended_range(D("500.00"), True, D("0.00"))
    assert price_range == (D("500.00"), D("500.00"))
    assert is_outside_range(D("500.00"), price_range) is False
    assert is_outside_range(D("500.01"), price_range) is True


def test_sensitive_without_bounds_is_no_range_and_is_logged(caplog):
    """The CHECK forbids it; if a row ever slips through, the page still
    renders (no range) and the row is named in a warning."""
    category_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger="app.services.price_range"):
        assert recommended_range(D("1000.00"), True, None, category_id=category_id) is None
    [record] = [r for r in caplog.records if r.name == "app.services.price_range"]
    assert record.levelno == logging.WARNING
    assert "price_range.no_bounds" in record.getMessage()
    assert str(category_id) in record.getMessage()


def test_no_warning_for_a_healthy_row(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.price_range"):
        recommended_range(D("1000.00"), True, D("20.00"))
        recommended_range(D("1000.00"), False, None)
    assert not [r for r in caplog.records if r.name == "app.services.price_range"]
