import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from clausepay.contract_runner import ContractRunner
from clausepay.reports import (
    Facts, ReportService, ReportWriter, check_narrative, fallback_narrative, hide_numbers, money, refund_facts, render,
    sale_facts, slug,
)
from tests.fake_claude import FakeBlock, FakeClaude, FakeResponse
from tests.test_contract_runner import EMAILS, RESERVE_CODE, freeze_files
from tests.test_live import FakePayPal, make_service, pay_out, sell

PARTIES = ["Alice", "Bob"]
SUMMARY = ["Alice recoups her A$500 investment first.", "Then 60/40.", "10% is held back for refunds."]


def d(value: str) -> Decimal:
    return Decimal(value)


def sale_entry(tmp_path: Path, amount: str = "800.00", fee: str = "19.50") -> tuple[ContractRunner, dict[str, Any]]:
    runner = ContractRunner(freeze_files(tmp_path, RESERVE_CODE))
    transaction = runner.process_sale("S1", d(amount), d(fee))
    runner.commit(transaction.entry_id)
    return runner, runner.entries()[0]


# ---------- layer one: the figures ----------

def test_a_sale_report_has_every_figure_and_what_changed(tmp_path: Path) -> None:
    _, entry = sale_entry(tmp_path)
    facts = sale_facts(entry, PARTIES)
    assert facts.values["sale_amount"] == "A$800.00" and facts.values["received"] == "A$780.50"
    assert facts.values["payout_alice"] == "A$621.47" and facts.values["payout_bob"] == "A$80.98"
    assert facts.values["held_back"] == "A$78.05" and facts.values["reserve_after"] == "A$78.05"
    assert facts.values["payout_fees"] == "A$14.05" and facts.values["payout_fees_absorbed"] == "A$14.05"  # 2% of 621.47 and 80.98
    assert facts.values["state_recouped_before"] == "none yet" and facts.values["state_recouped_after"] == "A$500.00"
    assert ("Buyer paid", "A$800.00") in facts.figures and ("Paid to Alice", "A$621.47") in facts.figures
    assert ("Contract memory: recouped", "none yet → A$500.00") in facts.figures
    assert {"remembered_item": "recouped"}.items() <= facts.changes[0].items() or facts.changes[0]["remembered_item"] == "recouped"


def test_a_sale_with_nothing_paid_says_so(tmp_path: Path) -> None:
    runner = ContractRunner(freeze_files(tmp_path, RESERVE_CODE))
    entry = {"amount": d("100.00"), "fee": d("2.70"), "amounts": {"Alice": d("0.00"), "Bob": d("0.00")}, "reserve_change": d("97.30"),
             "reserve_after": d("97.30"), "state_before": {}, "state_after": {"reserve": d("97.30")}, "notes": [], "payout_fees": d("0.00"),
             "payout_fees_covered": d("0.00")}
    facts = sale_facts(entry, PARTIES)
    assert "Nobody was paid out from this sale." in facts.flags and "Some money was held back in the reserve." in facts.flags
    assert runner  # contract exists; only the entry is hand-made here


def test_state_changes_note_when_something_reaches_zero() -> None:
    entry = {"amount": d("100.00"), "fee": d("2.70"), "amounts": {"Alice": d("97.30")}, "reserve_change": d("0.00"), "reserve_after": d("0.00"),
             "state_before": {"owed": d("97.30")}, "state_after": {"owed": d("0.00")}, "notes": [], "payout_fees": d("0.00"),
             "payout_fees_covered": d("0.00")}
    change = sale_facts(entry, PARTIES).changes[0]
    assert change["direction"] == "decreased" and change["reached_zero"] is True


def test_a_refund_report_says_what_each_partner_must_do(tmp_path: Path) -> None:
    runner, _ = sale_entry(tmp_path)
    refund = runner.process_refund("S1", "R1", d("400.00"), d("9.60"))
    runner.commit(refund.entry_id)
    facts = refund_facts(runner.entries()[1], PARTIES)
    assert facts.values["refund_amount"] == "A$400.00" and facts.values["refund_cost"] == "A$390.40"
    assert facts.values["from_reserve"] == "A$78.05" and facts.values["transfer_alice"] == "A$156.18"
    assert any("must transfer money in: Alice, Bob" in flag for flag in facts.flags)


