from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from clausepay.contract_chat import ContractAssistant
from clausepay.contract_store import ContractStore
from clausepay.orders import OrderStore
from clausepay.paypal_client import PayPalClient, PayPalError
from clausepay.webapp import create_app, parse_amount
from tests.contracts import reference_submission
from tests.fake_claude import FakeClaude, question


class FakePayPal:
    """Stands in for PayPalClient: no network, records what the app asked for."""

    currency = "AUD"

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.captured: list[str] = []
        self.capture_error: PayPalError | None = None
        self.webhook_ok = True


    def create_order(self, amount: Decimal, **kwargs: Any) -> dict[str, Any]:
        self.created.append({"amount": amount, **kwargs})
        return {"id": "ORDER1", "links": [{"rel": "payer-action", "href": "https://paypal.test/pay?token=ORDER1"}]}


    def approval_link(self, order: dict[str, Any]) -> str:
        return PayPalClient.approval_link(order)


    def capture_order(self, order_id: str) -> dict[str, Any]:
        self.captured.append(order_id)
        if self.capture_error:
            raise self.capture_error
        return self.order_response()


    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.order_response()


    def sale_fee(self, amount: Decimal) -> Decimal:
        return Decimal("0.54") if amount == Decimal("10.00") else Decimal("9.99")


    def verify_webhook(self, webhook_id: str, headers: dict[str, str], event: dict[str, Any]) -> bool:
        return self.webhook_ok


    @staticmethod
    def order_response() -> dict[str, Any]:
        money = lambda value: {"currency_code": "AUD", "value": value}  # noqa: E731
        capture = {
            "id": "CAP1", "status": "COMPLETED", "amount": money("10.00"),
            "seller_receivable_breakdown": {
                "gross_amount": money("10.00"), "paypal_fee": money("0.54"), "net_amount": money("9.46"),
            },
        }
        return {
            "payer": {"name": {"given_name": "John", "surname": "Doe"}, "email_address": "buyer@example.com"},
            "purchase_units": [{"payments": {"captures": [capture]}}],
        }


@pytest.fixture
def fake() -> FakePayPal:
    return FakePayPal()


@pytest.fixture
def claude() -> FakeClaude:
    return FakeClaude([])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, fake: FakePayPal, claude: FakeClaude, tmp_path: Path) -> Iterator[FlaskClient]:
    monkeypatch.setenv("u_1_email", "a@example.com")
    monkeypatch.setenv("u_2_email", "b@example.com")
    monkeypatch.setenv("buyer_email", "buyer@example.com")
    monkeypatch.setenv("business_email", "")  # set (empty), so the real .env is not loaded over it
    monkeypatch.setenv("paypal_webhook_id", "")
    monkeypatch.setenv("buyer_password", "super-secret-password")
    monkeypatch.setenv("client_secret", "super-secret-client")
    app = create_app(client=fake, store=OrderStore(), contracts=ContractStore(tmp_path),  # type: ignore[arg-type]
                     assistant=ContractAssistant(claude))  # type: ignore[arg-type]
    yield app.test_client()


def place_order(client: FlaskClient, amount: str = "10.00") -> dict[str, Any]:
    response = client.post("/api/orders", json={"amount": amount})
    assert response.status_code == 201
    return response.get_json()


def test_every_page_states_demo_only(client: FlaskClient) -> None:
    for path in ("/", "/accounts", "/order"):
        assert "DEMO ONLY" in client.get(path).get_data(as_text=True)


def test_accounts_page_shows_every_account_label_and_email(client: FlaskClient) -> None:
    page = client.get("/accounts").get_data(as_text=True)
    for text in ("Business", "Seller A", "Seller B", "Buyer", "a@example.com", "b@example.com", "buyer@example.com"):
        assert text in page
    assert "not configured" in page  # the business email is not set in this test


def test_pages_never_leak_secrets(client: FlaskClient) -> None:
    for path in ("/", "/accounts", "/order"):
        assert "super-secret" not in client.get(path).get_data(as_text=True)


def test_order_tab_has_buyer_card_with_email(client: FlaskClient) -> None:
    page = client.get("/order").get_data(as_text=True)
    assert "buyer@example.com" in page and "Copy email" in page


def test_health_endpoint(client: FlaskClient) -> None:
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("bad", ["", "abc", "0.99", "500.01", "10.005", "nan", "-5"])
def test_bad_amounts_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_amount(bad)


def test_creating_an_order_returns_the_link_and_sets_return_urls(client: FlaskClient, fake: FakePayPal) -> None:
    body = place_order(client)
    assert body["approve_url"].startswith("https://paypal.test/")
    sent = fake.created[0]
    assert sent["amount"] == Decimal("10.00")
    assert sent["return_url"].endswith("/paypal/return") and sent["cancel_url"].endswith("/paypal/cancel")


