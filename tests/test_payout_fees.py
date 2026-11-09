from decimal import Decimal

import pytest

from clausepay.contract_runner import ContractRunner
from clausepay.contract_sandbox import run_function
from clausepay.contract_sim import ContractProblem, apply_sale, parse_tests, run_tests, simulate_scenario
from clausepay.fees import payout_fee
from tests.contracts import REFERENCE_CODE, REFERENCE_TESTS, expect, refund, sale
from tests.test_contract_runner import freeze_files

PARTIES = ["Alice", "Bob"]

# Alice 60% / Bob 40% of what the business receives; each recipient bears the payout fee on their own payout.
EACH_BEARS_OWN_FEE = '''
def calculate_payout(sale_id, sale_amount, paypal_fee, state):
    net = sale_amount - paypal_fee
    alice_share = (net * Decimal("0.60")).quantize(CENT, rounding=ROUND_HALF_UP)
    bob_share = net - alice_share
    payouts = {}
    covered = Decimal("0.00")
    sent_total = Decimal("0.00")
    for name, share in (("Alice", alice_share), ("Bob", bob_share)):
        x = share - payout_fee(share)
        for i in range(5):
            if x + Decimal("0.01") + payout_fee(x + Decimal("0.01")) <= share:
                x = x + Decimal("0.01")
        payouts[name] = x
        covered = covered + payout_fee(x)
        sent_total = sent_total + x
    leftover = net - sent_total - covered
    return {"payouts": payouts, "held_back_change": leftover, "payout_fees_covered": covered, "state": state, "notes": []}


def calculate_refund(sale_id, refund_amount, fee_returned, state):
    cost = refund_amount - fee_returned
    alice = (cost / 2).quantize(CENT, rounding=ROUND_DOWN)
    return {"funding": {"Alice": alice, "Bob": cost - alice}, "from_holdback": Decimal("0.00"), "state": state, "notes": []}
'''


def d(value: str) -> Decimal:
    return Decimal(value)


# ---------- the fee itself ----------

@pytest.mark.parametrize("amount, fee", [("97.30", "1.95"), ("292.50", "5.85"), ("0.01", "0.00"), ("0.25", "0.01"), ("1000.00", "20.00")])
def test_payout_fee_is_two_percent_rounded_half_up(amount: str, fee: str) -> None:
    assert payout_fee(d(amount)) == d(fee)


def test_payout_fee_can_be_capped_and_reconfigured_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("payout_fee_cap", "1.25")
    assert payout_fee(d("1000.00")) == d("1.25") and payout_fee(d("50.00")) == d("1.00")
    monkeypatch.setenv("payout_fee_rate", "0.01")
    assert payout_fee(d("50.00")) == d("0.50")


def test_the_helper_inside_the_sandbox_gives_exactly_the_same_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    code = '''
def calculate_payout(sale_id, sale_amount, paypal_fee, state):
    return {"fee": payout_fee(sale_amount)}
def calculate_refund(sale_id, refund_amount, fee_returned, state):
    return {}
'''
    for env in ({}, {"payout_fee_cap": "1.25"}, {"payout_fee_rate": "0.035", "payout_fee_cap": "3.00"}):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        for amount in ("0.01", "0.25", "33.33", "97.30", "292.50", "999.99"):
            got = run_function(code, "calculate_payout", {"sale_id": "S", "sale_amount": d(amount), "paypal_fee": d("0"), "state": {}})
            assert got["fee"] == payout_fee(d(amount)), (env, amount)
        for key in env:
            monkeypatch.delenv(key)


# ---------- the rule: payouts + payout fees covered + held back == amount received ----------

def test_a_contract_where_each_recipient_bears_their_own_fee_balances_exactly() -> None:
    outcome = apply_sale(EACH_BEARS_OWN_FEE, PARTIES, "S1", d("100.00"), d("2.70"), {}, d("0.00"))
    assert outcome.amounts == {"Alice": d("57.24"), "Bob": d("38.16")}
    assert outcome.payout_fees == d("1.90") and outcome.payout_fees_covered == d("1.90")
    assert sum(outcome.amounts.values()) + outcome.payout_fees_covered + outcome.reserve_change == d("97.30")
    assert any("PayPal will charge the business A$1.90 in payout fees" in line and "A$1.90 of that comes out of this sale's money"
               in line and "absorbs A$0.00" in line for line in outcome.lines)


@pytest.mark.parametrize("cents", ["0.01", "1.00", "33.33", "250.00", "999.99"])
def test_the_each_bears_own_fee_pattern_balances_for_awkward_amounts(cents: str) -> None:
    amount = d(cents)
    fee = amount * d("0.024")
    paypal_fee = (fee.quantize(d("0.01")) + d("0.30"))
    if paypal_fee >= amount:
        pytest.skip("PayPal's fee would swallow the sale")
    outcome = apply_sale(EACH_BEARS_OWN_FEE, PARTIES, "S1", amount, paypal_fee, {}, d("0.00"))
    assert sum(outcome.amounts.values()) + outcome.payout_fees_covered + outcome.reserve_change == amount - paypal_fee


