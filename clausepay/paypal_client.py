"""Thin wrapper around PayPal's REST API (sandbox by default).

Every public method does exactly one PayPal call. The wrapper never decides an
amount: it only sends what it is given, formatted as whole cents.
"""
import os
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import requests
from dotenv import find_dotenv, load_dotenv

from clausepay import fees
from clausepay.fees import CENT, DEFAULT_FEE_RATE, DEFAULT_FIXED_FEE

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
TIMEOUT_SECONDS = 30

Json = dict[str, Any]


class PayPalError(Exception):
    """Raised for any failed PayPal call or invalid input to the wrapper."""

    def __init__(self, message: str, status: int | None = None, body: Json | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}


class PayPalClient:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        currency: str = "AUD",
        base_url: str = SANDBOX_URL,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
        fixed_fee: Decimal = DEFAULT_FIXED_FEE,
    ) -> None:
        load_dotenv(find_dotenv(usecwd=True))
        self.client_id: str | None = client_id or os.getenv("client_id")
        self.client_secret: str | None = client_secret or os.getenv("client_secret")
        if not self.client_id or not self.client_secret:
            raise PayPalError("client_id / client_secret missing (check .env)")
        self.currency = currency
        self.base_url = base_url
        self.fee_rate = fee_rate
        self.fixed_fee = fixed_fee
        self._token: str | None = None
        self._token_expires_at = 0.0


    # ---- plumbing -------------------------------------------------------

    def _fetch_token(self) -> tuple[str, float]:
        response = requests.post(
            f"{self.base_url}/v1/oauth2/token",
            auth=(self.client_id or "", self.client_secret or ""),
            data={"grant_type": "client_credentials"},
            timeout=TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise PayPalError(f"auth failed: {response.status_code}", response.status_code)
        body = response.json()
        return body["access_token"], time.time() + float(body["expires_in"])


    def _get_token(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at - 30:
            self._token, self._token_expires_at = self._fetch_token()
        return self._token


    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Json | None = None,
        body: Json | None = None,
        request_id: str | None = None,
    ) -> Json:
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            params=params,
            json=body,
            timeout=TIMEOUT_SECONDS,
        )
        if not response.ok:
            detail = response.json() if response.content else {}
            raise PayPalError(f"{method} {path} failed: {response.status_code}", response.status_code, detail)
        return response.json() if response.content else {}


    @staticmethod
    def new_request_id() -> str:
        """A fresh idempotency key. Reuse the same key to safely retry a call."""
        return uuid.uuid4().hex


    def _format_amount(self, amount: Decimal) -> str:
        if not isinstance(amount, Decimal):
            raise PayPalError(f"amount must be a Decimal, got {type(amount).__name__}")
        value = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        if value <= 0:
            raise PayPalError(f"amount must be positive, got {amount}")
        return f"{value:.2f}"


    def percentage_fee(self, amount: Decimal) -> Decimal:
        """The percentage part of PayPal's fee on `amount`, in whole cents (also what a refund returns)."""
        return fees.percentage_fee(amount, self.fee_rate)


    def sale_fee(self, amount: Decimal) -> Decimal:
        """The total fee PayPal takes on a sale of `amount`: percentage part plus the fixed fee."""
        return fees.sale_fee(amount, self.fee_rate, self.fixed_fee)


    def _money(self, amount: Decimal) -> Json:
        return {"currency_code": self.currency, "value": self._format_amount(amount)}


    # ---- account checks -------------------------------------------------

    def get_balances(self) -> Json:
        """Raw balances for the business account (needs the Balances feature)."""
        return self._request("GET", "/v1/reporting/balances", params={"currency_code": self.currency})


    def get_available_balance(self) -> Decimal:
        """Spendable balance in the client's currency."""
        for entry in self.get_balances().get("balances", []):
            if entry.get("currency") == self.currency:
                return Decimal(entry["available_balance"]["value"])
        raise PayPalError(f"no {self.currency} balance found on the account")


    def list_transactions(self, start_date: str, end_date: str) -> Json:
        """Transaction Search. Dates are ISO 8601 with timezone, range of at most 31 days."""
        params = {"start_date": start_date, "end_date": end_date, "fields": "all"}
        return self._request("GET", "/v1/reporting/transactions", params=params)


    # ---- sales (money in) -----------------------------------------------

    def create_order(
        self,
        amount: Decimal,
        custom_id: str | None = None,
        payment_source: Json | None = None,
        request_id: str | None = None,
        return_url: str | None = None,
        cancel_url: str | None = None,
    ) -> Json:
        """Create a CAPTURE order. `custom_id` is echoed back on webhooks and captures.

        With `return_url` and `cancel_url`, PayPal sends the buyer back there after they pay or cancel.
        """
        unit: Json = {"amount": self._money(amount)}
        if custom_id:
            unit["custom_id"] = custom_id
        if return_url and cancel_url and not payment_source:
            context: Json = {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            }
            payment_source = {"paypal": {"experience_context": context}}
        body: Json = {"intent": "CAPTURE", "purchase_units": [unit]}
        if payment_source:
            body["payment_source"] = payment_source
        return self._request("POST", "/v2/checkout/orders", body=body, request_id=request_id or self.new_request_id())


    @staticmethod
    def approval_link(order: Json) -> str:
        """The link the buyer opens to pay (PayPal calls it `payer-action`, or `approve` on older orders)."""
        for link in order.get("links", []):
            if link.get("rel") in ("payer-action", "approve"):
                return link["href"]
        raise PayPalError("order has no approval link")


    def get_order(self, order_id: str) -> Json:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")


    def capture_order(self, order_id: str, request_id: str | None = None) -> Json:
        """Capture an order the payer has approved."""
        return self._request(
            "POST", f"/v2/checkout/orders/{order_id}/capture", body={}, request_id=request_id or self.new_request_id()
        )


    def get_capture(self, capture_id: str) -> Json:
        """A capture, including seller_receivable_breakdown (gross, PayPal fee, net)."""
        return self._request("GET", f"/v2/payments/captures/{capture_id}")


    # ---- refunds (money out to buyer) -----------------------------------

    def refund_capture(
        self,
        capture_id: str,
        amount: Decimal | None = None,
        note_to_payer: str | None = None,
        request_id: str | None = None,
    ) -> Json:
        """Refund a capture. No amount means a full refund."""
        body: Json = {}
        if amount is not None:
            body["amount"] = self._money(amount)
        if note_to_payer:
            body["note_to_payer"] = note_to_payer
        return self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            body=body,
            request_id=request_id or self.new_request_id(),
        )


    def get_refund(self, refund_id: str) -> Json:
        """A refund, including seller_payable_breakdown (gross, fee returned, net)."""
        return self._request("GET", f"/v2/payments/refunds/{refund_id}")


    # ---- payouts (money out to partners) --------------------------------

    def send_payouts(self, payments: dict[str, Decimal], note: str = "ClausePay payout") -> Json:
        """Send a batch of {email: amount}. Returns the batch header."""
        batch_id = f"clausepay-{uuid.uuid4().hex}"
        body: Json = {
            "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": "You have a ClausePay payout"},
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": email,
                    "amount": {"value": self._format_amount(amount), "currency": self.currency},
                    "note": note,
                    "sender_item_id": f"{batch_id}-{index}",
                }
                for index, (email, amount) in enumerate(payments.items())
            ],
        }
        result = self._request("POST", "/v1/payments/payouts", body=body)
        return {**result["batch_header"], "sender_batch_id": batch_id}


    def get_payout(self, payout_batch_id: str) -> Json:
        """A payout batch with per-item status and the fee charged."""
        return self._request("GET", f"/v1/payments/payouts/{payout_batch_id}")


    # ---- vault (partner approves once, ClausePay charges later) ---------

    def create_vault_setup_token(self, return_url: str, cancel_url: str) -> Json:
        """Start saving a partner's PayPal wallet. Send them to the returned approve link."""
        paypal_source: Json = {
            "usage_type": "MERCHANT",
            "usage_pattern": "UNSCHEDULED_POSTPAID",
            "experience_context": {"return_url": return_url, "cancel_url": cancel_url},
        }
        return self._request(
            "POST",
            "/v3/vault/setup-tokens",
            body={"payment_source": {"paypal": paypal_source}},
            request_id=self.new_request_id(),
        )


    def create_vault_payment_token(self, setup_token_id: str) -> Json:
        """Turn an approved setup token into a reusable payment token (vault id)."""
        source: Json = {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}
        return self._request(
            "POST", "/v3/vault/payment-tokens", body={"payment_source": source}, request_id=self.new_request_id()
        )


    def charge_vaulted(self, vault_id: str, amount: Decimal, custom_id: str | None = None) -> Json:
        """Charge a saved wallet with no payer clicks. PayPal captures at creation for vaulted sources."""
        source: Json = {"paypal": {"vault_id": vault_id}}
        order = self.create_order(amount, custom_id=custom_id, payment_source=source)
        if order.get("status") == "COMPLETED":
            return order
        return self.capture_order(order["id"])


    # ---- webhooks -------------------------------------------------------

    def verify_webhook(self, webhook_id: str, headers: dict[str, str], event: Json) -> bool:
        """Ask PayPal whether a received webhook is genuine. `headers` are the request headers."""
        lowered = {key.lower(): value for key, value in headers.items()}
        body: Json = {
            "auth_algo": lowered.get("paypal-auth-algo"),
            "cert_url": lowered.get("paypal-cert-url"),
            "transmission_id": lowered.get("paypal-transmission-id"),
            "transmission_sig": lowered.get("paypal-transmission-sig"),
            "transmission_time": lowered.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        result = self._request("POST", "/v1/notifications/verify-webhook-signature", body=body)
        return result.get("verification_status") == "SUCCESS"
