import hashlib
import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from clausepay.contract_runner import (
    CodeChangedError, ContractFailedError, ContractRunner, FeeMismatchError, InvalidTransactionError,
    PendingTransactionError, RunnerError,
)
from clausepay.contract_store import ContractStore
from tests.contracts import REFERENCE_CODE, REFERENCE_TESTS, reference_submission

EMAILS = {"Alice": "alice@example.com", "Bob": "bob@example.com"}

# The code Claude generated for the demo contract: 10% reserve, A$500 recoupment, 60/40, refunds from the reserve first.
RESERVE_CODE = '''
def calculate_payout(sale_id, sale_amount, paypal_fee, state):
    net = sale_amount - paypal_fee
    reserve_in = (net * Decimal("0.10")).quantize(CENT, rounding=ROUND_HALF_UP)
    rest = net - reserve_in
    recouped = state.get("recouped", Decimal("0.00"))
    reserve = state.get("reserve", Decimal("0.00"))
    notes = []
    remaining = max(Decimal("500.00") - recouped, Decimal("0.00"))
    recoup_part = min(rest, remaining)
    after = rest - recoup_part
    if recoup_part > Decimal("0.00"):
        notes.append("Alice recoups her investment first: " + str(recoup_part))
    bob = (after * Decimal("0.40")).quantize(CENT, rounding=ROUND_HALF_UP)
    alice = recoup_part + (after - bob)
    notes.append("Held back for refunds: " + str(reserve_in))
    new_state = dict(state)
    new_state["recouped"] = recouped + recoup_part
    new_state["reserve"] = reserve + reserve_in
    return {"payouts": {"Alice": alice, "Bob": bob}, "held_back_change": reserve_in, "payout_fees_covered": Decimal("0.00"),
            "state": new_state, "notes": notes}


def calculate_refund(sale_id, refund_amount, fee_returned, state):
    cost = refund_amount - fee_returned
    reserve = state.get("reserve", Decimal("0.00"))
    notes = []
    from_hold = min(reserve, cost)
    missing = cost - from_hold
    bob = (missing / 2).quantize(CENT, rounding=ROUND_DOWN)
    alice = missing - bob
    notes.append("Taken from the reserve: " + str(from_hold))
    if missing > Decimal("0.00"):
        notes.append("Reserve not enough; Alice and Bob fund the missing " + str(missing))
    new_state = dict(state)
    new_state["reserve"] = reserve - from_hold
    return {"funding": {"Alice": alice, "Bob": bob}, "from_holdback": from_hold, "state": new_state, "notes": notes}
'''


def d(value: str) -> Decimal:
    return Decimal(value)


def freeze_files(folder: Path, code: str = REFERENCE_CODE, name: str = "abc123-v1") -> Path:
    """Write a frozen contract the way ContractStore.freeze does, and return the code path."""
    code_path = folder / f"{name}.py"
    code_path.write_text(code)
    meta = {"contract_id": "abc123", "version": "1", "sha256": hashlib.sha256(code.encode()).hexdigest(),
            "file": str(code_path), "parties": [{"name": "Alice", "description": ""}, {"name": "Bob", "description": ""}],
            "emails": EMAILS}
    code_path.with_suffix(".json").write_text(json.dumps(meta))
    return code_path


@pytest.fixture
def runner(tmp_path: Path) -> ContractRunner:
    return ContractRunner(freeze_files(tmp_path))


@pytest.fixture
def reserve_runner(tmp_path: Path) -> ContractRunner:
    return ContractRunner(freeze_files(tmp_path, RESERVE_CODE, "reserve-v1"))


# ---------- the memory: state is saved after each run and read at the start of the next ----------

def test_a_sale_is_run_and_recorded_as_pending(runner: ContractRunner) -> None:
    sale = runner.process_sale("S1", d("100.00"), d("2.70"))
    assert (sale.kind, sale.status, sale.duplicate) == ("sale", "pending", False)
    assert sale.amounts == {"Alice": d("97.30"), "Bob": d("0.00")} and sale.state_after == {"owed": d("102.70")}
    assert any("PayPal reported keeping A$2.70" in line for line in sale.lines)
    assert runner.journal_path.exists()


def test_state_carries_from_one_sale_to_the_next_once_committed(runner: ContractRunner) -> None:
    runner.commit(runner.process_sale("S1", d("100.00"), d("2.70")).entry_id)
    second = runner.process_sale("S2", d("150.00"), d("3.90"))
    assert second.amounts == {"Alice": d("128.74"), "Bob": d("17.36")}  # 102.70 recouped, then 60/40 of 43.40
    assert second.state_after == {"owed": d("0.00")}


