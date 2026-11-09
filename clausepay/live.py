"""Live (sandbox) sales that really run a frozen contract.

The flow for a sale: the buyer pays through PayPal, ClausePay captures the payment, the contract
runner decides who gets what (recorded as pending), the partners are paid through PayPal Payouts,
and only when PayPal confirms the payout does the runner commit and move the contract's memory
forward. A refund goes through the same runner, can only take from what is left of its sale, and
is safe to repeat (same request id means the same refund).

Demo shortcuts, clearly shown to the user: when a refund needs partners to transfer money in,
the user confirms that the transfer happened (ClausePay does not charge their wallets yet).
"""
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from clausepay.contract_runner import (
    ContractRunner, PendingTransactionError, RunnerError, Transaction,
)
from clausepay.orders import first_capture, payer_of
from clausepay.paypal_client import Json, PayPalClient, PayPalError

ZERO = Decimal("0.00")
FINAL_STATUSES = {"paid_out", "cancelled", "failed", "attention"}


@dataclass
class LiveSale:
    order_id: str
    amount: str
    approve_url: str = ""
    status: str = "created"  # created | queued | paying | paid_out | attention | failed | cancelled
    note: str = ""
    capture_id: str | None = None
    gross: str | None = None
    paypal_fee: str | None = None
    net: str | None = None
    payer_name: str | None = None
    payer_email: str | None = None
    entry_id: str | None = None
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    payouts: dict[str, str] = field(default_factory=dict)  # email -> amount
    payout_batch_id: str | None = None
    payout_status: str | None = None
    payout_fee: str | None = None
    refunds: list[dict[str, Any]] = field(default_factory=list)


class LiveStore:
    """The live sales, saved to a JSON file so a restart does not lose track of money in flight."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.lock = threading.RLock()
        self._sales: dict[str, LiveSale] = {}
        if path and path.exists():
            try:
                self._sales = {item["order_id"]: LiveSale(**item) for item in json.loads(path.read_text())}
            except (ValueError, TypeError, KeyError):
                self._sales = {}


    def add(self, sale: LiveSale) -> None:
        with self.lock:
            self._sales[sale.order_id] = sale
            self.save()


    def get(self, order_id: str) -> LiveSale | None:
        with self.lock:
            return self._sales.get(order_id)


    def all(self) -> list[LiveSale]:
        with self.lock:
            return list(self._sales.values())


    def save(self) -> None:
        if self.path is None:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps([asdict(sale) for sale in self._sales.values()], indent=2))
            os.replace(temporary, self.path)


def _jsonable(value: Any) -> Any:
    """Decimals become strings so state and amounts can be sent to the browser."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _money(value: Any) -> Decimal:
    return Decimal(str(value["value"]))


def payout_outcome(batch: Json) -> tuple[str, Decimal]:
    """Reduce a PayPal payout batch to ("success" | "pending" | "failed" | "attention", fee charged)."""
    status = str(batch.get("batch_header", {}).get("batch_status", "")).upper()
    items = batch.get("items", [])
    fee = sum((_money(item["payout_item_fee"]) for item in items if item.get("payout_item_fee")), ZERO)
    item_states = {str(item.get("transaction_status", "")).upper() for item in items}
    if status in ("DENIED", "CANCELED"):
        return "failed", fee
    if status in ("PENDING", "PROCESSING", "NEW", ""):
        return "pending", fee
    if status == "SUCCESS" and item_states <= {"SUCCESS"}:
        return "success", fee
    return "attention", fee


