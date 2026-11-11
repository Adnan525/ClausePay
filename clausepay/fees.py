import os
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


def gross_for_net(net: Decimal, rate: Decimal = DEFAULT_FEE_RATE, fixed: Decimal = DEFAULT_FIXED_FEE) -> Decimal:
    """The smallest amount to charge so that, after PayPal's fee on it, at least `net` reaches the business.

    Used when a partner pays the fee on their own transfer: charging exactly their share would leave the business short.
    """
    gross = cents((net + fixed) / (1 - rate)) - Decimal("0.05")
    while gross - sale_fee(gross, rate, fixed) < net:
        gross += CENT
    return gross


# PayPal charges the sender (the business account) a fee on every payout, on top of the amount sent.
# The sandbox charged 2% with no cap; the published rate is 2% capped per payout. Both are settings
# (environment variables `payout_fee_rate` and `payout_fee_cap`), read each time so a .env change applies.
DEFAULT_PAYOUT_FEE_RATE = Decimal("0.02")


def payout_fee_settings() -> tuple[Decimal, Decimal | None]:
    """The payout fee rate and the per-payout cap (None means no cap)."""
    rate = Decimal(os.getenv("payout_fee_rate", "").strip() or DEFAULT_PAYOUT_FEE_RATE)
    cap = os.getenv("payout_fee_cap", "").strip()
    return rate, (Decimal(cap) if cap else None)


def payout_fee(amount: Decimal, rate: Decimal | None = None, cap: Decimal | None = None) -> Decimal:
    """The fee PayPal charges the business for sending one payout of `amount`."""
    default_rate, default_cap = payout_fee_settings()
    fee = cents(amount * (default_rate if rate is None else rate))
    limit = default_cap if cap is None else cap
    return min(fee, limit) if limit is not None else fee
