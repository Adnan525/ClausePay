"""Dry-run a generated contract: arithmetic only, no PayPal calls, no balances touched.

Claude supplies the contract code and the scenario inputs (sale and refund amounts). Everything
else here is static code: fees, conservation checks, and the plain-English explanation of each
step. If the contract breaks a rule, the problem is reported instead of being shown as a result.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from clausepay.contract_sandbox import SandboxError, check_code, run_function
from clausepay.fees import CENT, DEFAULT_FEE_RATE, DEFAULT_FIXED_FEE, percentage_fee

MIN_SALE = Decimal("1.00")
MAX_SALE = Decimal("1000.00")
MAX_EVENTS = 10
ZERO = Decimal("0.00")


class ContractProblem(Exception):
    """The submission or its dry run broke a rule. `problems` is written for Claude to fix."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Submission:
    summary: list[str]
    parties: list[dict[str, str]]
    code: str
    tests: list[dict[str, Any]]


def aud(amount: Decimal) -> str:
    return f"A${amount:,.2f}"


def _text(value: Any, label: str, problems: list[str], limit: int = 600) -> str:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{label} must be a non-empty string")
        return ""
    return value.strip()[:limit]


def _amount(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount == amount.quantize(CENT) else None


def parse_submission(data: dict[str, Any]) -> Submission:
    """Validate the shape of what Claude sent. Raises ContractProblem listing everything wrong."""
    problems: list[str] = []
    summary = [_text(line, "summary line", problems) for line in data.get("summary", [])]
    if not summary:
        problems.append("summary must have at least one line")
    parties: list[dict[str, str]] = []
    for party in data.get("parties", []):
        parties.append({"name": _text(party.get("name"), "party name", problems, 40),
                        "description": _text(party.get("description"), "party description", problems)})
    names = [party["name"] for party in parties]
    if not 1 <= len(parties) <= 8 or len(set(names)) != len(names):
        problems.append("parties must be 1 to 8 entries with unique names")
    code = data.get("code", "")
    problems += check_code(code) if isinstance(code, str) and code.strip() else ["code is empty"]
    tests: list[dict[str, Any]] = []
    try:
        tests = parse_tests(data.get("tests", []), names)
    except ContractProblem as problem:
        problems += problem.problems
    if problems:
        raise ContractProblem(problems)
    return Submission(summary, parties, code, tests)


def _normalise_events(events: Any) -> list[dict[str, Any]]:
    """The user's events as clean dicts. Blank refund amounts mean 'full'; bad shapes are left to the checks."""
    cleaned: list[dict[str, Any]] = []
    for event in events if isinstance(events, list) else []:
        event = event if isinstance(event, dict) else {}
        amount = str(event.get("amount") if event.get("amount") is not None else "").strip()
        if event.get("type") == "refund" and amount == "":
            amount = "full"
        try:
            sale_number = int(event.get("sale_number") or 0)
        except (TypeError, ValueError):
            sale_number = 0
        cleaned.append({"type": event.get("type"), "amount": amount, "sale_number": sale_number})
    return cleaned


def _check_events(label: str, events: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    if not 1 <= len(events) <= MAX_EVENTS:
        problems.append(f"{label} needs 1 to {MAX_EVENTS} events")
    sales = 0
    for number, event in enumerate(events, start=1):
        where = f"{label}, event {number}"
        if event["type"] == "sale":
            amount = _amount(event["amount"])
            if amount is None or not MIN_SALE <= amount <= MAX_SALE:
                problems.append(f"{where}: a sale amount must be whole cents between {MIN_SALE} and {MAX_SALE}")
            sales += 1
        elif event["type"] == "refund":
            if event["amount"].lower() != "full" and _amount(event["amount"]) is None:
                problems.append(f"{where}: a refund amount must be whole cents, or empty for the full amount")
            if not 1 <= event["sale_number"] <= sales:
                problems.append(f"{where}: a refund must say which earlier sale it is for (1 to {sales})")
        else:
            problems.append(f"{where}: type must be sale or refund")
    return problems


def parse_scenario(slot: int, data: Any) -> dict[str, Any]:
    """Validate a scenario typed in by the user. Raises ContractProblem with messages for the user."""
    if not isinstance(data, dict):
        raise ContractProblem(["The scenario is not in the expected shape"])
    title = str(data.get("title") or "").strip()[:80] or f"Scenario {slot + 1}"
    events = _normalise_events(data.get("events"))
    problems = _check_events(f"Scenario {slot + 1}", events)
    if problems:
        raise ContractProblem(problems)
    return {"title": title, "events": events}


def _clean_split(split: Any, label: str, parties: set[str], where: str) -> dict[str, Decimal]:
    if not isinstance(split, dict):
        raise ContractProblem([f"{where}: {label} must be a dict of party name to Decimal"])
    for name, amount in split.items():
        if name not in parties:
            raise ContractProblem([f"{where}: {label} names '{name}', which is not one of the parties {sorted(parties)}"])
        if not isinstance(amount, Decimal) or amount < 0 or amount != amount.quantize(CENT):
            raise ContractProblem([f"{where}: {label} amount for {name} must be a non-negative Decimal in whole cents, got {amount!r}"])
    return dict(split)


def _clean_money(value: Any, label: str, where: str) -> Decimal:
    if not isinstance(value, Decimal) or value != value.quantize(CENT):
        raise ContractProblem([f"{where}: {label} must be a Decimal in whole cents, got {value!r}"])
    return value


def _clean_extras(result: Any, where: str) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(result, dict) or not isinstance(result.get("state"), dict):
        raise ContractProblem([f"{where}: the function must return a dict that includes a 'state' dict"])
    notes = result.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise ContractProblem([f"{where}: notes must be a list of strings"])
    return result["state"], [note[:300] for note in notes[:10]]


def _share(amount: Decimal, whole: Decimal, of_what: str) -> str:
    return f" ({amount / whole * 100:.1f}% of the {aud(whole)} {of_what})" if whole > 0 else ""


def _run(code: str, function: str, kwargs: dict[str, Any], where: str) -> Any:
    try:
        return run_function(code, function, kwargs)
    except SandboxError as error:
        raise ContractProblem([f"{where}: {error}"]) from None


def _sale_step(code: str, parties: set[str], sale_id: str, amount: Decimal, state: dict[str, Any],
               reserve: Decimal, rate: Decimal, fixed: Decimal, where: str) -> dict[str, Any]:
    percentage = percentage_fee(amount, rate)
    fee = percentage + fixed
    net = amount - fee
    result = _run(code, "calculate_payout", {"sale_id": sale_id, "sale_amount": amount, "paypal_fee": fee, "state": state}, where)
    new_state, notes = _clean_extras(result, where)
    payouts = _clean_split(result.get("payouts"), "payouts", parties, where)
    held = _clean_money(result.get("held_back_change"), "held_back_change", where)
    if sum(payouts.values(), ZERO) + held != net:
        raise ContractProblem([
            f"{where}: payouts ({aud(sum(payouts.values(), ZERO))}) plus held_back_change ({aud(held)}) must equal "
            f"the amount received after PayPal's fee ({aud(net)}) exactly"])
    if reserve + held < 0:
        raise ContractProblem([f"{where}: held_back_change {aud(held)} would release more than the reserve holds ({aud(reserve)})"])
    lines = [
        f"The buyer pays {aud(amount)} into the business account.",
        f"PayPal keeps {aud(fee)}: {rate * 100:.1f}% of {aud(amount)} is {aud(percentage)}, plus {aud(fixed)} fixed. "
        f"The business account receives {aud(net)}.",
    ]
    lines += [f"{name} is paid {aud(value)}{_share(value, net, 'the business received')}." for name, value in payouts.items()]
    if not payouts:
        lines.append("Nobody is paid out from this sale.")
    if held > 0:
        lines.append(f"{aud(held)} is held back in the reserve (reserve is now {aud(reserve + held)}).")
    elif held < 0:
        lines.append(f"{aud(-held)} is released from the reserve (reserve is now {aud(reserve + held)}).")
    else:
        lines.append(f"Nothing is held back (reserve stays at {aud(reserve)}).")
    lines.append(f"Check: payouts {aud(sum(payouts.values(), ZERO))} + held back {aud(held)} = {aud(net)} received.")
    return {"kind": "sale", "heading": f"Sale: buyer pays {aud(amount)}", "lines": lines, "notes": notes,
            "values": {"amounts": {name: str(value) for name, value in payouts.items()}, "other": str(held)},
            "_state": new_state, "_reserve": reserve + held,
            "_totals": {"paid": amount, "fee": fee, "refunded": ZERO, "payouts": payouts, "funding": {}}}


def _refund_step(code: str, parties: set[str], sale_id: str, amount: Decimal, state: dict[str, Any],
                 reserve: Decimal, rate: Decimal, where: str) -> dict[str, Any]:
    returned = percentage_fee(amount, rate)
    cost = amount - returned
    result = _run(code, "calculate_refund", {"sale_id": sale_id, "refund_amount": amount, "fee_returned": returned, "state": state}, where)
    new_state, notes = _clean_extras(result, where)
    funding = _clean_split(result.get("funding"), "funding", parties, where)
    from_reserve = _clean_money(result.get("from_holdback"), "from_holdback", where)
    if sum(funding.values(), ZERO) + from_reserve != cost:
        raise ContractProblem([
            f"{where}: funding ({aud(sum(funding.values(), ZERO))}) plus from_holdback ({aud(from_reserve)}) must equal "
            f"the refund's cost to the business ({aud(cost)}) exactly"])
    if from_reserve > reserve:
        raise ContractProblem([f"{where}: from_holdback {aud(from_reserve)} is more than the reserve holds ({aud(reserve)})"])
    lines = [
        f"The buyer is refunded {aud(amount)} from the business account.",
        f"PayPal returns {aud(returned)} of its fee ({percent(rate)} of {aud(amount)}); the fixed fee is not returned. "
        f"The refund costs the business {aud(cost)}.",
    ]
    if from_reserve > 0:
        lines.append(f"{aud(from_reserve)} is taken from the reserve (reserve is now {aud(reserve - from_reserve)}).")
    else:
        lines.append(f"Nothing is taken from the reserve (reserve stays at {aud(reserve)}).")
    lines += [f"{name} must transfer {aud(value)} into the business account{_share(value, cost, 'the refund costs the business')}." for name, value in funding.items()]
    if not funding:
        lines.append("No partner has to transfer money in.")
    lines.append(f"Check: transfers {aud(sum(funding.values(), ZERO))} + reserve {aud(from_reserve)} = {aud(cost)} cost.")
    return {"kind": "refund", "heading": f"Refund: buyer gets back {aud(amount)}", "lines": lines, "notes": notes,
            "values": {"amounts": {name: str(value) for name, value in funding.items()}, "other": str(from_reserve)},
            "_state": new_state, "_reserve": reserve - from_reserve,
            "_totals": {"paid": ZERO, "fee": -returned, "refunded": amount, "payouts": {}, "funding": funding}}


def percent(rate: Decimal) -> str:
    return f"{rate * 100:.1f}%"


def _add(target: dict[str, Decimal], extra: dict[str, Decimal]) -> None:
    for name, value in extra.items():
        target[name] = target.get(name, ZERO) + value


def simulate_scenario(code: str, party_names: list[str], scenario: dict[str, Any], index: int,
                      rate: Decimal = DEFAULT_FEE_RATE, fixed: Decimal = DEFAULT_FIXED_FEE,
                      label: str = "Scenario") -> dict[str, Any]:
    parties = set(party_names)
    state: dict[str, Any] = {}
    reserve = ZERO
    sales: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    totals = {"paid": ZERO, "fee": ZERO, "refunded": ZERO, "payouts": {}, "funding": {}}
    for number, event in enumerate(scenario["events"], start=1):
        where = f"{label} {index}, event {number}"
        if event["type"] == "sale":
            sale = {"id": f"S{len(sales) + 1}", "amount": Decimal(str(event["amount"])), "refunded": ZERO}
            sales.append(sale)
            step = _sale_step(code, parties, sale["id"], sale["amount"], state, reserve, rate, fixed, where)
            step["heading"] = f"Event {number}: sale {len(sales)}. Buyer pays {aud(sale['amount'])}"
        else:
            sale = sales[event["sale_number"] - 1]
            left = sale["amount"] - sale["refunded"]
            wanted = left if str(event["amount"]).strip().lower() == "full" else Decimal(str(event["amount"]))
            if wanted <= 0 or wanted > left:
                raise ContractProblem([f"{where}: cannot refund {aud(wanted)}; only {aud(left)} of sale {event['sale_number']} is left to refund"])
            sale["refunded"] += wanted
            step = _refund_step(code, parties, sale["id"], wanted, state, reserve, rate, where)
            step["heading"] = f"Event {number}: refund of sale {event['sale_number']}. Buyer gets back {aud(wanted)}"
        state, reserve = step.pop("_state"), step.pop("_reserve")
        piece = step.pop("_totals")
        for key in ("paid", "fee", "refunded"):
            totals[key] += piece[key]
        _add(totals["payouts"], piece["payouts"])
        _add(totals["funding"], piece["funding"])
        step["reserve_after"] = str(reserve)
        steps.append(step)
    return {
        "title": scenario["title"], "steps": steps,
        "totals": {"buyers_paid": str(totals["paid"]), "paypal_fees_kept": str(totals["fee"]),
                   "refunded": str(totals["refunded"]),
                   "paid_out": {name: str(value) for name, value in totals["payouts"].items()},
                   "transferred_in": {name: str(value) for name, value in totals["funding"].items()},
                   "reserve_at_end": str(reserve)},
    }


TEST_COUNT = 3


def parse_tests(data: Any, party_names: list[str]) -> list[dict[str, Any]]:
    """Validate Claude's three internal test cases (events plus the results it expects)."""
    problems: list[str] = []
    if not isinstance(data, list) or len(data) != TEST_COUNT:
        raise ContractProblem([f"there must be exactly {TEST_COUNT} tests, got {len(data) if isinstance(data, list) else 0}"])
    tests: list[dict[str, Any]] = []
    for index, test in enumerate(data, start=1):
        label = f"test {index}"
        raw_events = test.get("events") if isinstance(test, dict) else None
        events = _normalise_events(raw_events)
        problems += _check_events(label, events)
        expected: list[dict[str, Any]] = []
        for number, raw in enumerate(raw_events if isinstance(raw_events, list) else [], start=1):
            where = f"{label}, event {number}"
            amounts: dict[str, Decimal] = {}
            for item in raw.get("expected_amounts", []) if isinstance(raw, dict) else []:
                party, value = item.get("party"), _amount(item.get("amount"))
                if party not in party_names or value is None or value < 0:
                    problems.append(f"{where}: expected_amounts needs a known party and a non-negative amount in whole cents")
                else:
                    amounts[party] = value
            other = _amount(raw.get("expected_held_back")) if isinstance(raw, dict) else None
            if other is None:
                problems.append(f"{where}: expected_held_back must be an amount in whole cents (use 0.00 if none)")
            expected.append({"amounts": amounts, "other": other if other is not None else ZERO})
        tests.append({"title": str(test.get("title") or f"Test {index}")[:80] if isinstance(test, dict) else f"Test {index}",
                      "events": events, "expected": expected})
    if problems:
        raise ContractProblem(problems)
    return tests


def run_tests(code: str, party_names: list[str], tests: list[dict[str, Any]], rate: Decimal = DEFAULT_FEE_RATE,
              fixed: Decimal = DEFAULT_FIXED_FEE) -> list[str]:
    """Run Claude's tests on its own code. Returns the titles of the tests; raises ContractProblem listing every mismatch."""
    problems: list[str] = []
    for index, test in enumerate(tests, start=1):
        try:
            result = simulate_scenario(code, party_names, test, index, rate, fixed, label="Claude's test")
        except ContractProblem as problem:
            problems += problem.problems
            continue
        for number, (event, step, expected) in enumerate(zip(test["events"], result["steps"], test["expected"]), start=1):
            actual = {name: Decimal(value) for name, value in step["values"]["amounts"].items()}
            for party in party_names:
                want, got = expected["amounts"].get(party, ZERO), actual.get(party, ZERO)
                if want != got:
                    problems.append(f"Test {index} ('{test['title']}'), event {number} ({event['type']} {event['amount']}): "
                                    f"expected {party} to be {aud(want)} but the code gave {aud(got)}")
            if Decimal(step["values"]["other"]) != expected["other"]:
                problems.append(f"Test {index} ('{test['title']}'), event {number} ({event['type']} {event['amount']}): expected "
                                f"held back / taken from reserve {aud(expected['other'])} but the code gave {aud(Decimal(step['values']['other']))}")
    if problems:
        raise ContractProblem(problems)
    return [test["title"] for test in tests]


def _sale(amount: str) -> dict[str, Any]:
    return {"type": "sale", "amount": amount, "sale_number": 0}


def _refund(amount: str, sale_number: int) -> dict[str, Any]:
    return {"type": "refund", "amount": amount, "sale_number": sale_number}


# Fixed, built-in checks that every contract must survive. They are not shown to the user: they only
# catch broken arithmetic (odd cents, tiny sales, partial refunds, state carried between calls).
SELF_TEST_SCENARIOS: list[dict[str, Any]] = [
    {"title": "small and awkward amounts", "events": [
        _sale("1.00"), _sale("33.33"), _sale("999.99"), _refund("0.01", 1), _refund("full", 2), _refund("100.00", 3)]},
    {"title": "many sales then refunds", "events": [
        _sale("100.00"), _sale("250.00"), _sale("17.17"), _sale("1000.00"), _sale("5.00"),
        _refund("full", 2), _refund("40.00", 4), _refund("full", 5)]},
]


def self_test(code: str, party_names: list[str], rate: Decimal = DEFAULT_FEE_RATE,
              fixed: Decimal = DEFAULT_FIXED_FEE) -> None:
    """Run the built-in checks. Raises ContractProblem (written for Claude to fix) on the first broken rule."""
    for index, scenario in enumerate(SELF_TEST_SCENARIOS, start=1):
        simulate_scenario(code, party_names, scenario, index, rate, fixed, label="Built-in check")


def simulate_slots(code: str, party_names: list[str], scenarios: list[dict[str, Any] | None],
                   rate: Decimal = DEFAULT_FEE_RATE, fixed: Decimal = DEFAULT_FIXED_FEE) -> list[dict[str, Any] | None]:
    """Run each saved user scenario against `code`. A scenario the contract cannot handle becomes {"error": ...}."""
    results: list[dict[str, Any] | None] = []
    for slot, scenario in enumerate(scenarios):
        if scenario is None:
            results.append(None)
            continue
        try:
            results.append(simulate_scenario(code, party_names, scenario, slot + 1, rate, fixed))
        except ContractProblem as problem:
            results.append({"error": " ".join(problem.problems)})
    return results
