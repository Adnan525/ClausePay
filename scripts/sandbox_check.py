"""Live PayPal sandbox checks. Run: .venv/bin/python scripts/sandbox_check.py

Each check is independent and prints PayPal's raw answer, so we can see what the
sandbox actually supports. Nothing here prints credentials.
"""
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clausepay.paypal_client import PayPalClient, PayPalError  # noqa: E402


def show(title: str, check: Callable[[], object]) -> None:
    print(f"\n=== {title}")
    try:
        print(json.dumps(check(), indent=2, default=str))
    except PayPalError as error:
        print(f"FAILED: {error} status={error.status}")
        print(json.dumps(error.body, indent=2)[:1500])


def recent_transactions(client: PayPalClient) -> object:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=3)
    fmt = "%Y-%m-%dT%H:%M:%S-0000"
    result = client.list_transactions(start.strftime(fmt), end.strftime(fmt))
    return {"count": len(result.get("transaction_details", [])), "keys": list(result)}


def main() -> None:
    load_dotenv()
    client = PayPalClient()
    show("auth + balances", client.get_balances)
    show("available AUD balance", lambda: str(client.get_available_balance()))
    show("transaction search (last 3 days)", lambda: recent_transactions(client))
    show("vault setup token for a partner wallet", lambda: client.create_vault_setup_token(
        "https://example.com/return", "https://example.com/cancel"))
    show("create order (needs buyer approval to capture)", lambda: client.create_order(
        Decimal("10.00"), custom_id="sandbox-check"))
    partner = os.environ["u_1_email"].strip()
    show("payout A$1.00 to partner 1", lambda: client.send_payouts({partner: Decimal("1.00")}))


if __name__ == "__main__":
    main()