def test_the_state_survives_a_restart_because_it_is_read_from_the_file(tmp_path: Path) -> None:
    path = freeze_files(tmp_path)
    first = ContractRunner(path)
    first.commit(first.process_sale("S1", d("100.00"), d("2.70")).entry_id)
    again = ContractRunner(path)  # a brand-new runner, as after a restart
    assert again.state() == {"owed": d("102.70")}
    assert again.process_sale("S2", d("150.00"), d("3.90")).amounts["Alice"] == d("128.74")


def test_amounts_stay_exact_in_the_file(runner: ContractRunner) -> None:
    runner.commit(runner.process_sale("S1", d("33.33"), d("1.10")).entry_id)
    text = runner.journal_path.read_text()
    assert "__decimal__" in text and "0.1" not in text.replace("0.10", "")  # no float artefacts like 0.1000000001


def test_a_pending_transaction_blocks_the_next_one(runner: ContractRunner) -> None:
    first = runner.process_sale("S1", d("100.00"), d("2.70"))
    with pytest.raises(PendingTransactionError, match="sale:S1"):
        runner.process_sale("S2", d("50.00"), d("1.50"))
    runner.commit(first.entry_id)
    assert runner.process_sale("S2", d("50.00"), d("1.50")).status == "pending"


def test_a_failed_payout_leaves_the_state_where_it_was(runner: ContractRunner) -> None:
    first = runner.process_sale("S1", d("100.00"), d("2.70"))
    runner.fail(first.entry_id, "PayPal could not pay Bob")
    assert runner.state() == {} and runner.reserve() == d("0.00")
    retry = runner.process_sale("S1", d("100.00"), d("2.70"))  # the same sale may be tried again
    assert retry.duplicate is False and retry.amounts["Alice"] == d("97.30")
    assert [e["status"] for e in runner.entries()] == ["failed", "pending"]
    assert runner.entries()[0]["failure"] == "PayPal could not pay Bob" and runner.entries()[1]["attempt"] == 2


def test_only_a_pending_entry_can_be_finished(runner: ContractRunner) -> None:
    sale = runner.process_sale("S1", d("100.00"), d("2.70"))
    runner.commit(sale.entry_id)
    with pytest.raises(InvalidTransactionError):
        runner.commit(sale.entry_id)
    with pytest.raises(InvalidTransactionError):
        runner.fail("sale:nope", "x")


# ---------- the same sale twice (PayPal retries webhooks) ----------

def test_the_same_sale_twice_returns_the_stored_result_and_does_not_run_again(runner: ContractRunner) -> None:
    first = runner.process_sale("S1", d("100.00"), d("2.70"))
    again = runner.process_sale("S1", d("100.00"), d("2.70"))
    assert again.duplicate is True and again.amounts == first.amounts and len(runner.entries()) == 1
    runner.commit(first.entry_id)
    assert runner.process_sale("S1", d("100.00"), d("2.70")).duplicate is True  # also after it was committed
    assert runner.state() == {"owed": d("102.70")}  # recoupment was counted once


def test_many_simultaneous_copies_of_one_sale_are_counted_once(tmp_path: Path) -> None:
    path = freeze_files(tmp_path)
    results: list[bool] = []

    def attempt() -> None:
        results.append(ContractRunner(path).process_sale("S1", d("100.00"), d("2.70")).duplicate)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(False) == 1 and results.count(True) == 7
    assert len(ContractRunner(path).entries()) == 1


# ---------- the code must not have changed ----------

def test_changed_code_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    path = freeze_files(tmp_path)
    runner = ContractRunner(path)
    path.write_text(REFERENCE_CODE.replace('Decimal("0.60")', 'Decimal("0.90")'))
    with pytest.raises(CodeChangedError, match="does not match the hash"):
        runner.process_sale("S1", d("100.00"), d("2.70"))
    assert not runner.journal_path.exists()
    for call in (runner.state, runner.reserve, runner.entries):
        with pytest.raises(CodeChangedError):
            call()


def test_a_missing_code_file_is_refused(tmp_path: Path) -> None:
    path = freeze_files(tmp_path)
    runner = ContractRunner(path)
    path.unlink()
    with pytest.raises(CodeChangedError, match="missing"):
        runner.process_sale("S1", d("100.00"), d("2.70"))


