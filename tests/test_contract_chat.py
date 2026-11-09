import copy
import json
from pathlib import Path

import pytest

from clausepay.contract_chat import MODEL, RESPONSE_SCHEMA, SYSTEM_PROMPT, AssistantError, ContractAssistant
from clausepay.contract_sim import parse_scenario, simulate_scenario
from clausepay.contract_store import APPROVALS_NEEDED, SLOTS, ContractRecord, ContractStore
from tests.contracts import PARTY_NAMES, REFERENCE_CODE, SCENARIOS, reference_submission, refund, sale, with_expected
from tests.fake_claude import FakeBlock, FakeClaude, FakeResponse, question


def drafted(replies: list | None = None) -> ContractRecord:
    record = ContractRecord()
    ContractAssistant(FakeClaude(replies or [reference_submission()])).respond(record, "contract")
    return record


def add_scenario(record: ContractRecord, slot: int) -> None:
    scenario = parse_scenario(slot, SCENARIOS[slot])
    record.set_scenario(slot, scenario, simulate_scenario(record.code, PARTY_NAMES, scenario, slot + 1))


def test_a_question_is_shown_and_remembered() -> None:
    claude = FakeClaude([question("Who bears PayPal's fee?")])
    record = ContractRecord()
    ContractAssistant(claude).respond(record, "A and B split 60/40.")
    assert [m["role"] for m in record.display] == ["user", "assistant"]
    assert record.display[1]["text"] == "Who bears PayPal's fee?"
    assert record.status == "chatting" and record.version == 0
    assert [m["role"] for m in record.api_messages] == ["user", "assistant"]


def test_request_uses_sonnet_five_five_structured_output_and_the_system_prompt() -> None:
    claude = FakeClaude([question("?")])
    ContractAssistant(claude).respond(ContractRecord(), "hello")
    sent = claude.messages.requests[0]
    assert sent["model"] == MODEL == "claude-sonnet-5-5"
    assert sent["system"] == SYSTEM_PROMPT
    assert sent["output_config"]["format"]["schema"] == RESPONSE_SCHEMA
    assert "tool_choice" not in sent  # forced tool use is rejected by this model
    assert "temperature" not in sent


def test_claude_is_asked_for_internal_tests_but_not_user_scenarios_or_emails() -> None:
    assert set(RESPONSE_SCHEMA["properties"]) == {"status", "message", "summary", "parties", "code", "tests"}
    party = RESPONSE_SCHEMA["properties"]["parties"]["items"]["properties"]
    assert set(party) == {"name", "description"}
    assert "The user writes their own scenarios later" in SYSTEM_PROMPT and "never ask for emails" in SYSTEM_PROMPT.lower()


