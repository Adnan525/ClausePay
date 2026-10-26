"""Example usage of each ClausePay feature. Run: python main.py"""
import os
import time

from dotenv import load_dotenv

from clausepay.contract_store import JsonContractStore
from clausepay.deed import Deed
from clausepay.paypal_client import PayPalClient

load_dotenv()


def demo_paypal():
    paypal = PayPalClient()  # reads client_id / client_secret from .env, currency AUD

    # Send AUD 1.00 from the business account to a sandbox user
    sent = paypal.send_payout(os.getenv("u_1_email"), "1.00", note="ClausePay test")
    print("sent:", sent)

    # Check the batch: status and the fee PayPal charged
    time.sleep(5)
    batch = paypal.get_payout(sent["payout_batch_id"])
    print("status:", batch["batch_header"]["batch_status"])
    print("fees:", batch["batch_header"].get("fees"))


def demo_store():
    store = JsonContractStore("data/deeds.json")  # swap for a Postgres store later

    # Payees come from a form, not from the contract text
    deed = store.add(Deed(
        payees=[
            {"name": "Alice", "email": os.getenv("u_1_email")},
            {"name": "Bob", "email": os.getenv("u_2_email")},
        ],
        contract_text="Take PayPal's fee off first. Hold back 10% for 60 days. "
                      "Bob gets everything until his $1,000 licence is repaid, then Alice 60% / Bob 40%.",
    ))
    print("created:", deed.deed_id)

    deed = store.get(deed.deed_id)
    deed.status = "clarifying"
    store.save(deed)
    print("updated:", store.get(deed.deed_id).status)
    print("all deeds:", [d.deed_id for d in store.list()])

    store.delete(deed.deed_id)


if __name__ == "__main__":
    # demo_paypal()  # sends a real sandbox payout each time, so it is commented out
    demo_store()
