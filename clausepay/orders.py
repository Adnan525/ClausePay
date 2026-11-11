import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from clausepay.paypal_client import Json

TERMINAL_STATUSES = {"received", "pending", "cancelled", "failed"}


@dataclass
class Order:
    order_id: str
    amount: Decimal
    approve_url: str
    status: str = "created"  # created | received | pending | cancelled | failed
    capture_id: str | None = None
    gross: Decimal | None = None
    paypal_fee: Decimal | None = None
    net: Decimal | None = None
    expected_fee: Decimal | None = None
    payer_name: str | None = None
    payer_email: str | None = None
    webhook_confirmed: bool = False
    error: str | None = None


    def to_json(self, currency: str, webhook_enabled: bool) -> dict[str, Any]:
        def text(value: Decimal | None) -> str | None:
            return None if value is None else f"{value:.2f}"

        return {
            "order_id": self.order_id,
            "status": self.status,
            "currency": currency,
            "amount": text(self.amount),
            "approve_url": self.approve_url,
            "capture_id": self.capture_id,
            "gross": text(self.gross),
            "paypal_fee": text(self.paypal_fee),
            "net": text(self.net),
            "expected_fee": text(self.expected_fee),
            "fee_matches": None if self.expected_fee is None else self.expected_fee == self.paypal_fee,
            "payer_name": self.payer_name,
            "payer_email": self.payer_email,
            "webhook_confirmed": self.webhook_confirmed,
            "webhook_enabled": webhook_enabled,
            "error": self.error,
        }


def first_capture(order_response: Json) -> Json | None:
    """The first capture inside a PayPal order response, if there is one."""
    units = order_response.get("purchase_units", [])
    captures = units[0].get("payments", {}).get("captures", []) if units else []
    return captures[0] if captures else None


def payer_of(order_response: Json) -> tuple[str | None, str | None]:
    payer = order_response.get("payer", {})
    name = payer.get("name", {})
    full_name = " ".join(part for part in (name.get("given_name"), name.get("surname")) if part)
    return full_name or None, payer.get("email_address")


class OrderStore:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._seen_events: set[str] = set()
        self._lock = threading.Lock()


    def add(self, order: Order) -> None:
        with self._lock:
            self._orders[order.order_id] = order


    def get(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)


    def record_capture(self, order_id: str, order_response: Json, expected_fee: Decimal) -> None:
        """Store what PayPal reported for a captured order."""
        capture = first_capture(order_response)
        with self._lock:
            order = self._orders[order_id]
            if capture is None:
                order.status, order.error = "failed", "PayPal returned no capture"
                return
            breakdown = capture.get("seller_receivable_breakdown", {})
            order.capture_id = capture["id"]
            order.gross = Decimal(breakdown.get("gross_amount", capture["amount"])["value"])
            order.paypal_fee = Decimal(breakdown["paypal_fee"]["value"]) if "paypal_fee" in breakdown else None
            order.net = Decimal(breakdown["net_amount"]["value"]) if "net_amount" in breakdown else None
            order.expected_fee = expected_fee
            order.payer_name, order.payer_email = payer_of(order_response)
            order.status = "received" if capture.get("status") == "COMPLETED" else "pending"


    def mark_cancelled(self, order_id: str) -> None:
        with self._lock:
            order = self._orders.get(order_id)
            if order and order.status == "created":
                order.status = "cancelled"


    def mark_failed(self, order_id: str, message: str) -> None:
        with self._lock:
            order = self._orders.get(order_id)
            if order:
                order.status, order.error = "failed", message


    def confirm_webhook(self, order_id: str) -> bool:
        """Mark an order as confirmed by a verified webhook. False if we don't know the order."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return False
            order.webhook_confirmed = True
            return True


    def already_seen(self, event_id: str) -> bool:
        """True if this webhook event id was handled before; otherwise remembers it."""
        with self._lock:
            if event_id in self._seen_events:
                return True
            self._seen_events.add(event_id)
            return False
