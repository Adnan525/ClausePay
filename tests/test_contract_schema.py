import pytest

from clausepay.contract_schema import validate_refined_contract
from tests.conftest import good_contract


def test_good_contract_passes():
    assert validate_refined_contract(good_contract()) == []


def mutate(fn):
    c = good_contract()
    fn(c)
    return c


@pytest.mark.parametrize("bad", [
    mutate(lambda c: c["splits"][0]["shares"][0].update(percent=50)),          # sums to 90
    mutate(lambda c: c.update(paypal_fee_bearer="Carol")),                      # unknown party
    mutate(lambda c: c.update(payout_fee_bearer="nobody")),
    mutate(lambda c: c["holdback"].update(percent=100)),
    mutate(lambda c: c["recoupment"].update(amount=0)),
    mutate(lambda c: c["splits"][0].update(up_to_cumulative_net=500)),          # last tier must be null
    mutate(lambda c: c.update(parties=["Alice", "Alice"])),
    mutate(lambda c: c.update(splits=[])),
    mutate(lambda c: c["refunds"].update(shortfall_covered_by="single_party", shortfall_party=None)),
])
def test_bad_contracts_are_rejected(bad):
    assert validate_refined_contract(bad)


def test_tiers_must_increase():
    c = good_contract()
    shares = c["splits"][0]["shares"]
    c["splits"] = [
        {"up_to_cumulative_net": 500, "shares": shares},
        {"up_to_cumulative_net": 300, "shares": shares},
        {"up_to_cumulative_net": None, "shares": shares},
    ]
    assert validate_refined_contract(c)
