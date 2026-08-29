"""Test-suite for TinyBilling.

These tests encode the documented behaviour. They are NOT to be modified -
the task is to fix ``billing.py`` until all of them pass.
"""

from __future__ import annotations

import pytest
from billing import apply_coupon, describe, monthly_total, prorate


# --------------------------------------------------------------------------- #
# basic pricing
# --------------------------------------------------------------------------- #
def test_single_pro_seat_one_month():
    assert monthly_total("pro", 1) == 2900


def test_multiple_seats_scale_linearly():
    assert monthly_total("pro", 3) == 8700
    assert monthly_total("team", 2) == 19800


def test_free_plan_costs_nothing():
    assert monthly_total("free", 5) == 0


def test_several_months():
    assert monthly_total("pro", 1, months=3) == 8700


def test_unknown_plan_raises():
    with pytest.raises(ValueError):
        monthly_total("enterprise", 1)


def test_invalid_seats_and_months_raise():
    with pytest.raises(ValueError):
        monthly_total("pro", 0)
    with pytest.raises(ValueError):
        monthly_total("pro", 1, months=0)


# --------------------------------------------------------------------------- #
# annual billing
# --------------------------------------------------------------------------- #
def test_annual_plan_is_charged_as_ten_months():
    # 12 months at 2900 = 34800, charged as 10 months = 29000
    assert monthly_total("pro", 1, months=12) == 29000


def test_eleven_months_is_not_discounted():
    assert monthly_total("pro", 1, months=11) == 31900


def test_annual_with_multiple_seats():
    # 2 team seats * 12 months = 237600 -> charged as 10 months
    assert monthly_total("team", 2, months=12) == 198000


# --------------------------------------------------------------------------- #
# coupons
# --------------------------------------------------------------------------- #
def test_percent_coupon_subtracts_the_discount():
    # 2900 - 10% = 2610
    assert apply_coupon(2900, "SAVE10") == 2610


def test_percent_coupon_is_case_insensitive_and_tolerates_spaces():
    assert apply_coupon(2900, "  save10  ") == 2610


def test_full_discount_is_zero():
    assert apply_coupon(2900, "SAVE100") == 0


def test_flat_coupon_subtracts_dollars():
    assert apply_coupon(2900, "FLAT5") == 2400


def test_flat_coupon_clamps_at_zero():
    """A coupon can never make the invoice negative."""
    assert apply_coupon(2900, "FLAT50") == 0


def test_unknown_coupon_raises():
    with pytest.raises(ValueError):
        apply_coupon(2900, "BOGO1")


def test_coupon_applies_to_the_annual_total():
    # annual pro = 29000, 10% off = 26100
    assert monthly_total("pro", 1, months=12, coupon="SAVE10") == 26100


def test_percent_out_of_range_raises():
    with pytest.raises(ValueError):
        apply_coupon(2900, "SAVE150")


# --------------------------------------------------------------------------- #
# proration
# --------------------------------------------------------------------------- #
def test_prorate_half_month():
    assert prorate(3000, 15, 30) == 1500


def test_prorate_full_month():
    assert prorate(3000, 30, 30) == 3000


def test_prorate_zero_days():
    assert prorate(3000, 0, 30) == 0


def test_prorate_validates_input():
    with pytest.raises(ValueError):
        prorate(3000, 31, 30)
    with pytest.raises(ValueError):
        prorate(3000, 5, 0)


# --------------------------------------------------------------------------- #
# invoice text
# --------------------------------------------------------------------------- #
def test_describe_formats_dollars():
    assert describe("pro", 1) == "pro x1 for 1 month(s): $29.00"


def test_describe_with_coupon():
    line = describe("pro", 2, months=2, coupon="SAVE50")
    assert line.endswith("$58.00")
