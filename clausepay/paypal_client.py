"""Thin wrapper around PayPal's sandbox REST API.

Money amounts are Decimal and always sent as whole cents. The wrapper never
decides an amount; it only sends exactly what it is given.
"""
import os
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP

import requests
from dotenv import load_dotenv

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
CENT = Decimal("0.01")


class PayPalError(Exception):
    pass


class PayPalClient:
    def __init__(self, client_id=None, client_secret=None, currency="AUD", base_url=SANDBOX_URL):
        load_dotenv()
        self.client_id = client_id or os.getenv("client_id")
        self.client_secret = client_secret or os.getenv("client_secret")
        if not self.client_id or not self.client_secret:
            raise PayPalError("client_id / client_secret missing (check .env)")
        self.currency = currency
        self.base_url = base_url
        self._token = None
        self._token_expires = 0.0

    def _get_token(self):
        if self._token and time.time() < self._token_expires - 30:
            return self._token
        resp = requests.post(
            f"{self.base_url}/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        if not resp.ok:
            raise PayPalError(f"auth failed: {resp.status_code} {resp.text}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + body["expires_in"]
        return self._token

    def _request(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        resp = requests.request(method, f"{self.base_url}{path}", headers=headers, timeout=30, **kwargs)
        if not resp.ok:
            raise PayPalError(f"{method} {path} failed: {resp.status_code} {resp.text}")
        return resp.json()

    @staticmethod
    def _cents(amount):
        value = Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)
        if value <= 0:
            raise PayPalError(f"amount must be positive, got {amount}")
        return f"{value:.2f}"

    def send_payout(self, email, amount, note="ClausePay payout"):
        """Send one payment to one email. Returns the batch id and status."""
        return self.send_payouts([(email, amount)], note=note)

    def send_payouts(self, payments, note="ClausePay payout"):
        """Send a batch of (email, amount) pairs in the client's currency."""
        batch_id = f"clausepay-{uuid.uuid4().hex}"
        body = {
            "sender_batch_header": {
                "sender_batch_id": batch_id,
                "email_subject": "You have a ClausePay payout",
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": email,
                    "amount": {"value": self._cents(amount), "currency": self.currency},
                    "note": note,
                    "sender_item_id": f"{batch_id}-{i}",
                }
                for i, (email, amount) in enumerate(payments)
            ],
        }
        result = self._request("POST", "/v1/payments/payouts", json=body)
        header = result["batch_header"]
        return {
            "payout_batch_id": header["payout_batch_id"],
            "batch_status": header["batch_status"],
            "sender_batch_id": batch_id,
        }

    def get_payout(self, payout_batch_id):
        """Fetch a batch, including per-item status and the fee charged."""
        return self._request("GET", f"/v1/payments/payouts/{payout_batch_id}")
