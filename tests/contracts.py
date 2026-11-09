"""A small, correct reference contract used by several tests."""
import copy
from typing import Any

REFERENCE_CODE = '''
def calculate_payout(sale_id, sale_amount, paypal_fee, state):
    net = sale_amount - paypal_fee
    owed = state.get("owed", Decimal("200.00"))
    to_debt = min(net, owed)
    rest = net - to_debt
    alice_share = (rest * Decimal("0.60")).quantize(CENT, rounding=ROUND_HALF_UP)
    payouts = {"Alice": to_debt + alice_share, "Bob": rest - alice_share}
    notes = []
    if to_debt > 0:
        notes.append("Alice recoups her investment first: " + str(to_debt) + " of this sale.")
    return {"payouts": payouts, "held_back_change": Decimal("0.00"), "state": {"owed": owed - to_debt}, "notes": notes}


def calculate_refund(sale_id, refund_amount, fee_returned, state):
    cost = refund_amount - fee_returned
    alice = (cost * Decimal("0.50")).quantize(CENT, rounding=ROUND_HALF_UP)
    return {"funding": {"Alice": alice, "Bob": cost - alice}, "from_holdback": Decimal("0.00"),
            "state": state, "notes": ["Refunds are shared equally."]}
'''

PARTY_NAMES = ["Alice", "Bob"]


def sale(amount: str) -> dict[str, Any]:
    return {"type": "sale", "amount": amount, "sale_number": 0}


def refund(amount: str, sale_number: int) -> dict[str, Any]:
    return {"type": "refund", "amount": amount, "sale_number": sale_number}


def expect(held: str = "0.00", **amounts: str) -> dict[str, Any]:
    """Expected results for one event: what each party gets/transfers and the reserve movement."""
    return {"expected_amounts": [{"party": party, "amount": amount} for party, amount in amounts.items()],
            "expected_held_back": held}


def with_expected(event: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return {**event, **expect(**kwargs)}


# Claude's own internal tests for REFERENCE_CODE, worked out by hand from the contract (fees: 2.4% + 0.30 per sale).
REFERENCE_TESTS: list[dict[str, Any]] = [
    {"title": "Recoupment over two sales", "events": [
        with_expected(sale("100.00"), Alice="97.30", Bob="0.00"),      # fee 2.70, net 97.30, all to Alice
        with_expected(sale("150.00"), Alice="128.74", Bob="17.36")]},  # net 146.10: 102.70 recoups, 43.40 splits 26.04/17.36
    {"title": "Full refund after a sale", "events": [
        with_expected(sale("80.00"), Alice="77.78", Bob="0.00"),       # fee 2.22
        with_expected(refund("full", 1), Alice="39.04", Bob="39.04")]},  # fee back 1.92, cost 78.08
    {"title": "Partial refund then another sale", "events": [
        with_expected(sale("300.00"), Alice="255.50", Bob="37.00"),    # net 292.50: 200 recoups, 92.50 splits 55.50/37.00
        with_expected(refund("100.00", 1), Alice="48.80", Bob="48.80"),  # fee back 2.40, cost 97.60
        with_expected(sale("50.00"), Alice="29.10", Bob="19.40")]},    # net 48.50 split 60/40
]


# Scenarios as a user would type them in.
SCENARIOS: list[dict[str, Any]] = [
    {"title": "Investment is repaid in two sales", "events": [sale("100.00"), sale("150.00")]},
    {"title": "A refund after a sale", "events": [sale("80.00"), refund("full", 1)]},
    {"title": "A partial refund", "events": [sale("300.00"), refund("100.00", 1), sale("50.00")]},
]


def reference_submission() -> dict[str, Any]:
    """What Claude sends when it is ready: summary, parties and code (the user writes the scenarios)."""
    return {
        "status": "ready",
        "message": "I have everything I need. Open the Review tab to add emails and try your own scenarios.",
        "summary": ["Alice recoups her A$200 investment first.", "After that, sales split 60/40 (Alice/Bob).",
                    "Refunds are funded equally by both partners."],
        "parties": [{"name": "Alice", "description": "Invested A$200"}, {"name": "Bob", "description": "Developer"}],
        "code": REFERENCE_CODE,
        "tests": copy.deepcopy(REFERENCE_TESTS),
    }
