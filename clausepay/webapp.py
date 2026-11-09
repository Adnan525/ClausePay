"""ClausePay demo web app. Demo only: PayPal sandbox, no real money."""
import os
from decimal import Decimal, InvalidOperation

from dotenv import find_dotenv, load_dotenv
from flask import Flask, jsonify, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from clausepay.accounts import load_accounts
from clausepay.orders import Order, OrderStore
from clausepay.paypal_client import Json, PayPalClient, PayPalError

MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("500.00")


def parse_amount(text: object) -> Decimal:
    """A demo purchase amount in whole cents between MIN_AMOUNT and MAX_AMOUNT."""
    try:
        amount = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError("Enter an amount like 25.00")
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Use whole cents, like 25.00")
    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        raise ValueError(f"Demo amounts are between {MIN_AMOUNT} and {MAX_AMOUNT}")
    return amount


def create_app(client: PayPalClient | None = None, store: OrderStore | None = None) -> Flask:
    load_dotenv(find_dotenv(usecwd=True))
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # type: ignore[method-assign]
    orders = store or OrderStore()
    paypal: list[PayPalClient] = [client] if client else []


    def get_client() -> PayPalClient:
        if not paypal:
            paypal.append(PayPalClient())
        return paypal[0]


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
    def dashboard() -> str:
        return render_template("dashboard.html", accounts=load_accounts(), active="accounts")


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
        if orders.already_seen(str(event.get("id", ""))):
            return {"duplicate": True}, 200
        if event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
            related = event.get("resource", {}).get("supplementary_data", {}).get("related_ids", {})
            return {"matched": orders.confirm_webhook(str(related.get("order_id", "")))}, 200
        return {"ignored": event.get("event_type")}, 200


    @app.get("/health")
    def health() -> tuple[str, int]:
        return "ok", 200


    return app
