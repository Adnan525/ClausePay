import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from clausepay.contract_runner import ContractRunner, RunnerError
from clausepay.fees import percentage_fee, sale_fee
from clausepay.live import LiveService, LiveStore, payout_outcome
from clausepay.paypal_client import PayPalError
from tests.contracts import REFERENCE_CODE
from tests.test_contract_runner import EMAILS, RESERVE_CODE, freeze_files


def d(value: str) -> Decimal:
    return Decimal(value)


def money(value: Decimal) -> dict[str, str]:
    return {"currency_code": "AUD", "value": str(value)}


class FakePayPal:
    """Scriptable PayPal: records what was asked and answers like the sandbox did."""

    currency = "AUD"

    def __init__(self) -> None:
        self.orders: dict[str, Decimal] = {}
        self.payouts: list[dict[str, Decimal]] = []
        self.refunds: dict[str, dict[str, Any]] = {}  # request id -> refund
        self.payout_status = "PENDING"  # what get_payout reports
        self.item_status = "SUCCESS"
        self.send_error: PayPalError | None = None
        self.fee_off_by = Decimal("0.00")  # make the reported sale fee differ from the expected one
        self.refund_fee_off_by = Decimal("0.00")
        self.refund_status = "COMPLETED"


    def create_order(self, amount: Decimal, **kwargs: Any) -> dict[str, Any]:
        order_id = f"ORDER{len(self.orders) + 1}"
        self.orders[order_id] = amount
        return {"id": order_id, "links": [{"rel": "payer-action", "href": f"https://paypal.test/pay?token={order_id}"}]}


    @staticmethod
    def approval_link(order: dict[str, Any]) -> str:
        return order["links"][0]["href"]


    def get_order(self, order_id: str) -> dict[str, Any]:
        amount = self.orders[order_id]
        fee = sale_fee(amount) + self.fee_off_by
        capture = {"id": f"CAP-{order_id}", "status": "COMPLETED", "amount": money(amount),
                   "seller_receivable_breakdown": {"gross_amount": money(amount), "paypal_fee": money(fee),
                                                   "net_amount": money(amount - fee)}}
        return {"payer": {"name": {"given_name": "John", "surname": "Doe"}, "email_address": "buyer@example.com"},
                "purchase_units": [{"payments": {"captures": [capture]}}]}


    def capture_order(self, order_id: str) -> dict[str, Any]:
        return self.get_order(order_id)


    def send_payouts(self, payments: dict[str, Decimal], note: str = "") -> dict[str, Any]:
        if self.send_error:
            raise self.send_error
        self.payouts.append(payments)
        return {"payout_batch_id": f"BATCH{len(self.payouts)}", "batch_status": "PENDING"}


    def get_payout(self, batch_id: str) -> dict[str, Any]:
        return {"batch_header": {"batch_status": self.payout_status},
                "items": [{"transaction_status": self.item_status, "payout_item_fee": {"currency": "AUD", "value": "0.02"}}]}


    def refund_capture(self, capture_id: str, amount: Decimal | None = None, note_to_payer: str | None = None,
                       request_id: str | None = None) -> dict[str, Any]:
        key = request_id or str(len(self.refunds))
        if key not in self.refunds:  # PayPal returns the same refund for the same request id
            self.refunds[key] = {"id": f"REFUND{len(self.refunds) + 1}", "amount": amount, "capture": capture_id}
        return {"id": self.refunds[key]["id"], "status": self.refund_status}


    def get_refund(self, refund_id: str) -> dict[str, Any]:
        refund = next(r for r in self.refunds.values() if r["id"] == refund_id)
        returned = percentage_fee(refund["amount"]) + self.refund_fee_off_by
        return {"id": refund_id, "status": self.refund_status,
                "seller_payable_breakdown": {"paypal_fee": money(returned)}}


@pytest.fixture
def paypal() -> FakePayPal:
    return FakePayPal()


def make_service(tmp_path: Path, paypal: FakePayPal, code: str = REFERENCE_CODE) -> LiveService:
    frozen = tmp_path / "frozen"
    frozen.mkdir(exist_ok=True)
    freeze_files(frozen, code)
    return LiveService(lambda: paypal, LiveStore(tmp_path / "live.json"), frozen)  # type: ignore[arg-type, return-value]


@pytest.fixture
def service(tmp_path: Path, paypal: FakePayPal) -> LiveService:
    return make_service(tmp_path, paypal)


@pytest.fixture
def reserve_service(tmp_path: Path, paypal: FakePayPal) -> LiveService:
    return make_service(tmp_path, paypal, RESERVE_CODE)


