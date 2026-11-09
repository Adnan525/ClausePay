import copy
from decimal import Decimal
from typing import Any

import pytest

from clausepay.contract_sim import (
    ContractProblem, parse_scenario, parse_submission, parse_tests, run_tests, self_test, simulate_scenario,
    simulate_slots,
)
from tests.contracts import (
    PARTY_NAMES, REFERENCE_CODE, REFERENCE_TESTS, SCENARIOS, expect, reference_submission, refund, sale, with_expected,
)


def run(index: int, code: str = REFERENCE_CODE) -> dict[str, Any]:
    scenario = parse_scenario(index, SCENARIOS[index])
    return simulate_scenario(code, PARTY_NAMES, scenario, index + 1)


def mutated(find: str, replace: str) -> str:
    assert find in REFERENCE_CODE, find
    return REFERENCE_CODE.replace(find, replace)


def test_sale_steps_explain_fee_net_and_split() -> None:
    first = run(0)["steps"][0]
    text = " ".join(first["lines"])
    assert "A$100.00" in text and "A$2.70" in text and "A$97.30" in text
    assert "Alice is paid A$97.30" in text and "Bob is paid A$0.00" in text
    assert "Nothing is held back" in text and "Check: payouts A$97.30" in text
    assert first["notes"] == ["Alice recoups her investment first: 97.30 of this sale."]


def test_state_carries_between_sales_so_the_investment_is_recouped_once() -> None:
    text = " ".join(run(0)["steps"][1]["lines"])
    # net 146.10; A$102.70 still owed, the remaining 43.40 splits 26.04 / 17.36
    assert "Alice is paid A$128.74" in text and "Bob is paid A$17.36" in text


def test_totals_add_up() -> None:
    totals = run(0)["totals"]
    assert totals["buyers_paid"] == "250.00" and totals["paypal_fees_kept"] == "6.60"
    assert totals["paid_out"] == {"Alice": "226.04", "Bob": "17.36"}  # 243.40 net


def test_full_refund_returns_the_percentage_fee_and_is_funded_by_partners() -> None:
    text = " ".join(run(1)["steps"][1]["lines"])
    assert "refunded A$80.00" in text and "returns A$1.92" in text and "costs the business A$78.08" in text
    assert "Alice must transfer A$39.04" in text and "Bob must transfer A$39.04" in text


def test_partial_refund_and_later_sale_use_the_remembered_state() -> None:
    steps = run(2)["steps"]
    assert "returns A$2.40" in " ".join(steps[1]["lines"])
    assert "Alice is paid" in " ".join(steps[2]["lines"])


def test_payouts_that_do_not_add_up_are_reported_not_shown() -> None:
    code = mutated('"Bob": rest - alice_share', '"Bob": rest - alice_share + Decimal("0.01")')
    with pytest.raises(ContractProblem, match="must equal the amount received after PayPal's fee"):
        run(0, code)


def test_unknown_party_is_reported() -> None:
    with pytest.raises(ContractProblem, match="not one of the parties"):
        run(0, mutated('"Bob": rest', '"Carol": rest'))


def test_releasing_more_than_the_reserve_holds_is_reported() -> None:
    with pytest.raises(ContractProblem):
        run(0, mutated('"held_back_change": Decimal("0.00")', '"held_back_change": Decimal("-1.00")'))


def test_refund_funding_must_match_the_cost() -> None:
    with pytest.raises(ContractProblem, match="must equal the refund's cost"):
        run(1, mutated('"Bob": cost - alice', '"Bob": cost'))


def test_refunding_more_than_the_sale_is_reported() -> None:
    scenario = parse_scenario(0, {"events": [sale("80.00"), refund("90.00", 1)]})
    with pytest.raises(ContractProblem, match="only A\\$80.00"):
        simulate_scenario(REFERENCE_CODE, PARTY_NAMES, scenario, 1)


def test_a_second_refund_only_gets_what_is_left() -> None:
    scenario = parse_scenario(0, {"events": [sale("100.00"), refund("60.00", 1), refund("50.00", 1)]})
    with pytest.raises(ContractProblem, match="only A\\$40.00"):
        simulate_scenario(REFERENCE_CODE, PARTY_NAMES, scenario, 1)