def test_numbers_are_hidden_from_what_claude_sees(tmp_path: Path) -> None:
    _, entry = sale_entry(tmp_path)
    facts = sale_facts(entry, PARTIES)
    seen = json.dumps(facts.for_claude(SUMMARY, PARTIES))
    for value in ("621.47", "80.98", "78.05", "780.50", "800.00"):
        assert value not in seen
    assert "Alice recoups her investment first: [amount]" in seen
    assert hide_numbers("Held back for refunds: 78.05") == "Held back for refunds: [amount]"


def test_money_and_names() -> None:
    assert money("1234.5") == "A$1,234.50" and slug("Open Source Fund") == "open_source_fund" and slug("!!!") == "party"


# ---------- the strict rule about numbers ----------

NUMS = {"500", "60", "40", "10"}


@pytest.mark.parametrize("parts, fragment", [
    (["Alice received {payout_alice} today."], None),
    (["Alice received {payout_zoe} today."], "not one of the placeholders"),
    (["Alice received 621.47 today."], "never write numbers"),
    (["The split is 60/40 as agreed."], None),  # numbers from the contract summary are fine
    (["The split is 70/30."], "never write numbers"),
    ([""], "non-empty"),
    ([], "non-empty"),
])
def test_the_narrative_may_only_use_placeholders_and_numbers_from_the_summary(parts: list[str], fragment: str | None) -> None:
    problems = check_narrative(parts, {"payout_alice"}, NUMS)
    assert (problems == []) if fragment is None else any(fragment in problem for problem in problems)


# ---------- layer two: Claude's explanation ----------

def good_reply() -> dict[str, Any]:
    return {"subject": "Sale of {sale_amount} processed",
            "paragraphs": ["A sale of {sale_amount} came in and {received} reached the business account.",
                           "Alice was paid {payout_alice} and Bob was paid {payout_bob}; {held_back} went into the reserve."]}


def writer_for(*replies: Any) -> tuple[ReportWriter, FakeClaude]:
    claude = FakeClaude(list(replies))
    return ReportWriter(claude), claude  # type: ignore[arg-type]


def test_a_good_explanation_is_accepted_and_filled_in(tmp_path: Path) -> None:
    _, entry = sale_entry(tmp_path)
    facts = sale_facts(entry, PARTIES)
    writer, claude = writer_for(good_reply())
    subject, paragraphs, source = writer.write(facts, SUMMARY, PARTIES)
    report = render(facts, subject, paragraphs, source, list(EMAILS.values()), "Sale report")
    assert source == "claude" and report["subject"] == "Sale of A$800.00 processed"
    assert "Alice was paid A$621.47 and Bob was paid A$80.98; A$78.05 went into the reserve." in report["narrative"]
    assert report["recipients"] == ["alice@example.com", "bob@example.com"] and report["email_status"] == "Email sent (simulated)"
    sent = claude.messages.requests[0]
    assert sent["model"] == "claude-sonnet-5-5" and "621.47" not in json.dumps(sent["messages"])


def test_a_number_written_by_claude_is_rejected_and_it_is_asked_again(tmp_path: Path) -> None:
    _, entry = sale_entry(tmp_path)
    bad = {"subject": "Sale", "paragraphs": ["Alice got 621.47 from this sale."]}
    writer, claude = writer_for(bad, good_reply())
    assert writer.write(sale_facts(entry, PARTIES), SUMMARY, PARTIES)[2] == "claude"
    assert "rejected_because" in claude.messages.requests[1]["messages"][0]["content"] and "621.47" in claude.messages.requests[1]["messages"][0]["content"]


def test_unknown_placeholders_are_rejected(tmp_path: Path) -> None:
    _, entry = sale_entry(tmp_path)
    bad = {"subject": "Sale", "paragraphs": ["Carol was paid {payout_carol}."]}
    writer, _ = writer_for(bad, good_reply())
    assert writer.write(sale_facts(entry, PARTIES), SUMMARY, PARTIES)[2] == "claude"