def test_bad_amount_never_reaches_paypal(client: FlaskClient, fake: FakePayPal) -> None:
    assert client.post("/api/orders", json={"amount": "9999"}).status_code == 400
    assert fake.created == []


def test_return_captures_and_records_funds(client: FlaskClient, fake: FakePayPal) -> None:
    place_order(client)
    assert client.get("/api/orders/ORDER1").get_json()["status"] == "created"
    page = client.get("/paypal/return?token=ORDER1")
    assert page.status_code == 200 and "window.close" in page.get_data(as_text=True)
    order = client.get("/api/orders/ORDER1").get_json()
    assert (order["status"], order["gross"], order["paypal_fee"], order["net"]) == ("received", "10.00", "0.54", "9.46")
    assert order["fee_matches"] is True and order["payer_name"] == "John Doe"


def test_returning_twice_captures_only_once(client: FlaskClient, fake: FakePayPal) -> None:
    place_order(client)
    client.get("/paypal/return?token=ORDER1")
    client.get("/paypal/return?token=ORDER1")
    assert fake.captured == ["ORDER1"]


def test_unknown_order_is_not_captured(client: FlaskClient, fake: FakePayPal) -> None:
    assert client.get("/paypal/return?token=NOPE").status_code == 404
    assert fake.captured == []


def test_fee_mismatch_is_flagged(client: FlaskClient) -> None:
    place_order(client, "20.00")  # the fake expects 9.99 for this amount but PayPal reports 0.54
    client.get("/paypal/return?token=ORDER1")
    assert client.get("/api/orders/ORDER1").get_json()["fee_matches"] is False