def sell(service: LiveService, amount: str = "100.00") -> str:
    sale = service.create_sale(d(amount), "https://app/return", "https://app/cancel")
    service.handle_return(sale.order_id)
    return sale.order_id


def pay_out(service: LiveService, paypal: FakePayPal) -> None:
    paypal.payout_status = "SUCCESS"
    service.reconcile()


# ---------- a sale ----------

def test_a_sale_needs_a_frozen_contract(tmp_path: Path, paypal: FakePayPal) -> None:
    empty = LiveService(lambda: paypal, LiveStore(), tmp_path / "frozen")  # type: ignore[arg-type, return-value]
    with pytest.raises(RunnerError, match="no frozen contract"):
        empty.create_sale(d("100.00"), "r", "c")
    assert empty.public() == {"contract": None, "sales": []}


def test_a_paid_sale_runs_the_contract_and_pays_the_partners(service: LiveService, paypal: FakePayPal) -> None:
    order_id = sell(service)
    sale = service.store.get(order_id)
    assert sale is not None and sale.status == "paying" and sale.capture_id == f"CAP-{order_id}"
    assert paypal.payouts == [{"alice@example.com": d("97.30")}]  # 100.00 less the 2.70 fee, all recouped by Alice
    assert (sale.gross, sale.paypal_fee, sale.net) == ("100.00", "2.70", "97.30") and sale.payer_name == "John Doe"
    assert any("PayPal reported keeping A$2.70" in line for line in sale.lines)


def test_the_contracts_memory_only_moves_once_paypal_confirms_the_payout(service: LiveService, paypal: FakePayPal) -> None:
    sell(service)
    runner = service.require_runner()
    assert [e["status"] for e in runner.entries()] == ["pending"]
    service.reconcile()  # PayPal still says PENDING
    assert service.store.all()[0].status == "paying"
    pay_out(service, paypal)
    sale = service.store.all()[0]
    assert sale.status == "paid_out" and sale.payout_fee == "0.02"
    assert [e["status"] for e in runner.entries()] == ["committed"] and runner.state() == {"owed": d("102.70")}


def test_the_next_sale_remembers_the_first_one(service: LiveService, paypal: FakePayPal) -> None:
    sell(service, "100.00")
    pay_out(service, paypal)
    sell(service, "150.00")
    assert paypal.payouts[1] == {"alice@example.com": d("128.74"), "bob@example.com": d("17.36")}  # recoupment finishes


def test_a_second_sale_waits_for_the_first_payout_and_then_runs(service: LiveService, paypal: FakePayPal) -> None:
    first, second = sell(service, "100.00"), sell(service, "150.00")
    assert service.store.get(second).status == "queued" and len(paypal.payouts) == 1  # type: ignore[union-attr]
    pay_out(service, paypal)  # settles the first, then starts the queued one
    assert service.store.get(first).status == "paid_out" and service.store.get(second).status == "paying"  # type: ignore[union-attr]
    assert paypal.payouts[1]["alice@example.com"] == d("128.74")
    pay_out(service, paypal)
    assert service.store.get(second).status == "paid_out"  # type: ignore[union-attr]


def test_a_denied_payout_leaves_the_state_unchanged(service: LiveService, paypal: FakePayPal) -> None:
    order_id = sell(service)
    paypal.payout_status = "DENIED"
    service.reconcile()
    sale = service.store.get(order_id)
    assert sale is not None and sale.status == "failed" and "nobody was paid" in sale.note
    assert service.require_runner().state() == {} and service.require_runner().entries()[0]["status"] == "failed"


def test_a_partly_failed_payout_pauses_everything_instead_of_guessing(service: LiveService, paypal: FakePayPal) -> None:
    sell(service)
    paypal.payout_status, paypal.item_status = "SUCCESS", "UNCLAIMED"
    service.reconcile()
    assert service.store.all()[0].status == "attention"
    assert [e["status"] for e in service.require_runner().entries()] == ["pending"]  # still pending: needs a human


def test_an_unexpected_paypal_fee_pauses_the_sale_before_any_payout(service: LiveService, paypal: FakePayPal) -> None:
    paypal.fee_off_by = d("0.01")
    order_id = sell(service)
    sale = service.store.get(order_id)
    assert sale is not None and sale.status == "attention" and "was expected" in sale.note and paypal.payouts == []
    assert service.require_runner().entries() == []


def test_a_payout_paypal_refuses_outright_is_undone(service: LiveService, paypal: FakePayPal) -> None:
    paypal.send_error = PayPalError("bad", 400, {})
    order_id = sell(service)
    assert service.store.get(order_id).status == "failed"  # type: ignore[union-attr]
    assert service.require_runner().entries()[0]["status"] == "failed"


