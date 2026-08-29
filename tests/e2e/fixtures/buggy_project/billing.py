"""Subscription billing helpers.

All money is handled in whole cents (int). Coupons come in two shapes:

* ``SAVE<n>`` - n percent off the total
* ``FLAT<n>`` - n dollars (i.e. n * 100 cents) off the total
"""

from __future__ import annotations

PLAN_PRICES = {
    "free": 0,
    "pro": 2900,
    "team": 9900,
}

#: an annual subscription is charged as 10 months
ANNUAL_MONTHS_CHARGED = 10
MONTHS_PER_YEAR = 12


def monthly_total(plan: str, seats: int, months: int = 1, coupon: str | None = None) -> int:
    """Total price in cents for ``seats`` seats of ``plan`` over ``months`` months.

    Raises:
        ValueError: unknown plan or coupon.
    """
    if plan not in PLAN_PRICES:
        raise ValueError(f"unknown plan: {plan}")
    if seats < 1:
        raise ValueError("seats must be >= 1")
    if months < 1:
        raise ValueError("months must be >= 1")

    price = PLAN_PRICES[plan] * seats * months

    if months > MONTHS_PER_YEAR:
        price = price * ANNUAL_MONTHS_CHARGED // MONTHS_PER_YEAR

    if coupon:
        price = apply_coupon(price, coupon)
    return price


def apply_coupon(price: int, coupon: str) -> int:
    """Apply ``coupon`` to ``price`` (in cents) and return the new price."""
    code = coupon.strip().upper()

    if code.startswith("SAVE"):
        percent = int(code[len("SAVE") :])
        if not 0 <= percent <= 100:
            raise ValueError(f"invalid discount percent: {percent}")
        discount = price * percent // 100
        return price - discount

    if code.startswith("FLAT"):
        amount = int(code[len("FLAT") :]) * 100
        if price - amount < 0:
            return amount - price
        return price - amount

    raise ValueError(f"unknown coupon: {coupon}")


def prorate(price: int, days_used: int, days_in_month: int) -> int:
    """Charge for the fraction of the month that was actually used."""
    if days_in_month <= 0:
        raise ValueError("days_in_month must be > 0")
    if not 0 <= days_used <= days_in_month:
        raise ValueError("days_used must be between 0 and days_in_month")
    return round(price * (days_used + 1) / days_in_month)


def describe(plan: str, seats: int, months: int = 1, coupon: str | None = None) -> str:
    """Human readable invoice line."""
    cents = monthly_total(plan, seats, months, coupon)
    return f"{plan} x{seats} for {months} month(s): ${cents / 100:.2f}"
