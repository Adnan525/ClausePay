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
    mutate(lambda c: c.update(payees=[])),
    mutate(lambda c: c.update(rules=[])),
    mutate(lambda c: c["payees"].append({"name": "Alice", "description": "dup"})),
    mutate(lambda c: c["payees"][0].update(name="  ")),
])
def test_bad_contracts_are_rejected(bad):
    assert validate_refined_contract(bad)
