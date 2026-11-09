from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from clausepay.paypal_client import PayPalClient, PayPalError


def fake_response(body: dict[str, Any], status: int = 200) -> MagicMock:
    response = MagicMock()
    response.ok = status < 400
    response.status_code = status
    response.content = b"x"
    response.json.return_value = body
    return response


@pytest.fixture
def client() -> PayPalClient:
    paypal = PayPalClient(client_id="id", client_secret="secret", currency="AUD")
    paypal._token = "token"
    paypal._token_expires_at = 9e12
    return paypal


def test_missing_credentials_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("client_id", raising=False)
    monkeypatch.delenv("client_secret", raising=False)
    with patch("clausepay.paypal_client.load_dotenv"):
        with pytest.raises(PayPalError):
            PayPalClient()


def test_token_is_cached() -> None:
    paypal = PayPalClient(client_id="id", client_secret="secret")
    with patch("clausepay.paypal_client.requests.post", return_value=fake_response(
        {"access_token": "abc", "expires_in": 3600}
    )) as post:
        assert paypal._get_token() == "abc"
        assert paypal._get_token() == "abc"
    assert post.call_count == 1


def test_amounts_must_be_positive_decimals(client: PayPalClient) -> None:
    assert client._format_amount(Decimal("10.005")) == "10.01"
    with pytest.raises(PayPalError):
        client._format_amount(Decimal("0"))
    with pytest.raises(PayPalError):
        client._format_amount(10.5)  # type: ignore[arg-type]


def test_percentage_fee_defaults_to_two_point_four_percent_and_matches_sandbox(client: PayPalClient) -> None:
    assert client.percentage_fee(Decimal("10.00")) == Decimal("0.24")
    assert client.percentage_fee(Decimal("7.00")) == Decimal("0.17")  # sandbox returned 0.17 on a A$7 refund
    assert client.percentage_fee(Decimal("333.33")) == Decimal("8.00")  # and 8.00 on A$333.33


@pytest.mark.parametrize("amount, fee", [
    ("1.00", "0.32"), ("10.00", "0.54"), ("20.00", "0.78"), ("100.00", "2.70"),
    ("250.00", "6.30"), ("1000.00", "24.30"),
])
def test_sale_fee_matches_every_sandbox_sale(client: PayPalClient, amount: str, fee: str) -> None:
    assert client.sale_fee(Decimal(amount)) == Decimal(fee)


def test_fixed_fee_is_configurable() -> None:
    paypal = PayPalClient(client_id="id", client_secret="secret", fixed_fee=Decimal("0.59"))
    assert paypal.sale_fee(Decimal("100.00")) == Decimal("2.99")


def test_fee_rate_is_configurable() -> None:
    paypal = PayPalClient(client_id="id", client_secret="secret", fee_rate=Decimal("0.029"))
    assert paypal.percentage_fee(Decimal("100.00")) == Decimal("2.90")


def test_payout_uses_currency_field_and_one_item_per_email(client: PayPalClient) -> None:
    reply = {"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}}
    with patch("clausepay.paypal_client.requests.request", return_value=fake_response(reply)) as request:
        result = client.send_payouts({"a@x.com": Decimal("60.00"), "b@x.com": Decimal("40.00")})
    items = request.call_args.kwargs["json"]["items"]
    assert [item["receiver"] for item in items] == ["a@x.com", "b@x.com"]
    assert items[0]["amount"] == {"value": "60.00", "currency": "AUD"}
    assert result["payout_batch_id"] == "B1"


def test_create_order_with_return_urls_sets_experience_context(client: PayPalClient) -> None:
    with patch("clausepay.paypal_client.requests.request", return_value=fake_response({"id": "O1"})) as request:
        client.create_order(Decimal("10"), return_url="https://app/return", cancel_url="https://app/cancel")
    context = request.call_args.kwargs["json"]["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == "https://app/return" and context["cancel_url"] == "https://app/cancel"
    assert context["user_action"] == "PAY_NOW"


def test_approval_link_accepts_both_link_names() -> None:
    new = {"links": [{"rel": "self", "href": "x"}, {"rel": "payer-action", "href": "https://pay/new"}]}
    old = {"links": [{"rel": "approve", "href": "https://pay/old"}]}
    assert PayPalClient.approval_link(new) == "https://pay/new"
    assert PayPalClient.approval_link(old) == "https://pay/old"
    with pytest.raises(PayPalError):
        PayPalClient.approval_link({"links": []})


def test_refund_without_amount_is_full_refund(client: PayPalClient) -> None:
    with patch("clausepay.paypal_client.requests.request", return_value=fake_response({"id": "R1"})) as request:
        client.refund_capture("CAP1")
    assert request.call_args.kwargs["json"] == {}
    assert request.call_args.kwargs["headers"]["PayPal-Request-Id"]


def test_partial_refund_sends_amount(client: PayPalClient) -> None:
    with patch("clausepay.paypal_client.requests.request", return_value=fake_response({"id": "R1"})) as request:
        client.refund_capture("CAP1", Decimal("5"))
    assert request.call_args.kwargs["json"]["amount"] == {"currency_code": "AUD", "value": "5.00"}


def test_available_balance_is_read_for_client_currency(client: PayPalClient) -> None:
    reply = {"balances": [
        {"currency": "USD", "available_balance": {"value": "1.00"}},
        {"currency": "AUD", "available_balance": {"value": "250.50"}},
    ]}
    with patch("clausepay.paypal_client.requests.request", return_value=fake_response(reply)):
        assert client.get_available_balance() == Decimal("250.50")


def test_failed_call_raises_with_status_and_body(client: PayPalClient) -> None:
    with patch("clausepay.paypal_client.requests.request", return_value=fake_response({"name": "NOPE"}, 403)):
        with pytest.raises(PayPalError) as caught:
            client.get_balances()
    assert caught.value.status == 403
    assert caught.value.body == {"name": "NOPE"}


def test_charge_vaulted_does_not_capture_an_already_completed_order(client: PayPalClient) -> None:
    with patch("clausepay.paypal_client.requests.request",
               return_value=fake_response({"id": "O1", "status": "COMPLETED"})) as request:
        result = client.charge_vaulted("VAULT1", Decimal("30"), custom_id="topup:1")
    assert request.call_count == 1
    sent = request.call_args.kwargs["json"]
    assert sent["payment_source"] == {"paypal": {"vault_id": "VAULT1"}}
    assert sent["purchase_units"][0]["custom_id"] == "topup:1"
    assert result["status"] == "COMPLETED"


def test_charge_vaulted_captures_when_order_is_not_completed(client: PayPalClient) -> None:
    replies = [fake_response({"id": "O1", "status": "APPROVED"}), fake_response({"status": "COMPLETED"})]
    with patch("clausepay.paypal_client.requests.request", side_effect=replies) as request:
        client.charge_vaulted("VAULT1", Decimal("30"))
    assert request.call_args_list[1].args[1].endswith("/v2/checkout/orders/O1/capture")


def test_verify_webhook_reads_paypal_headers(client: PayPalClient) -> None:
    headers = {"PAYPAL-TRANSMISSION-ID": "t1", "paypal-auth-algo": "algo"}
    with patch("clausepay.paypal_client.requests.request",
               return_value=fake_response({"verification_status": "SUCCESS"})) as request:
        assert client.verify_webhook("WH1", headers, {"id": "EVT"}) is True
    sent = request.call_args.kwargs["json"]
    assert sent["transmission_id"] == "t1" and sent["webhook_id"] == "WH1"