def test_a_payout_whose_result_is_unknown_stays_pending_for_a_human(service: LiveService, paypal: FakePayPal) -> None:
    paypal.send_error = PayPalError("boom", 500, {})
    order_id = sell(service)
    assert service.store.get(order_id).status == "attention"  # type: ignore[union-attr]
    assert service.require_runner().entries()[0]["status"] == "pending"


def test_returning_twice_pays_out_once(service: LiveService, paypal: FakePayPal) -> None:
    order_id = sell(service)
    service.handle_return(order_id)
    assert len(paypal.payouts) == 1


def test_cancelling_before_paying(service: LiveService) -> None:
    sale = service.create_sale(d("100.00"), "r", "c")
    service.handle_cancel(sale.order_id)
    assert service.store.get(sale.order_id).status == "cancelled"  # type: ignore[union-attr]
    assert service.public()["sales"] == []


def test_a_contract_whose_code_changed_refuses_to_run(tmp_path: Path, paypal: FakePayPal) -> None:
    service = make_service(tmp_path, paypal)
    path = service.contract_path()
    assert path is not None
    path.write_text(REFERENCE_CODE.replace("0.60", "0.90"))
    order_id = sell(service)
    assert service.store.get(order_id).status == "attention" and "does not match the hash" in service.store.get(order_id).note  # type: ignore[union-attr]
    assert "does not match" in service.public()["contract"]["problem"]


@pytest.mark.parametrize("batch, expected", [
    ({"batch_header": {"batch_status": "SUCCESS"}, "items": [{"transaction_status": "SUCCESS"}]}, "success"),
    ({"batch_header": {"batch_status": "PENDING"}, "items": []}, "pending"),
    ({"batch_header": {"batch_status": "PROCESSING"}, "items": []}, "pending"),
    ({"batch_header": {"batch_status": "DENIED"}, "items": []}, "failed"),
    ({"batch_header": {"batch_status": "SUCCESS"}, "items": [{"transaction_status": "RETURNED"}]}, "attention"),
    ({"batch_header": {"batch_status": "SUCCESS"}, "items": [{"transaction_status": "SUCCESS"}, {"transaction_status": "FAILED"}]}, "attention"),
])
def test_payout_batches_are_reduced_to_four_outcomes(batch: dict[str, Any], expected: str) -> None:
    assert payout_outcome(batch)[0] == expected


# ---------- refunds ----------

def paid_800(service: LiveService, paypal: FakePayPal) -> str:
    order_id = sell(service, "800.00")  # 780.50 received: reserve 78.05, Alice 621.47, Bob 80.98
    pay_out(service, paypal)
    return order_id


def test_a_small_refund_comes_from_the_reserve_and_needs_no_partner_money(reserve_service: LiveService, paypal: FakePayPal) -> None:
    order_id = paid_800(reserve_service, paypal)
    result = reserve_service.refund(order_id, d("20.00"), "request-0001", False)
    sale = result["sale"]
    assert sale["refunds"][0]["status"] == "done" and sale["refunds"][0]["from_reserve"] == "19.52"
    assert sale["refunded"] == "20.00" and sale["refundable"] == "780.00"
    assert len(paypal.refunds) == 1 and reserve_service.require_runner().reserve() == d("58.53")


def test_a_refund_only_takes_what_is_left_of_the_sale(reserve_service: LiveService, paypal: FakePayPal) -> None:
    order_id = paid_800(reserve_service, paypal)
    reserve_service.refund(order_id, d("500.00"), "request-0001", True)
    with pytest.raises(RunnerError, match="Only 300.00"):
        reserve_service.refund(order_id, d("400.00"), "request-0002", True)
    assert len(paypal.refunds) == 1  # PayPal was never asked for the second one
    final = reserve_service.refund(order_id, None, "request-0003", True)["sale"]  # no amount: refund what is left
    assert final["refundable"] == "0.00" and final["refunded"] == "800.00"
    with pytest.raises(RunnerError, match="left to refund"):
        reserve_service.refund(order_id, d("1.00"), "request-0004", True)


def test_the_same_refund_click_twice_refunds_once(reserve_service: LiveService, paypal: FakePayPal) -> None:
    order_id = paid_800(reserve_service, paypal)
    reserve_service.refund(order_id, d("20.00"), "request-0001", False)
    again = reserve_service.refund(order_id, d("20.00"), "request-0001", False)["sale"]
    assert len(paypal.refunds) == 1 and len(again["refunds"]) == 1 and again["refunded"] == "20.00"