def test_changing_the_hash_in_the_details_file_too_is_still_caught_by_the_journal(tmp_path: Path) -> None:
    path = freeze_files(tmp_path)
    runner = ContractRunner(path)
    runner.commit(runner.process_sale("S1", d("100.00"), d("2.70")).entry_id)
    new_code = REFERENCE_CODE.replace('Decimal("0.60")', 'Decimal("0.90")')
    path.write_text(new_code)
    meta = json.loads(path.with_suffix(".json").read_text())
    meta["sha256"] = hashlib.sha256(new_code.encode()).hexdigest()
    path.with_suffix(".json").write_text(json.dumps(meta))
    with pytest.raises(CodeChangedError, match="journal belongs to different code"):
        ContractRunner(path).process_sale("S2", d("100.00"), d("2.70"))


def test_an_unreadable_journal_stops_everything_instead_of_resetting_the_state(runner: ContractRunner) -> None:
    runner.commit(runner.process_sale("S1", d("100.00"), d("2.70")).entry_id)
    runner.journal_path.write_text("{not json")
    with pytest.raises(RunnerError, match="could not be read"):
        runner.process_sale("S2", d("50.00"), d("1.50"))
    assert runner.journal_path.read_text() == "{not json"  # left alone for a human to look at


def test_a_missing_details_file_is_explained(tmp_path: Path) -> None:
    path = freeze_files(tmp_path)
    path.with_suffix(".json").unlink()
    with pytest.raises(RunnerError, match="could not be read"):
        ContractRunner(path)


# ---------- what is accepted ----------

def test_a_fee_that_differs_from_the_expected_fee_pauses_the_transaction(runner: ContractRunner) -> None:
    with pytest.raises(FeeMismatchError, match="2.70 was expected"):
        runner.process_sale("S1", d("100.00"), d("2.71"))
    assert not runner.journal_path.exists()


@pytest.mark.parametrize("amount, fee", [
    (d("-5.00"), d("0.30")), (d("0.00"), d("0.00")), (d("10.00"), d("10.00")), (d("10.00"), d("-1.00")),
    (d("10.005"), d("0.54")),
])
def test_unreasonable_amounts_are_refused(runner: ContractRunner, amount: Decimal, fee: Decimal) -> None:
    with pytest.raises(InvalidTransactionError):
        runner.process_sale("S1", amount, fee)


def test_floats_are_refused(runner: ContractRunner) -> None:
    with pytest.raises(InvalidTransactionError, match="whole cents"):
        runner.process_sale("S1", 100.0, d("2.70"))  # type: ignore[arg-type]


def test_a_contract_that_breaks_a_rule_is_refused_and_nothing_is_recorded(tmp_path: Path) -> None:
    code = REFERENCE_CODE.replace('"Bob": rest - alice_share', '"Bob": rest')
    runner = ContractRunner(freeze_files(tmp_path, code))
    with pytest.raises(ContractFailedError, match="must equal the amount received"):
        runner.process_sale("S1", d("300.00"), d("7.50"))
    assert not runner.journal_path.exists()


# ---------- refunds and the reserve ----------

def test_the_reserve_is_tracked_by_the_runner_itself(reserve_runner: ContractRunner) -> None:
    sale = reserve_runner.process_sale("S1", d("800.00"), d("19.50"))
    assert sale.amounts == {"Alice": d("621.47"), "Bob": d("80.98")}  # the numbers the user asked about
    assert sale.reserve_change == d("78.05") and reserve_runner.reserve() == d("78.05")
    assert sale.state_after == {"recouped": d("500.00"), "reserve": d("78.05")}


def test_a_small_refund_comes_entirely_from_the_reserve(reserve_runner: ContractRunner) -> None:
    reserve_runner.commit(reserve_runner.process_sale("S1", d("800.00"), d("19.50")).entry_id)
    refund = reserve_runner.process_refund("S1", "R1", d("20.00"), d("0.48"))
    assert refund.reserve_change == d("-19.52") and refund.by_email(EMAILS) == {}  # nobody has to transfer anything
    assert refund.reserve_after == d("58.53")
    assert any("PayPal reported returning A$0.48" in line for line in refund.lines)


