from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from clausepay.contract_payments import ContractPayments, Split
from clausepay.paypal_client import PayPalError


class SixtyForty(ContractPayments):
    """A 60/40 contract with a configurable (possibly buggy) result."""

    def __init__(self, client: MagicMock, payout: Split | None = None, refund: Split | None = None) -> None:
        super().__init__(client)
        self.payout = payout
        self.refund = refund


    def calculate_payout(self, sale_amount: Decimal) -> Split:
        if self.payout is not None:
            return self.payout
        return {"a@x.com": sale_amount * Decimal("0.6"), "b@x.com": sale_amount * Decimal("0.4")}


    def calculate_refund(self, refund_amount: Decimal) -> Split:
        if self.refund is not None:
            return self.refund
        return {"a@x.com": refund_amount * Decimal("0.6"), "b@x.com": refund_amount * Decimal("0.4")}


def test_cannot_instantiate_without_implementing_abstract_methods() -> None:
    with pytest.raises(TypeError):
        ContractPayments(MagicMock())  # type: ignore[abstract]


def test_valid_payout_passes_through() -> None:
    contract = SixtyForty(MagicMock())
    assert contract.payout_for_sale(Decimal("100.00")) == {"a@x.com": Decimal("60.00"), "b@x.com": Decimal("40.00")}


def test_zero_amounts_are_dropped() -> None:
    contract = SixtyForty(MagicMock(), payout={"a@x.com": Decimal("10.00"), "b@x.com": Decimal("0.00")})
    assert contract.payout_for_sale(Decimal("10.00")) == {"a@x.com": Decimal("10.00")}


@pytest.mark.parametrize("bad", [
    {"a@x.com": Decimal("60.001")},
    {"a@x.com": Decimal("-1.00")},
    {"a@x.com": 60.0},
    {"not-an-email": Decimal("1.00")},
    {"a@x.com": Decimal("80.00"), "b@x.com": Decimal("30.00")},
])
def test_invalid_payouts_are_rejected(bad: Split) -> None:
    with pytest.raises(PayPalError):
        SixtyForty(MagicMock(), payout=bad).payout_for_sale(Decimal("100.00"))


def test_refund_funding_cannot_exceed_refund() -> None:
    contract = SixtyForty(MagicMock(), refund={"a@x.com": Decimal("101.00")})
    with pytest.raises(PayPalError):
        contract.funding_for_refund(Decimal("100.00"))


def test_send_sale_payout_sends_validated_split() -> None:
    client = MagicMock()
    SixtyForty(client).send_sale_payout(Decimal("100.00"))
    client.send_payouts.assert_called_once_with({"a@x.com": Decimal("60.00"), "b@x.com": Decimal("40.00")})


def test_balance_covers() -> None:
    client = MagicMock()
    client.get_available_balance.return_value = Decimal("50.00")
    contract = SixtyForty(client)
    assert contract.balance_covers(Decimal("50.00")) is True
    assert contract.balance_covers(Decimal("50.01")) is False
