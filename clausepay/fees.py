"""PayPal fee arithmetic. Pure functions, so contract simulations need no PayPal credentials.

Rates are the ones the PayPal sandbox charged during testing (2.4% + A$0.30 per sale). The
percentage part is returned pro rata on a refund; the fixed part never is.
"""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
DEFAULT_FEE_RATE = Decimal("0.024")  # percentage part of PayPal's fee
DEFAULT_FIXED_FEE = Decimal("0.30")  # fixed part of PayPal's fee per sale (never returned on refund)


def cents(amount: Decimal) -> Decimal:
    """`amount` rounded half-up to whole cents."""
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def percentage_fee(amount: Decimal, rate: Decimal = DEFAULT_FEE_RATE) -> Decimal:
    """The percentage part of PayPal's fee on `amount` (also what a refund of `amount` returns)."""
    return cents(amount * rate)


def sale_fee(amount: Decimal, rate: Decimal = DEFAULT_FEE_RATE, fixed: Decimal = DEFAULT_FIXED_FEE) -> Decimal:
    """The total fee PayPal takes on a sale of `amount`."""
    return percentage_fee(amount, rate) + fixed