def test_a_big_refund_asks_for_the_partner_transfer_before_touching_paypal(reserve_service: LiveService, paypal: FakePayPal) -> None:
    order_id = paid_800(reserve_service, paypal)
    asked = reserve_service.refund(order_id, d("400.00"), "request-0001", False)
    needs = asked["needs_partner_transfer"]
    assert needs["from_reserve"] == "78.05" and needs["transfers"] == {"Alice": "156.18", "Bob": "156.17"}
    assert paypal.refunds == {} and reserve_service.store.get(order_id).refunds == []  # type: ignore[union-attr]
    done = reserve_service.refund(order_id, d("400.00"), "request-0001", True)["sale"]
    assert done["refunds"][0]["partners_confirmed"] is True and done["refunds"][0]["transfers"] == {"Alice": "156.18", "Bob": "156.17"}
    assert reserve_service.require_runner().reserve() == d("0.00") and len(paypal.refunds) == 1


def test_only_a_paid_out_sale_can_be_refunded(service: LiveService, paypal: FakePayPal) -> None:
    order_id = sell(service)  # payout still pending
    with pytest.raises(RunnerError, match="paid out"):
        service.refund(order_id, d("10.00"), "request-0001", False)
    with pytest.raises(RunnerError, match="paid out"):
        service.refund("nope", d("10.00"), "request-0001", False)


def test_a_refund_paypal_makes_but_the_contract_cannot_record_is_flagged_loudly(reserve_service: LiveService, paypal: FakePayPal) -> None:
    order_id = paid_800(reserve_service, paypal)
    paypal.refund_fee_off_by = d("0.05")
    sale = reserve_service.refund(order_id, d("20.00"), "request-0001", False)["sale"]
    refund = sale["refunds"][0]
    assert refund["status"] == "attention" and "WAS refunded by PayPal" in refund["note"]
    assert reserve_service.require_runner().refundable(f"CAP-{order_id}") == d("800.00")  # the contract did not count it


def test_a_refund_paypal_has_not_completed_is_flagged(reserve_service: LiveService, paypal: FakePayPal) -> None:
    order_id = paid_800(reserve_service, paypal)
    paypal.refund_status = "PENDING"
    refund = reserve_service.refund(order_id, d("20.00"), "request-0001", False)["sale"]["refunds"][0]
    assert refund["status"] == "attention" and "PENDING" in refund["note"]


def test_nothing_is_refunded_while_another_transaction_is_unconfirmed(reserve_service: LiveService, paypal: FakePayPal) -> None:
    order_id = paid_800(reserve_service, paypal)
    sell(reserve_service, "100.00")  # leaves a pending entry
    with pytest.raises(RunnerError, match="still waiting"):
        reserve_service.refund(order_id, d("20.00"), "request-0001", False)
    assert paypal.refunds == {}


# ---------- storage and what the page shows ----------

def test_sales_survive_a_restart(tmp_path: Path, paypal: FakePayPal) -> None:
    service = make_service(tmp_path, paypal)
    order_id = sell(service)
    reloaded = LiveStore(tmp_path / "live.json")
    assert reloaded.get(order_id).status == "paying" and reloaded.get(order_id).payouts == {"alice@example.com": "97.30"}  # type: ignore[union-attr]


def test_a_damaged_sales_file_does_not_crash_the_app(tmp_path: Path) -> None:
    path = tmp_path / "live.json"
    path.write_text("{broken")
    assert LiveStore(path).all() == []


def test_the_page_data_shows_the_contract_its_memory_and_the_sales(service: LiveService, paypal: FakePayPal) -> None:
    sell(service)
    pay_out(service, paypal)
    page = service.public()
    contract = page["contract"]
    assert contract["emails"] == EMAILS and contract["state"] == {"owed": "102.70"} and contract["reserve"] == "0.00"
    assert contract["pending"] == [] and len(contract["sha256"]) == 64
    assert page["sales"][0]["status"] == "paid_out" and page["sales"][0]["refundable"] == "100.00"
    json.dumps(page)  # everything is JSON-safe


def test_the_most_recently_frozen_contract_is_the_one_used(tmp_path: Path, paypal: FakePayPal) -> None:
    service = make_service(tmp_path, paypal)
    newer = freeze_files(tmp_path / "frozen", RESERVE_CODE, "newer-v1")
    newer.with_suffix(".json").touch()
    newer.touch()
    assert service.contract_path() == newer
    assert isinstance(service.runner(), ContractRunner)
