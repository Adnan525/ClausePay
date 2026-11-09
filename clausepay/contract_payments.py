"""Contract-driven money movement.

ClausePay generates one subclass per contract. The subclass only answers two
questions (who gets what from a sale, who funds what for a refund). Everything
else, including validating those answers and talking to PayPal, is static code
here, so a bad generated function is caught before any money moves.
"""
from abc import ABC, abstractmethod
from decimal import Decimal

from clausepay.paypal_client import CENT, Json, PayPalClient, PayPalError

Split = dict[str, Decimal]


class ContractPayments(ABC):
    def __init__(self, client: PayPalClient) -> None:
        self.client = client


    @abstractmethod
    def calculate_payout(self, sale_amount: Decimal) -> Split:
        """How one sale is paid out.

        Input: the sale amount (net of PayPal fees, as decided by the caller).
        Output: {recipient_email: amount_to_pay}. Amounts are Decimal, whole
        cents, non-negative, and must not add up to more than the sale.
        """


    @abstractmethod
    def calculate_refund(self, refund_amount: Decimal) -> Split:
        """How one refund is funded.

        Input: the amount being refunded to the buyer.
        Output: {partner_email: amount_they_must_transfer_to_the_business}.
        Amounts are Decimal, whole cents, non-negative, and must not add up
        to more than the refund.
        """


    @staticmethod
    def _validate_split(split: Split, limit: Decimal, label: str) -> Split:
        if not isinstance(split, dict):
            raise PayPalError(f"{label} must return a dict, got {type(split).__name__}")
        for email, amount in split.items():
            if not isinstance(email, str) or "@" not in email:
                raise PayPalError(f"{label}: invalid email {email!r}")
            if not isinstance(amount, Decimal):
                raise PayPalError(f"{label}: amount for {email} must be Decimal, got {type(amount).__name__}")
            if amount < 0 or amount != amount.quantize(CENT):
                raise PayPalError(f"{label}: amount for {email} must be non-negative whole cents, got {amount}")
        if sum(split.values(), Decimal("0")) > limit:
            raise PayPalError(f"{label}: total {sum(split.values(), Decimal('0'))} exceeds {limit}")
        return split


    def payout_for_sale(self, sale_amount: Decimal) -> Split:
        """The validated payout split for a sale. Zero amounts are dropped."""
        split = self._validate_split(self.calculate_payout(sale_amount), sale_amount, "calculate_payout")
        return {email: amount for email, amount in split.items() if amount > 0}


    def funding_for_refund(self, refund_amount: Decimal) -> Split:
        """The validated amounts partners must transfer in before a refund."""
        split = self._validate_split(self.calculate_refund(refund_amount), refund_amount, "calculate_refund")
        return {email: amount for email, amount in split.items() if amount > 0}


    def send_sale_payout(self, sale_amount: Decimal) -> Json:
        """Pay out a sale according to the contract. Returns the PayPal batch header."""
        return self.client.send_payouts(self.payout_for_sale(sale_amount))


    def balance_covers(self, amount: Decimal) -> bool:
        """Sanity check: does the business account hold at least `amount`?"""
        return self.client.get_available_balance() >= amount
