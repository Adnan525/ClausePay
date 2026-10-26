from unittest.mock import patch, MagicMock

import pytest

from clausepay.paypal_client import PayPalClient, PayPalError


def fake_response(body, ok=True, status=200):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status
    resp.text = str(body)
    resp.json.return_value = body
    return resp


@pytest.fixture
def client():
    return PayPalClient(client_id="id", client_secret="secret")


def test_cents_rounds_and_formats():
    assert PayPalClient._cents("1.005") == "1.01"
    assert PayPalClient._cents(3) == "3.00"


@pytest.mark.parametrize("bad", [0, -1, "0.001"])
def test_cents_rejects_non_positive(bad):
    with pytest.raises(PayPalError):
        PayPalClient._cents(bad)


def test_send_payout_builds_expected_body(client):
    token = fake_response({"access_token": "tok", "expires_in": 3600})
    payout = fake_response({"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}})
    with patch("clausepay.paypal_client.requests.post", return_value=token), \
         patch("clausepay.paypal_client.requests.request", return_value=payout) as req:
        result = client.send_payout("a@b.com", "1.5")

    assert result["payout_batch_id"] == "B1"
    item = req.call_args.kwargs["json"]["items"][0]
    assert item["receiver"] == "a@b.com"
    assert item["amount"] == {"value": "1.50", "currency": "AUD"}
    assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"


def test_api_error_raises(client):
    token = fake_response({"access_token": "tok", "expires_in": 3600})
    bad = fake_response({"name": "VALIDATION_ERROR"}, ok=False, status=400)
    with patch("clausepay.paypal_client.requests.post", return_value=token), \
         patch("clausepay.paypal_client.requests.request", return_value=bad):
        with pytest.raises(PayPalError):
            client.get_payout("nope")