# ---------- what the user types ----------

def test_a_user_scenario_is_cleaned_up() -> None:
    scenario = parse_scenario(1, {"title": "  ", "events": [
        {"type": "sale", "amount": " 100.00 ", "sale_number": None},
        {"type": "refund", "amount": "", "sale_number": "1"}]})
    assert scenario["title"] == "Scenario 2"
    assert scenario["events"] == [sale("100.00"), refund("full", 1)]


@pytest.mark.parametrize("events, fragment", [
    ([], "needs 1 to 10 events"),
    ([sale("0.50")], "sale amount must be"),
    ([sale("12.345")], "sale amount must be"),
    ([sale("abc")], "sale amount must be"),
    ([sale("2000.00")], "sale amount must be"),
    ([refund("5.00", 1)], "which earlier sale"),
    ([sale("10.00"), refund("5.00", 2)], "which earlier sale"),
    ([sale("10.00"), refund("x", 1)], "refund amount must be"),
    ([{"type": "gift", "amount": "1", "sale_number": 0}], "sale or refund"),
    ([sale("10.00")] * 11, "needs 1 to 10 events"),
])
def test_bad_user_scenarios_are_explained(events: list[dict[str, Any]], fragment: str) -> None:
    with pytest.raises(ContractProblem, match=fragment):
        parse_scenario(0, {"events": events})


def test_a_scenario_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(ContractProblem):
        parse_scenario(0, ["nope"])


def test_saved_scenarios_are_re_run_and_failures_become_errors() -> None:
    saved = [parse_scenario(0, SCENARIOS[0]), None, parse_scenario(2, SCENARIOS[2])]
    good = simulate_slots(REFERENCE_CODE, PARTY_NAMES, saved)
    assert good[1] is None and "steps" in good[0] and "steps" in good[2]
    broken = simulate_slots(mutated('"Bob": rest', '"Carol": rest'), PARTY_NAMES, saved)
    assert "not one of the parties" in broken[0]["error"] and broken[1] is None


# ---------- Claude's submission and the built-in self-test ----------

def test_the_reference_contract_passes_the_self_test() -> None:
    self_test(REFERENCE_CODE, PARTY_NAMES)


def test_the_self_test_catches_a_contract_that_loses_money() -> None:
    with pytest.raises(ContractProblem, match="Built-in check"):
        self_test(mutated('"Bob": rest - alice_share', '"Bob": rest - alice_share - Decimal("0.01")'), PARTY_NAMES)


def test_the_self_test_catches_a_contract_that_fails_on_tiny_amounts() -> None:
    code = mutated("net = sale_amount - paypal_fee", 'net = sale_amount - paypal_fee\n    if net < Decimal("5"):\n        raise ValueError("too small")')
    with pytest.raises(ContractProblem, match="too small"):
        self_test(code, PARTY_NAMES)


def test_submission_shape_problems_are_all_listed() -> None:
    data = reference_submission()
    data["parties"] = []
    data["summary"] = []
    with pytest.raises(ContractProblem) as caught:
        parse_submission(data)
    text = " ".join(caught.value.problems)
    assert "parties must be" in text and "summary must have" in text


def test_a_submission_needs_no_scenarios() -> None:
    assert "scenarios" not in reference_submission()
    assert parse_submission(reference_submission()).parties[0]["name"] == "Alice"


def test_bad_code_is_rejected_before_any_run() -> None:
    data = reference_submission()
    data["code"] = "import os\n" + REFERENCE_CODE
    with pytest.raises(ContractProblem, match="imports are not allowed"):
        parse_submission(data)


# ---------- Claude's internal tests ----------

def test_the_reference_tests_were_worked_out_correctly() -> None:
    tests = parse_tests(REFERENCE_TESTS, PARTY_NAMES)
    assert run_tests(REFERENCE_CODE, PARTY_NAMES, tests) == [
        "Recoupment over two sales", "Full refund after a sale", "Partial refund then another sale"]