def test_already_captured_order_is_recovered(client: FlaskClient, fake: FakePayPal) -> None:
    fake.capture_error = PayPalError("x", 422, {"details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
    place_order(client)
    client.get("/paypal/return?token=ORDER1")
    assert client.get("/api/orders/ORDER1").get_json()["status"] == "received"


def test_failed_capture_is_reported_without_details(client: FlaskClient, fake: FakePayPal) -> None:
    fake.capture_error = PayPalError("secret detail", 500, {})
    place_order(client)
    client.get("/paypal/return?token=ORDER1")
    order = client.get("/api/orders/ORDER1").get_json()
    assert order["status"] == "failed" and "secret detail" not in str(order)


def test_cancel_marks_the_order_cancelled(client: FlaskClient) -> None:
    place_order(client)
    client.get("/paypal/cancel?token=ORDER1")
    assert client.get("/api/orders/ORDER1").get_json()["status"] == "cancelled"


def webhook_event(event_id: str = "EVT1") -> dict[str, Any]:
    return {"id": event_id, "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"supplementary_data": {"related_ids": {"order_id": "ORDER1"}}}}


def test_webhook_is_refused_when_not_configured(client: FlaskClient) -> None:
    assert client.post("/webhooks/paypal", json=webhook_event()).status_code == 503


def test_webhook_with_bad_signature_is_rejected(
    client: FlaskClient, fake: FakePayPal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("paypal_webhook_id", "WH1")
    fake.webhook_ok = False
    place_order(client)
    assert client.post("/webhooks/paypal", json=webhook_event()).status_code == 400
    assert client.get("/api/orders/ORDER1").get_json()["webhook_confirmed"] is False


def test_verified_webhook_confirms_the_order_once(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("paypal_webhook_id", "WH1")
    place_order(client)
    client.get("/paypal/return?token=ORDER1")
    assert client.post("/webhooks/paypal", json=webhook_event()).get_json() == {"matched": True}
    assert client.post("/webhooks/paypal", json=webhook_event()).get_json() == {"duplicate": True}
    order = client.get("/api/orders/ORDER1").get_json()
    assert order["webhook_confirmed"] is True and order["webhook_enabled"] is True


# ---------- contract (main app) ----------

def ready(client: FlaskClient, claude: FakeClaude) -> dict[str, Any]:
    claude.messages.replies.append(reference_submission())
    return client.post("/api/contract/message", json={"text": "the whole contract"}).get_json()


def run_scenario(client: FlaskClient, slot: int, events: list[dict[str, Any]], title: str = "") -> Any:
    return client.post("/api/contract/scenario", json={"slot": slot, "title": title, "events": events})


def sale(amount: str) -> dict[str, Any]:
    return {"type": "sale", "amount": amount, "sale_number": 0}


def refund(amount: str, sale_number: int) -> dict[str, Any]:
    return {"type": "refund", "amount": amount, "sale_number": sale_number}


def approve_all(client: FlaskClient) -> None:
    for slot in range(3):
        assert run_scenario(client, slot, [sale("100.00"), refund("", 1)]).status_code == 200
        assert client.post("/api/contract/approve", json={"scenario": slot}).status_code == 200


EMAILS = {"emails": {"Alice": "alice@example.com", "Bob": "bob@example.com"}}


def test_contract_is_the_main_tab_and_the_other_tabs_are_demo_helpers(client: FlaskClient) -> None:
    home = client.get("/").get_data(as_text=True)
    assert 'id="panel-chat"' in home and "Review &amp; approve" in home
    assert 'href="/" class="active"' in home and "Demo helpers" in home
    assert 'href="/accounts"' in home and 'href="/order"' in home


def test_a_new_contract_starts_empty(client: FlaskClient) -> None:
    state = client.get("/api/contract").get_json()
    assert (state["status"], state["display"], state["approvals_needed"]) == ("chatting", [], 3)
    assert state["scenarios"] == [None, None, None] and state["emails"] == {} and "api_messages" not in state


def test_chatting_with_claude_returns_the_updated_conversation(client: FlaskClient, claude: FakeClaude) -> None:
    claude.messages.replies.append(question("Who pays PayPal's fee?"))
    state = client.post("/api/contract/message", json={"text": "A and B split 60/40"}).get_json()
    assert [m["role"] for m in state["display"]] == ["user", "assistant"]
    assert state["display"][1]["text"] == "Who pays PayPal's fee?" and state["status"] == "chatting"


def test_empty_messages_are_refused(client: FlaskClient, claude: FakeClaude) -> None:
    assert client.post("/api/contract/message", json={"text": "  "}).status_code == 400
    assert claude.messages.requests == []


def test_a_finished_contract_is_ready_with_no_scenarios_yet(client: FlaskClient, claude: FakeClaude) -> None:
    state = ready(client, claude)
    assert state["status"] == "ready" and state["scenarios"] == [None] * 3 and state["approvals"] == []
    assert state["parties"][0]["name"] == "Alice" and "def calculate_payout" in state["code"]
    assert [d["label"] for d in state["demo_emails"]] == ["Seller A", "Seller B"]


def test_scenarios_cannot_be_run_before_there_is_a_contract(client: FlaskClient) -> None:
    response = run_scenario(client, 0, [sale("100.00")])
    assert response.status_code == 400 and "no draft" in response.get_json()["error"]


def test_a_user_scenario_is_run_and_explained(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    state = run_scenario(client, 0, [sale("100.00"), refund("", 1)], "My first test").get_json()
    assert state["scenarios"][0]["title"] == "My first test"
    steps = state["simulations"][0]["steps"]
    assert "A$97.30" in " ".join(steps[0]["lines"]) and "refunded A$100.00" in " ".join(steps[1]["lines"])
    assert state["simulations"][1] is None


def test_bad_scenario_input_is_explained_and_not_saved(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    response = run_scenario(client, 0, [sale("0.10"), refund("5", 3)])
    assert response.status_code == 400 and "sale amount must be" in response.get_json()["error"]
    assert client.get("/api/contract").get_json()["scenarios"][0] is None
    assert run_scenario(client, 7, [sale("10.00")]).status_code == 400


def test_a_scenario_the_contract_cannot_handle_shows_the_problem(client: FlaskClient, claude: FakeClaude) -> None:
    claude.messages.replies.append({**reference_submission(), "code": reference_submission()["code"].replace(
        "    net = sale_amount - paypal_fee",
        '    if sale_amount == Decimal("77.00"):\n        raise ValueError("unlucky amount")\n    net = sale_amount - paypal_fee', 1)})
    client.post("/api/contract/message", json={"text": "contract"})
    state = run_scenario(client, 0, [sale("77.00")]).get_json()
    assert "unlucky amount" in state["simulations"][0]["error"]
    assert client.post("/api/contract/approve", json={"scenario": 0}).status_code == 400


def test_editing_a_scenario_withdraws_only_its_approval(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    for slot in (0, 1):
        run_scenario(client, slot, [sale("100.00")])
        client.post("/api/contract/approve", json={"scenario": slot})
    state = run_scenario(client, 0, [sale("120.00")]).get_json()
    assert state["approvals"] == [1]


def test_clearing_a_scenario(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    run_scenario(client, 2, [sale("100.00")])
    state = client.post("/api/contract/scenario/clear", json={"slot": 2}).get_json()
    assert state["scenarios"][2] is None and state["simulations"][2] is None


def test_unknown_or_unrun_scenarios_cannot_be_approved(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    assert client.post("/api/contract/approve", json={"scenario": 0}).status_code == 400
    assert client.post("/api/contract/approve", json={"scenario": 7}).status_code == 400
    assert client.post("/api/contract/approve", json={"scenario": "x"}).status_code == 400


def test_emails_are_saved_by_the_server_and_checked(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    state = client.post("/api/contract/emails", json=EMAILS).get_json()
    assert state["emails"] == EMAILS["emails"] and state["emails_complete"] is True
    bad = client.post("/api/contract/emails", json={"emails": {"Alice": "nope"}})
    assert bad.status_code == 400 and "does not look like an email" in bad.get_json()["error"]
    assert client.get("/api/contract").get_json()["emails"] == EMAILS["emails"]  # a rejected save changes nothing
    assert client.post("/api/contract/emails", json={"emails": {"Carol": "c@example.com"}}).status_code == 400


def test_freezing_needs_three_approvals_and_every_email(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    early = client.post("/api/contract/freeze", json={})
    assert early.status_code == 400 and "approved scenarios and an email" in early.get_json()["error"]
    approve_all(client)
    assert client.post("/api/contract/freeze", json={}).status_code == 400  # still no emails
    client.post("/api/contract/emails", json=EMAILS)
    frozen = client.post("/api/contract/freeze", json={})
    assert frozen.status_code == 200 and frozen.get_json()["status"] == "frozen"
    assert len(frozen.get_json()["frozen"]["sha256"]) == 64


def test_a_frozen_contract_is_locked(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    approve_all(client)
    client.post("/api/contract/emails", json=EMAILS)
    client.post("/api/contract/freeze", json={})
    assert client.post("/api/contract/message", json={"text": "change it"}).status_code == 409
    assert client.post("/api/contract/approve", json={"scenario": 0}).status_code == 400
    assert run_scenario(client, 0, [sale("5.00")]).status_code == 400
    assert client.post("/api/contract/emails", json={"emails": {"Alice": "x@example.com"}}).status_code == 400


def test_a_changed_contract_resets_the_approvals_but_keeps_scenarios_and_emails(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    run_scenario(client, 0, [sale("100.00")])
    client.post("/api/contract/approve", json={"scenario": 0})
    client.post("/api/contract/emails", json=EMAILS)
    claude.messages.replies.append(reference_submission())
    state = client.post("/api/contract/message", json={"text": "actually change it"}).get_json()
    assert state["approvals"] == [] and state["version"] == 2
    assert state["scenarios"][0] is not None and "steps" in state["simulations"][0]
    assert state["emails"] == EMAILS["emails"]


def test_start_over_clears_the_draft(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    state = client.post("/api/contract/reset", json={}).get_json()
    assert state["status"] == "chatting" and state["display"] == [] and state["emails"] == {}


def test_claude_failures_are_shown_in_the_chat_not_as_a_crash(client: FlaskClient, claude: FakeClaude) -> None:
    from tests.fake_claude import FakeBlock, FakeResponse
    claude.messages.replies.append(FakeResponse([FakeBlock("{")], stop_reason="max_tokens"))
    response = client.post("/api/contract/message", json={"text": "contract"})
    body = response.get_json()
    assert response.status_code == 502 and "cut off" in body["error"]
    assert body["display"][-1]["kind"] == "error"


def test_a_missing_claude_key_is_explained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("claude_key", "")
    app = create_app(contracts=ContractStore(tmp_path))
    response = app.test_client().post("/api/contract/message", json={"text": "hello"})
    assert response.status_code == 502 and "claude_key" in response.get_json()["error"]


def test_commenting_on_a_scenario_sends_its_numbers_to_claude_and_a_short_note_to_the_chat(
        client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    run_scenario(client, 0, [sale("100.00"), refund("", 1)], "Sale then refund")
    claude.messages.replies.append(question("Here is how the rules produced those numbers..."))
    state = client.post("/api/contract/message", json={"text": "Alice should get more", "scenario": 0}).get_json()
    shown = state["display"][-2]["text"]
    assert shown == 'About scenario 1 ("Sale then refund"): Alice should get more'
    sent = claude.messages.requests[-1]["messages"][-1]["content"]
    assert "The buyer pays A$100.00" in sent and "Alice should get more" in sent
    assert "The buyer pays" not in str(state["display"])
    assert state["approvals"] == [] and state["status"] == "ready"  # talking does not change the draft


def test_commenting_on_an_empty_slot_is_just_a_normal_message(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    claude.messages.replies.append(question("ok"))
    state = client.post("/api/contract/message", json={"text": "hello", "scenario": 2}).get_json()
    assert state["display"][-2]["text"] == "hello"
    assert "[Context" not in claude.messages.requests[-1]["messages"][-1]["content"]


def test_a_nonsense_scenario_number_is_refused(client: FlaskClient, claude: FakeClaude) -> None:
    ready(client, claude)
    assert client.post("/api/contract/message", json={"text": "hi", "scenario": "x"}).status_code == 400