def test_conversation_history_is_sent_back_each_turn() -> None:
    claude = FakeClaude([question("first?"), question("second?")])
    record, assistant = ContractRecord(), ContractAssistant(claude)
    assistant.respond(record, "contract text")
    assistant.respond(record, "my answer")
    roles = [m["role"] for m in claude.messages.requests[1]["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert claude.messages.requests[1]["messages"][2]["content"] == "my answer"


def test_a_ready_answer_creates_a_draft_with_empty_scenario_slots() -> None:
    record = drafted()
    assert record.status == "ready" and record.version == 1
    assert record.scenarios == [None] * SLOTS and record.simulations == [None] * SLOTS
    assert record.parties[0]["name"] == "Alice" and record.display[-1]["kind"] == "draft"
    assert record.internal_checks == ["Recoupment over two sales", "Full refund after a sale", "Partial refund then another sale"]


def test_failed_checks_go_back_to_claude_and_the_user_never_sees_it() -> None:
    bad = copy.deepcopy(reference_submission())
    bad["code"] = bad["code"].replace('"Bob": rest - alice_share', '"Bob": rest')
    claude = FakeClaude([bad, reference_submission()])
    record = ContractRecord()
    ContractAssistant(claude).respond(record, "contract")
    assert record.status == "ready"
    retry = claude.messages.requests[1]["messages"][-1]["content"]
    assert "AUTOMATIC CHECKS FAILED" in retry and "Built-in check" in retry
    assert all("AUTOMATIC" not in m["text"] for m in record.display)


def test_giving_up_after_repeated_failures_shows_a_plain_error() -> None:
    bad = copy.deepcopy(reference_submission())
    bad["code"] = "import os\n" + bad["code"]
    claude = FakeClaude([bad, bad, bad, bad])
    record = ContractRecord()
    ContractAssistant(claude).respond(record, "contract")
    assert record.status == "chatting" and record.display[-1]["kind"] == "error"
    assert len(claude.messages.requests) == 4


def test_a_new_draft_clears_approvals_but_keeps_and_re_runs_the_users_scenarios() -> None:
    record = drafted([reference_submission(), reference_submission()])
    add_scenario(record, 0)
    record.approve(0)
    ContractAssistant(FakeClaude([reference_submission()])).respond(record, "change it")
    assert record.approvals == [] and record.version == 2
    assert record.scenarios[0] is not None and "steps" in record.simulations[0]
    assert record.simulations[1] is None


def test_a_new_draft_that_cannot_handle_a_saved_scenario_reports_it() -> None:
    record = drafted()
    scenario = parse_scenario(0, {"events": [{"type": "sale", "amount": "77.00", "sale_number": 0}]})
    record.set_scenario(0, scenario, simulate_scenario(record.code, PARTY_NAMES, scenario, 1))
    record.approve(0)
    picky = reference_submission()
    picky["code"] = REFERENCE_CODE.replace(
        "    net = sale_amount - paypal_fee",
        '    if sale_amount == Decimal("77.00"):\n        raise ValueError("unlucky amount")\n    net = sale_amount - paypal_fee', 1)
    ContractAssistant(FakeClaude([picky])).respond(record, "add a quirk")  # passes the built-in checks
    assert record.version == 2 and record.approvals == []
    assert "unlucky amount" in record.simulations[0]["error"]


def test_claude_is_told_exactly_which_expected_value_disagrees_and_fixes_it() -> None:
    wrong_code = copy.deepcopy(reference_submission())
    wrong_code["code"] = REFERENCE_CODE.replace('alice_share = (rest * Decimal("0.60"))', 'alice_share = (rest * Decimal("0.50"))')
    claude = FakeClaude([wrong_code, reference_submission()])
    record = ContractRecord()
    ContractAssistant(claude).respond(record, "contract")
    assert record.status == "ready" and len(claude.messages.requests) == 2
    retry = claude.messages.requests[1]["messages"][-1]["content"]
    assert "expected Alice to be A$128.74 but the code gave A$124.40" in retry
    assert "recompute that event step by step" in retry and "Do not simply copy the code's output" in retry
    assert record.code == REFERENCE_CODE  # the corrected version was kept


def test_claude_can_fix_a_wrong_test_instead_of_the_code() -> None:
    wrong_test = copy.deepcopy(reference_submission())
    wrong_test["tests"][1]["events"][1]["expected_amounts"][0]["amount"] = "39.50"
    claude = FakeClaude([wrong_test, reference_submission()])
    record = ContractRecord()
    ContractAssistant(claude).respond(record, "contract")
    assert record.status == "ready" and "39.50" in claude.messages.requests[1]["messages"][-1]["content"]


def test_a_missing_or_malformed_test_set_is_sent_back() -> None:
    no_tests = copy.deepcopy(reference_submission())
    no_tests["tests"] = []
    claude = FakeClaude([no_tests, reference_submission()])
    record = ContractRecord()
    ContractAssistant(claude).respond(record, "contract")
    assert record.status == "ready" and "exactly 3 tests" in claude.messages.requests[1]["messages"][-1]["content"]


def test_tests_that_keep_failing_end_with_a_plain_error_and_no_draft() -> None:
    wrong = copy.deepcopy(reference_submission())
    wrong["tests"][0]["events"][0]["expected_amounts"][0]["amount"] = "1.00"
    claude = FakeClaude([wrong] * 4)
    record = ContractRecord()
    ContractAssistant(claude).respond(record, "contract")
    assert len(claude.messages.requests) == 4  # the first answer plus three fix rounds
    assert record.status == "chatting" and record.display[-1]["kind"] == "error" and record.internal_checks == []


@pytest.mark.parametrize("response, fragment", [
    (FakeResponse([], stop_reason="refusal"), "declined"),
    (FakeResponse([FakeBlock("{")], stop_reason="max_tokens"), "cut off"),
    (FakeResponse([FakeBlock("not json")]), "not understandable"),
])
def test_unusable_answers_raise_friendly_errors(response: FakeResponse, fragment: str) -> None:
    with pytest.raises(AssistantError, match=fragment):
        ContractAssistant(FakeClaude([response])).respond(ContractRecord(), "x")


def test_system_prompt_covers_the_things_that_must_be_pinned_down() -> None:
    for topic in ("PayPal's fee", "Rounding", "Refunds", "reserve", "recoup", "calculate_payout", "calculate_refund"):
        assert topic.lower() in SYSTEM_PROMPT.lower(), topic


# ---------- scenarios, approvals, emails, freezing ----------

def test_a_scenario_must_run_cleanly_before_it_can_be_approved() -> None:
    record = drafted()
    with pytest.raises(ValueError, match="Run this scenario"):
        record.approve(0)
    record.set_scenario(0, parse_scenario(0, SCENARIOS[0]), {"error": "it broke"})
    with pytest.raises(ValueError, match="Run this scenario"):
        record.approve(0)
    with pytest.raises(ValueError, match="Unknown"):
        record.approve(9)


def test_changing_a_scenario_withdraws_its_approval_only() -> None:
    record = drafted()
    for slot in (0, 1):
        add_scenario(record, slot)
        record.approve(slot)
    add_scenario(record, 0)
    assert record.approvals == [1]
    record.set_scenario(1, None, None)
    assert record.approvals == [] and record.scenarios[1] is None


def test_emails_are_checked_normalised_and_kept_per_party() -> None:
    record = drafted()
    record.set_emails({"Alice": "  Alice@Example.COM ", "Bob": ""})
    assert record.emails == {"Alice": "alice@example.com"} and not record.emails_complete()
    record.set_emails({"Alice": "a@example.com", "Bob": "b@example.com"})
    assert record.emails_complete()


@pytest.mark.parametrize("emails, fragment", [
    ({"Alice": "not-an-email"}, "does not look like an email"),
    ({"Alice": "a@b"}, "does not look like an email"),
    ({"Alice": "a@example.com, b@example.com"}, "does not look like an email"),
    ({"Carol": "c@example.com"}, "not one of the parties"),
    ({"Alice": "same@example.com", "Bob": "SAME@example.com"}, "same email"),
])
def test_bad_emails_are_refused_with_a_reason(emails: dict[str, str], fragment: str) -> None:
    record = drafted()
    with pytest.raises(ValueError, match=fragment):
        record.set_emails(emails)
    assert record.emails == {}


SOLO_CODE = '''
def calculate_payout(sale_id, sale_amount, paypal_fee, state):
    return {"payouts": {"Alice": sale_amount - paypal_fee}, "held_back_change": Decimal("0.00"), "state": state, "notes": []}


def calculate_refund(sale_id, refund_amount, fee_returned, state):
    return {"funding": {"Alice": refund_amount - fee_returned}, "from_holdback": Decimal("0.00"), "state": state, "notes": []}
'''


def test_emails_for_parties_who_are_gone_are_dropped_on_a_new_draft() -> None:
    record = drafted()
    record.set_emails({"Alice": "a@example.com", "Bob": "b@example.com"})
    solo = reference_submission()
    solo["parties"] = [{"name": "Alice", "description": "Now the only partner"}]
    solo["code"] = SOLO_CODE
    solo["tests"] = [
        {"title": "one sale", "events": [with_expected(sale("100.00"), Alice="97.30")]},
        {"title": "sale and full refund", "events": [with_expected(sale("50.00"), Alice="48.50"),
                                                      with_expected(refund("full", 1), Alice="48.80")]},
        {"title": "a small sale", "events": [with_expected(sale("10.00"), Alice="9.46")]},
    ]
    ContractAssistant(FakeClaude([solo])).respond(record, "Bob leaves")
    assert record.status == "ready" and record.version == 2
    assert record.emails == {"Alice": "a@example.com"}


def test_freezing_needs_three_approved_scenarios_and_every_email(tmp_path: Path) -> None:
    store = ContractStore(tmp_path)
    record = store.current()
    ContractAssistant(FakeClaude([reference_submission()])).respond(record, "contract")
    with pytest.raises(ValueError, match="approved scenarios and an email"):
        store.freeze()
    for slot in range(APPROVALS_NEEDED):
        add_scenario(record, slot)
        record.approve(slot)
    record.approve(0)  # approving twice does not count twice
    with pytest.raises(ValueError, match="an email for every party"):
        store.freeze()  # scenarios approved but no emails
    record.set_emails({"Alice": "a@example.com", "Bob": "b@example.com"})
    meta = store.freeze()
    assert record.status == "frozen" and record.frozen == meta
    frozen_file = Path(meta["file"])
    assert frozen_file.read_text() == record.code and not (frozen_file.stat().st_mode & 0o222)
    details = json.loads(frozen_file.with_suffix(".json").read_text())
    assert details["sha256"] == meta["sha256"] and details["emails"] == {"Alice": "a@example.com", "Bob": "b@example.com"}
    assert "a@example.com" not in frozen_file.read_text()  # emails live beside the code, never in it


def test_a_frozen_contract_cannot_be_changed(tmp_path: Path) -> None:
    store = ContractStore(tmp_path)
    record = store.current()
    ContractAssistant(FakeClaude([reference_submission()])).respond(record, "contract")
    for slot in range(SLOTS):
        add_scenario(record, slot)
        record.approve(slot)
    record.set_emails({"Alice": "a@example.com", "Bob": "b@example.com"})
    store.freeze()
    with pytest.raises(ValueError, match="frozen"):
        record.set_scenario(0, None, None)
    with pytest.raises(ValueError, match="frozen"):
        record.set_emails({"Alice": "x@example.com"})
    with pytest.raises(ValueError, match="frozen"):
        record.approve(0)


def test_record_survives_a_restart(tmp_path: Path) -> None:
    store = ContractStore(tmp_path)
    ContractAssistant(FakeClaude([reference_submission()])).respond(store.current(), "contract")
    add_scenario(store.current(), 0)
    store.current().set_emails({"Alice": "a@example.com"})
    store.save()
    again = ContractStore(tmp_path).current()
    assert again.id == store.current().id and again.version == 1
    assert again.scenarios[0]["title"] == SCENARIOS[0]["title"] and again.emails == {"Alice": "a@example.com"}


def test_reset_starts_a_new_contract_and_keeps_frozen_files(tmp_path: Path) -> None:
    store = ContractStore(tmp_path)
    record = store.current()
    ContractAssistant(FakeClaude([reference_submission()])).respond(record, "contract")
    for slot in range(SLOTS):
        add_scenario(record, slot)
        record.approve(slot)
    record.set_emails({"Alice": "a@example.com", "Bob": "b@example.com"})
    frozen = Path(store.freeze()["file"])
    fresh = store.reset()
    assert fresh.status == "chatting" and fresh.display == [] and frozen.exists()


def test_public_view_hides_the_hidden_messages() -> None:
    record = ContractRecord(api_messages=[{"role": "user", "content": "secret fix request"}])
    assert "api_messages" not in record.public() and "secret fix request" not in json.dumps(record.public())