def test_a_submission_includes_its_parsed_tests() -> None:
    submission = parse_submission(reference_submission())
    assert len(submission.tests) == 3 and submission.tests[1]["expected"][1]["amounts"]["Alice"] == Decimal("39.04")


def test_code_that_disagrees_with_the_expected_values_is_caught_with_the_exact_difference() -> None:
    code = mutated('alice_share = (rest * Decimal("0.60"))', 'alice_share = (rest * Decimal("0.50"))')
    with pytest.raises(ContractProblem) as caught:
        run_tests(code, PARTY_NAMES, parse_tests(REFERENCE_TESTS, PARTY_NAMES))
    text = " ".join(caught.value.problems)
    assert "Test 1 ('Recoupment over two sales'), event 2 (sale 150.00): expected Alice to be A$128.74 but the code gave A$124.40" in text
    assert "expected Bob to be A$17.36 but the code gave A$21.70" in text


def test_a_wrong_expected_value_is_caught_too() -> None:
    tests = copy.deepcopy(REFERENCE_TESTS)
    tests[1]["events"][1]["expected_amounts"][0]["amount"] = "39.05"
    with pytest.raises(ContractProblem, match="expected Alice to be A\\$39.05 but the code gave A\\$39.04"):
        run_tests(REFERENCE_CODE, PARTY_NAMES, parse_tests(tests, PARTY_NAMES))


def test_a_party_left_out_of_the_expected_list_is_expected_to_get_nothing() -> None:
    tests = copy.deepcopy(REFERENCE_TESTS)
    tests[0]["events"][0] = with_expected(sale("100.00"), Alice="97.30")  # Bob omitted: expected 0.00, which is right
    run_tests(REFERENCE_CODE, PARTY_NAMES, parse_tests(tests, PARTY_NAMES))
    tests[0]["events"][1] = with_expected(sale("150.00"), Alice="128.74")  # Bob omitted but really gets 17.36
    with pytest.raises(ContractProblem, match="expected Bob to be A\\$0.00 but the code gave A\\$17.36"):
        run_tests(REFERENCE_CODE, PARTY_NAMES, parse_tests(tests, PARTY_NAMES))


def test_the_expected_reserve_movement_is_compared() -> None:
    tests = copy.deepcopy(REFERENCE_TESTS)
    tests[0]["events"][0]["expected_held_back"] = "5.00"
    with pytest.raises(ContractProblem, match="held back / taken from reserve A\\$5.00 but the code gave A\\$0.00"):
        run_tests(REFERENCE_CODE, PARTY_NAMES, parse_tests(tests, PARTY_NAMES))


def test_a_broken_rule_inside_a_test_is_reported_instead_of_compared() -> None:
    code = mutated('"Bob": rest - alice_share', '"Bob": rest')
    with pytest.raises(ContractProblem, match="Claude's test 1, event 2"):
        run_tests(code, PARTY_NAMES, parse_tests(REFERENCE_TESTS, PARTY_NAMES))


@pytest.mark.parametrize("change, fragment", [
    (lambda t: t.pop(), "exactly 3 tests"),
    (lambda t: t[0]["events"][0].update(expected_held_back="abc"), "expected_held_back must be"),
    (lambda t: t[0]["events"][0].update(expected_amounts=[{"party": "Carol", "amount": "1.00"}]), "known party"),
    (lambda t: t[0]["events"][0].update(expected_amounts=[{"party": "Alice", "amount": "-1.00"}]), "non-negative"),
    (lambda t: t[0]["events"][0].update(expected_amounts=[{"party": "Alice", "amount": "1.005"}]), "whole cents"),
    (lambda t: t[1]["events"][1].update(sale_number=4), "which earlier sale"),
    (lambda t: t[0].update(events=[]), "needs 1 to 10 events"),
])
def test_badly_shaped_tests_are_explained(change: Any, fragment: str) -> None:
    tests = copy.deepcopy(REFERENCE_TESTS)
    change(tests)
    with pytest.raises(ContractProblem, match=fragment):
        parse_tests(tests, PARTY_NAMES)
