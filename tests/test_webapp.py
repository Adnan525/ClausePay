from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from flask.testing import FlaskClient

from clausepay.orders import OrderStore
from clausepay.paypal_client import PayPalClient, PayPalError
from clausepay.webapp import create_app, parse_amount


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
def client(monkeypatch: pytest.MonkeyPatch, fake: FakePayPal) -> Iterator[FlaskClient]:
    monkeypatch.setenv("u_1_email", "a@example.com")
    monkeypatch.setenv("u_2_email", "b@example.com")
    monkeypatch.setenv("buyer_email", "buyer@example.com")
    monkeypatch.setenv("business_email", "")  # set (empty), so the real .env is not loaded over it
    monkeypatch.setenv("paypal_webhook_id", "")
    monkeypatch.setenv("buyer_password", "super-secret-password")
    monkeypatch.setenv("client_secret", "super-secret-client")
    yield create_app(client=fake, store=OrderStore()).test_client()  # type: ignore[arg-type]


def place_order(client: FlaskClient, amount: str = "10.00") -> dict[str, Any]:
    response = client.post("/api/orders", json={"amount": amount})
    assert response.status_code == 201
    return response.get_json()


def test_dashboard_states_demo_only(client: FlaskClient) -> None:
    for path in ("/", "/order"):
        assert "DEMO ONLY" in client.get(path).get_data(as_text=True)


def test_dashboard_shows_every_account_label_and_email(client: FlaskClient) -> None:
    page = client.get("/").get_data(as_text=True)
    for text in ("Business", "Seller A", "Seller B", "Buyer", "a@example.com", "b@example.com", "buyer@example.com"):
        assert text in page
    assert "not configured" in page  # the business email is not set in this test


def test_pages_never_leak_secrets(client: FlaskClient) -> None:
    for path in ("/", "/order"):
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
