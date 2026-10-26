"""Example usage of each ClausePay feature. Run: python main.py"""
import os
import time

from dotenv import load_dotenv

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


if __name__ == "__main__":
    demo_paypal()