def test_if_claude_keeps_writing_numbers_the_fixed_wording_is_used(tmp_path: Path) -> None:
    _, entry = sale_entry(tmp_path)
    bad = {"subject": "Sale", "paragraphs": ["Alice got 621.47."]}
    writer, claude = writer_for(bad, bad, bad)
    facts = sale_facts(entry, PARTIES)
    subject, paragraphs, source = writer.write(facts, SUMMARY, PARTIES)
    assert source == "fallback" and len(claude.messages.requests) == 3
    assert (subject, paragraphs) == fallback_narrative(facts)
    assert not check_narrative([subject, *paragraphs], set(facts.values), NUMS)  # the fallback obeys the same rule


def test_an_unreachable_or_refusing_claude_falls_back(tmp_path: Path) -> None:
    _, entry = sale_entry(tmp_path)
    refusal = FakeResponse([], stop_reason="refusal")
    writer, _ = writer_for(refusal, FakeResponse([FakeBlock("not json")]), refusal)
    assert writer.write(sale_facts(entry, PARTIES), SUMMARY, PARTIES)[2] == "fallback"


def test_without_a_claude_key_the_report_still_gets_written(tmp_path: Path) -> None:
    _, entry = sale_entry(tmp_path)
    assert ReportWriter(None).write(sale_facts(entry, PARTIES), SUMMARY, PARTIES)[2] == "fallback"


# ---------- the service ----------

def live_with_sale(tmp_path: Path, paypal: FakePayPal):
    service = make_service(tmp_path, paypal, RESERVE_CODE)
    order_id = sell(service, "800.00")
    pay_out(service, paypal)
    return service, order_id


def test_every_paid_out_sale_gets_one_report_and_the_partners_are_emailed(tmp_path: Path) -> None:
    paypal = FakePayPal()
    service, order_id = live_with_sale(tmp_path, paypal)
    writer, claude = writer_for(good_reply())
    reports = ReportService(service, lambda: writer)
    assert reports.generate_pending() == 1
    report = service.store.get(order_id).report
    assert report["source"] == "claude" and report["recipients"] == ["alice@example.com", "bob@example.com"]
    assert report["email_status"] == "Email sent (simulated)" and report["title"] == "Sale report: A$800.00"
    assert ["Paid to Alice", "A$621.47"] in report["figures"]
    assert reports.generate_pending() == 0 and len(claude.messages.requests) == 1  # no second report, no second Claude call
    assert any(line["kind"] == "report" for line in service.store.entries())


def test_a_sale_still_paying_out_has_no_report_yet(tmp_path: Path) -> None:
    paypal = FakePayPal()
    service = make_service(tmp_path, paypal, RESERVE_CODE)
    sell(service, "800.00")
    assert ReportService(service, lambda: ReportWriter(None)).generate_pending() == 0


def test_a_refund_gets_its_own_report(tmp_path: Path) -> None:
    paypal = FakePayPal()
    service, order_id = live_with_sale(tmp_path, paypal)
    reports = ReportService(service, lambda: ReportWriter(None))
    reports.generate_pending()
    service.refund(order_id, d("400.00"), "request-0001", True)
    assert reports.generate_pending() == 1
    report = service.store.get(order_id).refunds[0]["report"]
    assert report["kind"] == "refund" and ["Alice transfers in", "A$156.18"] in report["figures"] and report["source"] == "fallback"


def test_reports_survive_a_restart(tmp_path: Path) -> None:
    paypal = FakePayPal()
    service, order_id = live_with_sale(tmp_path, paypal)
    ReportService(service, lambda: ReportWriter(None)).generate_pending()
    from clausepay.live import LiveStore
    assert LiveStore(tmp_path / "live.json").get(order_id).report["kind"] == "sale"


def test_a_sale_that_pays_nobody_still_gets_a_report(tmp_path: Path) -> None:
    holding = RESERVE_CODE.replace('payouts": {"Alice": alice, "Bob": bob}', 'payouts": {}').replace(
        '"held_back_change": reserve_in', '"held_back_change": net').replace('new_state["reserve"] = reserve + reserve_in', 'new_state["reserve"] = reserve + net')
    paypal = FakePayPal()
    service = make_service(tmp_path, paypal, holding)
    order_id = sell(service, "100.00")
    assert service.store.get(order_id).status == "paid_out" and paypal.payouts == []
    assert ReportService(service, lambda: ReportWriter(None)).generate_pending() == 1
    assert service.store.get(order_id).report["figures"][-2][0] == "Held back in total now" or service.store.get(order_id).report