class LiveService:
    def __init__(self, get_client: Callable[[], PayPalClient], store: LiveStore, frozen_dir: Path) -> None:
        self.get_client = get_client
        self.store = store
        self.frozen_dir = frozen_dir


    # ---- the frozen contract ------------------------------------------------------------

    def contract_path(self) -> Path | None:
        """The code file of the most recently frozen contract, if any."""
        if not self.frozen_dir.exists():
            return None
        details = [path for path in self.frozen_dir.glob("*.json") if not path.name.endswith(".journal.json")]
        details = [path for path in details if path.with_suffix(".py").exists()]
        return max(details, key=lambda path: path.stat().st_mtime).with_suffix(".py") if details else None


    def runner(self) -> ContractRunner | None:
        path = self.contract_path()
        return ContractRunner(path) if path else None


    def require_runner(self) -> ContractRunner:
        runner = self.runner()
        if runner is None:
            raise RunnerError("There is no frozen contract yet. Freeze one in the Contract tab first.")
        return runner


    # ---- a sale -------------------------------------------------------------------------

    def create_sale(self, amount: Decimal, return_url: str, cancel_url: str) -> LiveSale:
        self.require_runner()
        client = self.get_client()
        order = client.create_order(amount, custom_id="clausepay-live-sale", return_url=return_url, cancel_url=cancel_url)
        sale = LiveSale(order_id=order["id"], amount=str(amount), approve_url=client.approval_link(order))
        self.store.add(sale)
        return sale


    def handle_cancel(self, order_id: str) -> None:
        with self.store.lock:
            sale = self.store.get(order_id)
            if sale and sale.status == "created":
                sale.status = "cancelled"
                self.store.save()


    def handle_return(self, order_id: str) -> LiveSale | None:
        """The buyer came back from PayPal: capture the payment, then run the contract."""
        with self.store.lock:
            sale = self.store.get(order_id)
            if sale is None or sale.status != "created":
                return sale
            client = self.get_client()
            try:
                response = client.capture_order(order_id)
            except PayPalError as error:
                issues = [detail.get("issue") for detail in error.body.get("details", [])]
                if "ORDER_ALREADY_CAPTURED" not in issues:
                    sale.status, sale.note = "failed", f"The payment could not be captured (error {error.status})."
                    self.store.save()
                    return sale
                response = client.get_order(order_id)
            capture = first_capture(response)
            if capture is None or capture.get("status") != "COMPLETED":
                sale.status, sale.note = "attention", "PayPal has not completed this payment, so nothing was paid out."
                self.store.save()
                return sale
            breakdown = capture["seller_receivable_breakdown"]
            sale.capture_id = capture["id"]
            sale.gross, sale.paypal_fee = str(_money(breakdown["gross_amount"])), str(_money(breakdown["paypal_fee"]))
            sale.net = str(_money(breakdown["net_amount"]))
            sale.payer_name, sale.payer_email = payer_of(response)
            self._process(sale)
            return sale


    def _process(self, sale: LiveSale) -> None:
        """Run the contract on a captured sale and start the payout. Caller holds the store lock."""
        runner = self.require_runner()
        try:
            transaction = runner.process_sale(str(sale.capture_id), Decimal(str(sale.gross)), Decimal(str(sale.paypal_fee)))
        except PendingTransactionError:
            sale.status, sale.note = "queued", "Waiting for the previous transaction to finish."
            self.store.save()
            return
        except RunnerError as error:
            sale.status, sale.note = "attention", f"Paused, nothing was paid out. {error}"
            self.store.save()
            return
        sale.entry_id, sale.lines, sale.notes = transaction.entry_id, transaction.lines, transaction.notes
        try:
            payouts = transaction.by_email(runner.emails)
        except RunnerError as error:
            runner.fail(transaction.entry_id, str(error))
            sale.status, sale.note = "failed", str(error)
            self.store.save()
            return
        sale.payouts = {email: str(amount) for email, amount in payouts.items()}
        if not payouts:
            runner.commit(transaction.entry_id)
            sale.status, sale.note = "paid_out", "Nobody is paid out from this sale."
            self.store.save()
            return
        try:
            batch = self.get_client().send_payouts(payouts, note="ClausePay payout")
        except PayPalError as error:
            if error.status is not None and 400 <= error.status < 500:  # refused outright: nothing was sent
                runner.fail(transaction.entry_id, f"PayPal refused the payout (error {error.status})")
                sale.status, sale.note = "failed", f"PayPal refused the payout (error {error.status}). The contract's state was not changed."
            else:  # unknown whether it went through: stay pending and ask for a human
                sale.status, sale.note = "attention", "The payout request may or may not have gone through. The contract is paused until it is checked."
            self.store.save()
            return
        sale.payout_batch_id, sale.payout_status = batch["payout_batch_id"], str(batch.get("batch_status", "PENDING"))
        sale.status, sale.note = "paying", ""
        self.store.save()


    # ---- keeping up with PayPal ---------------------------------------------------------

    def reconcile(self) -> None:
        """Settle payouts that PayPal has finished, then start sales that were waiting their turn."""
        with self.store.lock:
            runner = self.runner()
            if runner is None:
                return
            for sale in self.store.all():
                if sale.status == "paying":
                    self._settle_payout(runner, sale)
            for sale in self.store.all():
                if sale.status == "queued":
                    self._process(sale)


    def _settle_payout(self, runner: ContractRunner, sale: LiveSale) -> None:
        try:
            batch = self.get_client().get_payout(str(sale.payout_batch_id))
        except PayPalError:
            return  # try again on the next check
        outcome, fee = payout_outcome(batch)
        sale.payout_status = str(batch.get("batch_header", {}).get("batch_status", ""))
        sale.payout_fee = str(fee)
        if outcome == "success":
            runner.commit(str(sale.entry_id))
            sale.status, sale.note = "paid_out", ""
        elif outcome == "failed":
            runner.fail(str(sale.entry_id), "PayPal denied the payout")
            sale.status, sale.note = "failed", "PayPal denied the payout, so nobody was paid. The contract's state was not changed."
        elif outcome == "attention":
            sale.status, sale.note = "attention", "PayPal reports a problem with part of this payout. The contract is paused until it is checked."
        self.store.save()


    # ---- a refund -----------------------------------------------------------------------

    def refund(self, order_id: str, amount: Decimal | None, request_id: str, partners_confirmed: bool) -> dict[str, Any]:
        """Refund (part of) a sale through PayPal and run it through the contract.

        Returns {"sale": ...} when done, or {"needs_partner_transfer": ...} when the contract says partners must
        put money in first and the user has not confirmed that yet. Raises RunnerError for refusals.
        """
        with self.store.lock:
            sale = self.store.get(order_id)
            if sale is None or sale.status != "paid_out" or not sale.capture_id:
                raise RunnerError("Only a sale that has been paid out can be refunded.")
            if any(refund["request_id"] == request_id for refund in sale.refunds):
                return {"sale": self.describe(sale)}  # the same click arriving twice
            runner = self.require_runner()
            left = runner.refundable(sale.capture_id)
            wanted = left if amount is None else amount
            quote = runner.quote_refund(sale.capture_id, wanted)  # raises if over-refunding or something is pending
            transfers = {name: value for name, value in quote.amounts.items() if value > 0}
            if transfers and not partners_confirmed:
                return {"needs_partner_transfer": {"amount": str(wanted), "transfers": _jsonable(transfers),
                                                   "from_reserve": str(-quote.reserve_change), "lines": quote.lines}}
            client = self.get_client()
            try:
                response = client.refund_capture(sale.capture_id, wanted, request_id=request_id)
                details = client.get_refund(response["id"])
            except PayPalError as error:
                raise RunnerError(f"PayPal did not refund the buyer (error {error.status}). Nothing was recorded.") from None
            record: dict[str, Any] = {"request_id": request_id, "refund_id": response["id"], "amount": str(wanted),
                                      "status": "attention", "note": "", "lines": [], "notes": [],
                                      "transfers": _jsonable(transfers), "partners_confirmed": bool(transfers)}
            sale.refunds.append(record)
            self._record_refund(runner, sale, record, details, wanted)
            self.store.save()
            return {"sale": self.describe(sale)}


    def _record_refund(self, runner: ContractRunner, sale: LiveSale, record: dict[str, Any], details: Json, wanted: Decimal) -> None:
        """PayPal has refunded the buyer: run it through the contract. Any problem here needs a human, so it is flagged."""
        if details.get("status") != "COMPLETED":
            record["note"] = f"PayPal's refund is {details.get('status')}, not completed. Check it before relying on it."
            return
        try:
            fee_returned = _money(details["seller_payable_breakdown"]["paypal_fee"])
            transaction: Transaction = runner.process_refund(str(sale.capture_id), record["refund_id"], wanted, fee_returned)
            runner.commit(transaction.entry_id)
        except (RunnerError, KeyError, InvalidOperation) as error:
            record["note"] = f"The buyer WAS refunded by PayPal, but ClausePay could not record it: {error}"
            return
        record.update(status="done", lines=transaction.lines, notes=transaction.notes,
                      fee_returned=str(fee_returned), from_reserve=str(-transaction.reserve_change))


    # ---- what the page shows ------------------------------------------------------------

    def describe(self, sale: LiveSale) -> dict[str, Any]:
        data = _jsonable(asdict(sale))
        runner = self.runner()
        data["refunded"], data["refundable"] = "0.00", "0.00"
        if runner is not None and sale.status == "paid_out" and sale.capture_id:
            try:
                left = runner.refundable(sale.capture_id)
                data["refundable"], data["refunded"] = str(left), str(Decimal(str(sale.gross)) - left)
            except RunnerError:
                pass
        return data


    def public(self) -> dict[str, Any]:
        runner = self.runner()
        if runner is None:
            return {"contract": None, "sales": []}
        try:
            state, reserve = _jsonable(runner.state()), str(runner.reserve())
            problem = ""
        except RunnerError as error:
            state, reserve, problem = {}, "0.00", str(error)  # for example the code no longer matches its hash
        meta = json.loads(runner.meta_path.read_text())
        pending = [entry["entry_id"] for entry in (runner.entries() if not problem else []) if entry["status"] == "pending"]
        return {
            "contract": {"id": runner.contract_id, "version": runner.version, "sha256": runner.expected_hash,
                         "frozen_at": meta.get("frozen_at", ""), "parties": meta["parties"], "emails": runner.emails,
                         "summary": meta.get("summary", []), "state": state, "reserve": reserve, "problem": problem,
                         "pending": pending},
            "sales": [self.describe(sale) for sale in reversed(self.store.all()) if sale.status != "cancelled"],
        }
