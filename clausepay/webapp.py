"""ClausePay demo web app. Demo only: PayPal sandbox, no real money."""
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anthropic
from dotenv import find_dotenv, load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from clausepay.accounts import load_accounts
from clausepay.contract_chat import AssistantError, ContractAssistant, scenario_context
from clausepay.contract_runner import PendingTransactionError, RunnerError
from clausepay.contract_sim import MAX_SALE, ContractProblem, parse_scenario, simulate_scenario
from clausepay.contract_store import ContractStore
from clausepay.live import BackgroundWorker, LiveService, LiveStore
from clausepay.orders import Order, OrderStore
from clausepay.reports import ReportService, ReportWriter
from clausepay.paypal_client import Json, PayPalClient, PayPalError

MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("500.00")


def parse_amount(text: object, maximum: Decimal = MAX_AMOUNT) -> Decimal:
    """A demo purchase amount in whole cents between MIN_AMOUNT and `maximum`."""
    try:
        amount = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError("Enter an amount like 25.00")
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Use whole cents, like 25.00")
    if not MIN_AMOUNT <= amount <= maximum:
        raise ValueError(f"Demo amounts are between {MIN_AMOUNT} and {maximum}")
    return amount


def parse_refund_amount(text: object) -> Decimal | None:
    """A refund amount in whole cents, or None for "the whole remaining amount"."""
    if text is None or str(text).strip() == "":
        return None
    try:
        amount = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError("Enter an amount like 25.00, or leave it empty to refund what is left")
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("A refund must be a positive amount in whole cents")
    return amount


