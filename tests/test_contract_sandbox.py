from decimal import Decimal

import pytest

from clausepay.contract_sandbox import SandboxError, check_code, run_function

GOOD = '''
def calculate_payout(sale_id, sale_amount, paypal_fee, state):
    net = sale_amount - paypal_fee
    a = (net * Decimal("0.6")).quantize(CENT, rounding=ROUND_HALF_UP)
    return {"payouts": {"Alice": a, "Bob": net - a}, "held_back_change": Decimal("0"), "state": {"n": 1}, "notes": []}


def calculate_refund(sale_id, refund_amount, fee_returned, state):
    cost = refund_amount - fee_returned
    return {"funding": {"Alice": cost}, "from_holdback": Decimal("0"), "state": state, "notes": []}
'''


def test_good_code_passes_the_static_check() -> None:
    assert check_code(GOOD) == []


@pytest.mark.parametrize("snippet, fragment", [
    ("import os", "imports"),
    ("from os import path", "imports"),
    ("x = float('1')", "'float'"),
    ("x = 1.5", "floating-point"),
    ("while True:\n    pass", "while"),
    ("x = (1).__class__", "private attributes"),
    ("x = open('f')", "'open'"),
    ("x = eval('1')", "'eval'"),
    ("x = 9 ** 9", "**"),
    ("class A:\n    pass", "classes"),
    ("print(1)", "'print'"),
])
def test_dangerous_code_is_rejected(snippet: str, fragment: str) -> None:
    problems = check_code(GOOD + "\n" + snippet)
    assert any(fragment in problem for problem in problems), problems


def test_missing_function_and_wrong_parameters_are_rejected() -> None:
    assert "missing function calculate_refund" in check_code("def calculate_payout(sale_id, sale_amount, paypal_fee, state):\n    return {}")
    wrong = GOOD.replace("sale_id, sale_amount, paypal_fee, state", "amount")
    assert any("exactly the parameters" in p for p in check_code(wrong))


def test_syntax_error_is_reported() -> None:
    assert "syntax error" in check_code("def (:")[0]


def test_runs_a_function_with_exact_decimals_and_state() -> None:
    result = run_function(GOOD, "calculate_payout", {
        "sale_id": "S1", "sale_amount": Decimal("100.00"), "paypal_fee": Decimal("2.70"), "state": {}})
    assert result["payouts"] == {"Alice": Decimal("58.38"), "Bob": Decimal("38.92")}
    assert all(isinstance(v, Decimal) for v in result["payouts"].values())
    assert result["state"] == {"n": 1}


def test_decimals_inside_state_survive_the_round_trip() -> None:
    result = run_function(GOOD, "calculate_refund", {
        "sale_id": "S1", "refund_amount": Decimal("10.00"), "fee_returned": Decimal("0.24"),
        "state": {"owed": Decimal("12.34")}})
    assert result["state"] == {"owed": Decimal("12.34")}


def test_runtime_errors_become_sandbox_errors() -> None:
    broken = GOOD.replace('net = sale_amount - paypal_fee', 'net = sale_amount / Decimal("0")')
    with pytest.raises(SandboxError, match="DivisionByZero|InvalidOperation"):
        run_function(broken, "calculate_payout", {
            "sale_id": "S1", "sale_amount": Decimal("1"), "paypal_fee": Decimal("0"), "state": {}})


def test_unknown_names_do_not_exist_at_runtime() -> None:
    sneaky = GOOD.replace('net = sale_amount - paypal_fee', 'net = sale_amount - paypal_fee\n    z = __builtins__')
    # blocked statically (dunder name) before it can run
    with pytest.raises(SandboxError, match="not allowed"):
        run_function(sneaky, "calculate_payout", {
            "sale_id": "S1", "sale_amount": Decimal("1"), "paypal_fee": Decimal("0"), "state": {}})


def test_endless_loops_are_stopped() -> None:
    slow = GOOD.replace('net = sale_amount - paypal_fee', 'net = sale_amount - paypal_fee\n    for i in range(10**9):\n        net = net + 0')
    slow = slow.replace("10**9", "1000000000")
    with pytest.raises(SandboxError, match="longer than|crashed|raised"):
        run_function(slow, "calculate_payout", {
            "sale_id": "S1", "sale_amount": Decimal("1"), "paypal_fee": Decimal("0"), "state": {}})