def test_a_big_refund_empties_the_reserve_and_partners_fund_the_rest(reserve_runner: ContractRunner) -> None:
    reserve_runner.commit(reserve_runner.process_sale("S1", d("800.00"), d("19.50")).entry_id)
    refund = reserve_runner.process_refund("S1", "R1", d("400.00"), d("9.60"))
    # cost 390.40: 78.05 from the reserve, the missing 312.35 split between the partners
    assert refund.reserve_change == d("-78.05") and refund.reserve_after == d("0.00")
    assert refund.by_email(EMAILS) == {"alice@example.com": d("156.18"), "bob@example.com": d("156.17")}
    assert sum(refund.amounts.values()) + (-refund.reserve_change) == d("390.40")


def test_refunds_need_a_known_sale_and_cannot_exceed_it(reserve_runner: ContractRunner) -> None:
    with pytest.raises(InvalidTransactionError, match="not in this contract's journal"):
        reserve_runner.process_refund("S9", "R1", d("5.00"), d("0.12"))
    reserve_runner.commit(reserve_runner.process_sale("S1", d("100.00"), d("2.70")).entry_id)
    reserve_runner.commit(reserve_runner.process_refund("S1", "R1", d("60.00"), d("1.44")).entry_id)
    with pytest.raises(InvalidTransactionError, match="Only 40.00 of sale S1"):
        reserve_runner.process_refund("S1", "R2", d("50.00"), d("1.20"))
    assert reserve_runner.process_refund("S1", "R2", d("40.00"), d("0.96")).status == "pending"


def test_a_refund_with_the_wrong_returned_fee_is_paused(reserve_runner: ContractRunner) -> None:
    reserve_runner.commit(reserve_runner.process_sale("S1", d("100.00"), d("2.70")).entry_id)
    with pytest.raises(FeeMismatchError):
        reserve_runner.process_refund("S1", "R1", d("20.00"), d("0.00"))


def test_the_same_refund_twice_is_counted_once(reserve_runner: ContractRunner) -> None:
    reserve_runner.commit(reserve_runner.process_sale("S1", d("100.00"), d("2.70")).entry_id)
    first = reserve_runner.process_refund("S1", "R1", d("20.00"), d("0.48"))
    again = reserve_runner.process_refund("S1", "R1", d("20.00"), d("0.48"))
    assert again.duplicate is True and again.amounts == first.amounts
    assert reserve_runner.reserve() == first.reserve_after


def test_two_partial_refunds_of_one_sale_are_different_refunds(reserve_runner: ContractRunner) -> None:
    reserve_runner.commit(reserve_runner.process_sale("S1", d("100.00"), d("2.70")).entry_id)
    reserve_runner.commit(reserve_runner.process_refund("S1", "R1", d("10.00"), d("0.24")).entry_id)
    second = reserve_runner.process_refund("S1", "R2", d("10.00"), d("0.24"))
    assert second.duplicate is False and len(reserve_runner.entries()) == 3


# ---------- turning names into emails ----------

def test_amounts_are_turned_into_emails_for_paying_out(runner: ContractRunner) -> None:
    sale = runner.process_sale("S1", d("300.00"), d("7.50"))
    # 292.50 received: 200 recoups, 92.50 splits 55.50 / 37.00
    assert sale.by_email(runner.emails) == {"alice@example.com": d("255.50"), "bob@example.com": d("37.00")}


def test_a_party_without_an_email_cannot_be_paid(runner: ContractRunner) -> None:
    sale = runner.process_sale("S1", d("300.00"), d("7.50"))
    with pytest.raises(RunnerError, match="No email is recorded for: Bob"):
        sale.by_email({"Alice": "alice@example.com"})


# ---------- from freezing to running ----------

def test_a_contract_frozen_by_the_app_runs_straight_away(tmp_path: Path) -> None:
    store = ContractStore(tmp_path)
    record = store.current()
    submission = reference_submission()
    submission["tests"] = REFERENCE_TESTS
    from clausepay.contract_chat import accept_submission
    accept_submission(record, submission)
    from clausepay.contract_sim import parse_scenario, simulate_scenario
    from tests.contracts import PARTY_NAMES, SCENARIOS
    for slot in range(3):
        scenario = parse_scenario(slot, SCENARIOS[slot])
        record.set_scenario(slot, scenario, simulate_scenario(record.code, PARTY_NAMES, scenario, slot + 1))
        record.approve(slot)
    record.set_emails(EMAILS)
    runner = ContractRunner(Path(store.freeze()["file"]))
    sale = runner.process_sale("S1", d("100.00"), d("2.70"))
    assert sale.by_email(runner.emails) == {"alice@example.com": d("97.30")}