def create_app(
    client: PayPalClient | None = None,
    store: OrderStore | None = None,
    contracts: ContractStore | None = None,
    assistant: ContractAssistant | None = None,
    report_writer: ReportWriter | None = None,
) -> Flask:
    load_dotenv(find_dotenv(usecwd=True))
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore[method-assign]
    orders = store or OrderStore()
    contract_store = contracts or ContractStore(Path(os.getenv("data_dir", "data")))
    paypal: list[PayPalClient] = [client] if client else []
    claude: list[ContractAssistant] = [assistant] if assistant else []
    live_store = LiveStore(contract_store.directory / "live.json")


    def get_client() -> PayPalClient:
        if not paypal:
            paypal.append(PayPalClient())
        return paypal[0]


    live = LiveService(lambda: get_client(), live_store, contract_store.directory / "frozen")


    def get_report_writer() -> ReportWriter:
        if report_writer is not None:
            return report_writer
        key = os.getenv("claude_key", "").strip()  # without a key the reports use fixed wording, but are still written
        return ReportWriter(anthropic.Anthropic(api_key=key, timeout=60.0) if key else None)


    reports = ReportService(live, get_report_writer)


    def get_assistant() -> ContractAssistant:
        if not claude:
            key = os.getenv("claude_key", "").strip()
            if not key:
                raise AssistantError("Claude is not set up on this server: the claude_key setting is missing.")
            claude.append(ContractAssistant(anthropic.Anthropic(api_key=key, timeout=170.0)))
        return claude[0]


    def contract_json() -> Json:
        """The contract as the browser sees it, plus the demo accounts it can offer for the email form."""
        sellers = [account for account in load_accounts() if account.label.startswith("Seller") and account.email]
        return {**contract_store.current().public(),
                "demo_emails": [{"label": account.label, "email": account.email} for account in sellers]}


    def webhook_id() -> str:
        return os.getenv("paypal_webhook_id", "").strip()


    def order_json(order: Order) -> Json:
        return order.to_json(get_client().currency, bool(webhook_id()))


    def capture_and_record(order_id: str) -> None:
        """Capture an approved order and store what PayPal reported."""
        paypal_client = get_client()
        try:
            response = paypal_client.capture_order(order_id)
        except PayPalError as error:
            issues = [detail.get("issue") for detail in error.body.get("details", [])]
            if "ORDER_ALREADY_CAPTURED" not in issues:
                orders.mark_failed(order_id, f"Capture failed ({error.status})")
                return
            response = paypal_client.get_order(order_id)
        placed = orders.get(order_id)
        gross = placed.amount if placed else Decimal("0")
        orders.record_capture(order_id, response, paypal_client.sale_fee(gross))


    @app.get("/")
    def home() -> Any:
        """Once a contract is frozen the main app opens on the transactions; before that, on the contract."""
        if live.contract_path() is not None:
            return redirect(url_for("transactions_page"))
        return render_template("contract.html", active="contract")


    @app.get("/contract")
    def contract_page() -> str:
        return render_template("contract.html", active="contract")


    @app.get("/transactions")
    def transactions_page() -> str:
        return render_template("transactions.html", active="transactions")


    @app.get("/accounts")
    def dashboard() -> str:
        return render_template("dashboard.html", accounts=load_accounts(), active="accounts")


    @app.get("/api/contract")
    def contract_state() -> Json:
        return contract_json()


    @app.post("/api/contract/message")
    def contract_message() -> tuple[Json, int]:
        body = request.get_json(silent=True) or {}
        text = str(body.get("text", "")).strip()
        record = contract_store.current()
        context = ""
        if body.get("scenario") is not None and text:
            try:
                slot = int(body["scenario"])
            except (TypeError, ValueError):
                return {"error": "Unknown scenario."}, 400
            context = scenario_context(record, slot)
            if context:
                text = f"About scenario {slot + 1} (\"{record.scenarios[slot]['title']}\"): {text}"
        if not text:
            return {"error": "Write a message first."}, 400
        if record.status == "frozen":
            return {"error": "This contract is frozen. Start over to write a new one."}, 409
        if not contract_store.lock.acquire(blocking=False):
            return {"error": "Claude is still answering your last message."}, 409
        try:
            try:
                get_assistant().respond(record, text, context)
            except AssistantError as error:
                record.display.append({"role": "assistant", "kind": "error", "text": str(error)})
                contract_store.save()
                return {**contract_json(), "error": str(error)}, 502
            contract_store.save()
            return contract_json(), 200
        finally:
            contract_store.lock.release()


    @app.post("/api/contract/approve")
    def contract_approve() -> tuple[Json, int]:
        record = contract_store.current()
        try:
            record.approve(int((request.get_json(silent=True) or {}).get("scenario", -1)))
        except (ValueError, TypeError) as error:
            return {"error": str(error)}, 400
        contract_store.save()
        return contract_json(), 200


    @app.post("/api/contract/scenario")
    def contract_scenario() -> tuple[Json, int]:
        """Save one of the user's scenarios and dry-run it against the contract code."""
        record = contract_store.current()
        body = request.get_json(silent=True) or {}
        try:
            slot = int(body.get("slot", -1))
            scenario = parse_scenario(slot, body)
            names = [party["name"] for party in record.parties]
            try:
                result: Json = simulate_scenario(record.code, names, scenario, slot + 1)
            except ContractProblem as problem:
                result = {"error": " ".join(problem.problems)}
            record.set_scenario(slot, scenario, result)
        except ContractProblem as problem:
            return {"error": " ".join(problem.problems)}, 400
        except (ValueError, TypeError) as error:
            return {"error": str(error)}, 400
        contract_store.save()
        return contract_json(), 200


    @app.post("/api/contract/scenario/clear")
    def contract_scenario_clear() -> tuple[Json, int]:
        try:
            contract_store.current().set_scenario(int((request.get_json(silent=True) or {}).get("slot", -1)), None, None)
        except (ValueError, TypeError) as error:
            return {"error": str(error)}, 400
        contract_store.save()
        return contract_json(), 200


    @app.post("/api/contract/emails")
    def contract_emails() -> tuple[Json, int]:
        try:
            contract_store.current().set_emails((request.get_json(silent=True) or {}).get("emails", {}))
        except (ValueError, AttributeError, TypeError) as error:
            return {"error": str(error)}, 400
        contract_store.save()
        return contract_json(), 200


    @app.post("/api/contract/freeze")
    def contract_freeze() -> tuple[Json, int]:
        try:
            contract_store.freeze()
        except ValueError as error:
            return {"error": str(error)}, 400
        return contract_json(), 200


    @app.post("/api/contract/reset")
    def contract_reset() -> tuple[Json, int]:
        if not contract_store.lock.acquire(blocking=False):
            return {"error": "Claude is still answering; try again in a moment."}, 409
        try:
            contract_store.reset()
            return contract_json(), 200
        finally:
            contract_store.lock.release()


    @app.get("/order")
    def order_page() -> str:
        buyer = next(account for account in load_accounts() if account.label == "Buyer")
        return render_template("order.html", buyer=buyer, active="order", min_amount=MIN_AMOUNT, max_amount=MAX_AMOUNT)


    @app.post("/api/orders")
    def create_order() -> tuple[Json, int]:
        try:
            amount = parse_amount((request.get_json(silent=True) or {}).get("amount"))
        except ValueError as error:
            return {"error": str(error)}, 400
        try:
            created = get_client().create_order(
                amount,
                custom_id="clausepay-demo-sale",
                return_url=url_for("paypal_return", _external=True),
                cancel_url=url_for("paypal_cancel", _external=True),
            )
            link = get_client().approval_link(created)
        except PayPalError as error:
            return {"error": f"PayPal refused the order ({error.status})"}, 502
        orders.add(Order(order_id=created["id"], amount=amount, approve_url=link))
        return {"order_id": created["id"], "approve_url": link}, 201


    @app.get("/api/orders/<order_id>")
    def order_status(order_id: str) -> tuple[Json, int]:
        order = orders.get(order_id)
        if order is None:
            return {"error": "unknown order"}, 404
        return order_json(order), 200


    @app.get("/paypal/return")
    def paypal_return() -> tuple[str, int]:
        order_id = request.args.get("token", "")
        order = orders.get(order_id)
        if order is None:
            return render_template("closing.html", message="We don't know this order.", order_id=""), 404
        if order.status == "created":
            capture_and_record(order_id)
        return render_template("closing.html", message="Payment approved. You can close this tab.", order_id=order_id), 200


    @app.get("/paypal/cancel")
    def paypal_cancel() -> tuple[str, int]:
        order_id = request.args.get("token", "")
        orders.mark_cancelled(order_id)
        return render_template("closing.html", message="Payment cancelled. You can close this tab.", order_id=order_id), 200


    @app.post("/webhooks/paypal")
    def paypal_webhook() -> tuple[Json, int]:
        if not webhook_id():
            return {"error": "webhook not configured"}, 503
        event = request.get_json(silent=True) or {}
        try:
            genuine = get_client().verify_webhook(webhook_id(), dict(request.headers), event)
        except PayPalError:
            return {"error": "could not verify"}, 502
        if not genuine:
            return {"error": "bad signature"}, 400
        event_type, resource = event.get("event_type"), event.get("resource", {})
        live_store.log("webhook", f"PayPal sent {event_type} ({event.get('id')}).")
        first_time = not orders.already_seen(str(event.get("id", "")))
        result: Json = {}
        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            related = resource.get("supplementary_data", {}).get("related_ids", {})
            try:  # every sale runs the contract; this is safe to repeat, so PayPal's retries do no harm
                sale = live.ingest_capture(str(resource.get("id", "")), "webhook", related.get("order_id"))
            except PayPalError:
                return {"error": "could not read the payment from PayPal"}, 502  # PayPal will try again
            except RunnerError as error:
                live_store.log("attention", f"Could not run the contract for payment {resource.get('id')}: {error}")
                sale = None
            if sale is not None:
                result["live"] = sale.status
            if first_time:
                result["matched"] = orders.confirm_webhook(str(related.get("order_id", "")))
        elif event_type == "PAYMENT.CAPTURE.REFUNDED":
            live_store.log("refund", f"PayPal reports a refund ({resource.get('id')}). Refunds made outside ClausePay are not "
                                     "run through the contract.")
        if not first_time:
            result["duplicate"] = True
        elif not result and event_type != "PAYMENT.CAPTURE.COMPLETED":
            result["ignored"] = event_type
        return result, 200


    # ---------- live sales: real sandbox sales that run the frozen contract ----------

    @app.get("/live")
    def live_page() -> str:
        buyer = next(account for account in load_accounts() if account.label == "Buyer")
        return render_template("live.html", buyer=buyer, active="live", min_amount=MIN_AMOUNT, max_amount=MAX_AMOUNT)


    @app.get("/api/live")
    def live_state() -> Json:
        live.reconcile()
        return {**live.public(), "log": list(reversed(live_store.entries())), "webhook_configured": bool(webhook_id()),
                "journal": live.journal()}


    @app.get("/api/transactions")
    def transactions_state() -> Json:
        live.reconcile()
        data = live.public()
        data["sales"] = [sale for sale in data["sales"] if sale["status"] != "created"]  # unpaid orders are not transactions
        return data


    @app.post("/api/live/sales")
    def live_create_sale() -> tuple[Json, int]:
        try:
            amount = parse_amount((request.get_json(silent=True) or {}).get("amount"), MAX_SALE)
        except ValueError as error:
            return {"error": str(error)}, 400
        try:
            sale = live.create_sale(amount, url_for("live_return", _external=True), url_for("live_cancel", _external=True))
        except RunnerError as error:
            return {"error": str(error)}, 409
        except PayPalError as error:
            return {"error": f"PayPal refused the order ({error.status})"}, 502
        return {"order_id": sale.order_id, "approve_url": sale.approve_url}, 201


    @app.get("/live/return")
    def live_return() -> tuple[str, int]:
        order_id = request.args.get("token", "")
        if live_store.get(order_id) is None:
            return render_template("closing.html", message="We don't know this order.", order_id=""), 404
        live.handle_return(order_id)
        return render_template("closing.html", message="Payment approved. You can close this tab.", order_id=order_id), 200


    @app.get("/live/cancel")
    def live_cancel() -> tuple[str, int]:
        order_id = request.args.get("token", "")
        live.handle_cancel(order_id)
        return render_template("closing.html", message="Payment cancelled. You can close this tab.", order_id=order_id), 200


    @app.post("/api/live/sales/<order_id>/refund")
    def live_refund(order_id: str) -> tuple[Json, int]:
        body = request.get_json(silent=True) or {}
        request_id = str(body.get("request_id", "")).strip()
        if not 8 <= len(request_id) <= 64:
            return {"error": "A refund needs a request id so that a double click cannot refund twice."}, 400
        try:
            amount = parse_refund_amount(body.get("amount"))
            return live.refund(order_id, amount, request_id, bool(body.get("partners_confirmed"))), 200
        except ValueError as error:
            return {"error": str(error)}, 400
        except PendingTransactionError as error:
            return {"error": str(error)}, 409
        except RunnerError as error:
            return {"error": str(error)}, 400


    @app.get("/health")
    def health() -> tuple[str, int]:
        return "ok", 200


    if os.getenv("background_worker", "on").strip().lower() != "off":
        BackgroundWorker([live.reconcile, reports.generate_pending], live_store).start()  # payouts settle even when nobody has a page open
    return app