def test_a_business_that_absorbs_the_fee_says_so() -> None:
    outcome = apply_sale(REFERENCE_CODE, PARTIES, "S1", d("300.00"), d("7.50"), {}, d("0.00"))
    assert outcome.payout_fees == d("5.85") and outcome.payout_fees_covered == d("0.00")
    assert any("the business absorbs A$5.85" in line for line in outcome.lines)


def test_a_sale_with_no_payouts_has_no_payout_fees() -> None:
    nothing = REFERENCE_CODE.replace('payouts = {"Alice": to_debt + alice_share, "Bob": rest - alice_share}', 'payouts = {}').replace(
        '"held_back_change": Decimal("0.00")', '"held_back_change": net')
    outcome = apply_sale(nothing, PARTIES, "S1", d("100.00"), d("2.70"), {}, d("0.00"))
    assert outcome.amounts == {} and outcome.payout_fees == d("0.00")


def test_covering_more_than_paypal_will_charge_is_refused() -> None:
    greedy = REFERENCE_CODE.replace('"payout_fees_covered": Decimal("0.00")', '"payout_fees_covered": Decimal("9.99")')
    with pytest.raises(ContractProblem, match="must be between 0 and the payout fees PayPal will charge"):
        apply_sale(greedy, PARTIES, "S1", d("100.00"), d("2.70"), {}, d("0.00"))


def test_a_negative_covered_amount_is_refused() -> None:
    bad = REFERENCE_CODE.replace('"payout_fees_covered": Decimal("0.00")', '"payout_fees_covered": Decimal("-1.00")')
    with pytest.raises(ContractProblem, match="must be between 0"):
        apply_sale(bad, PARTIES, "S1", d("100.00"), d("2.70"), {}, d("0.00"))


def test_forgetting_to_declare_the_covered_fees_is_refused_with_a_helpful_message() -> None:
    forgot = REFERENCE_CODE.replace('"payout_fees_covered": Decimal("0.00"),', "")
    with pytest.raises(ContractProblem, match="must include payout_fees_covered"):
        apply_sale(forgot, PARTIES, "S1", d("100.00"), d("2.70"), {}, d("0.00"))


def test_covering_fees_without_reducing_the_payouts_breaks_the_balance() -> None:
    unbalanced = REFERENCE_CODE.replace('"payout_fees_covered": Decimal("0.00")', '"payout_fees_covered": Decimal("1.95")')
    with pytest.raises(ContractProblem, match="plus payout_fees_covered \\(A\\$1.95\\) must equal the amount received"):
        apply_sale(unbalanced, PARTIES, "S1", d("100.00"), d("2.70"), {}, d("0.00"))


def test_scenario_totals_show_the_payout_fees_and_what_the_business_absorbs() -> None:
    scenario = {"title": "t", "events": [sale("300.00"), refund("full", 1)]}
    totals = simulate_scenario(REFERENCE_CODE, PARTIES, scenario, 1)["totals"]
    assert totals["payout_fees"] == "5.85" and totals["payout_fees_absorbed"] == "5.85"


# ---------- Claude's own tests state the fees they expect ----------

def test_claudes_tests_must_state_the_expected_covered_fees() -> None:
    tests = [dict(test, events=[dict(event) for event in test["events"]]) for test in REFERENCE_TESTS]
    del tests[0]["events"][0]["expected_payout_fees_covered"]
    with pytest.raises(ContractProblem, match="expected_payout_fees_covered must be"):
        parse_tests(tests, PARTIES)


def test_a_wrong_expectation_about_covered_fees_is_caught() -> None:
    tests = [{"title": "t1", "events": [{**sale("100.00"), **expect(Alice="57.24", Bob="38.16", covered="1.00")}]},
             REFERENCE_TESTS[1], REFERENCE_TESTS[2]]
    with pytest.raises(ContractProblem, match="expected payout fees covered A\\$1.00 but the code gave A\\$1.90"):
        run_tests(EACH_BEARS_OWN_FEE, PARTIES, parse_tests(tests, PARTIES))


def test_a_correct_expectation_passes() -> None:
    tests = [{"title": "t1", "events": [{**sale("100.00"), **expect(Alice="57.24", Bob="38.16", covered="1.90")}]},
             {"title": "t2", "events": [{**sale("100.00"), **expect(Alice="57.24", Bob="38.16", covered="1.90")},
                                         {**refund("full", 1), **expect(Alice="48.80", Bob="48.80")}]},
             {"title": "t3", "events": [{**sale("100.00"), **expect(Alice="57.24", Bob="38.16", covered="1.90")}]}]
    run_tests(EACH_BEARS_OWN_FEE, PARTIES, parse_tests(tests, PARTIES))


# ---------- the runner records them ----------

def test_the_runner_records_the_expected_payout_fees_and_the_state_before(tmp_path) -> None:
    runner = ContractRunner(freeze_files(tmp_path))
    first = runner.process_sale("S1", d("100.00"), d("2.70"))
    assert first.payout_fees == d("1.95") and first.payout_fees_covered == d("0.00") and first.state_before == {}
    runner.commit(first.entry_id)
    second = runner.process_sale("S2", d("150.00"), d("3.90"))
    assert second.state_before == {"owed": d("102.70")} and second.state_after == {"owed": d("0.00")}
    assert runner.entries()[0]["payout_fees"] == d("1.95")
