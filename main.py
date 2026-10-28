"""Example usage of each ClausePay feature. Run: python main.py"""
import os
import time

from dotenv import load_dotenv

from clausepay.contract_analyzer import ContractAnalyzer
from clausepay.contract_store import JsonContractStore
from clausepay.deed import Deed
from clausepay.paypal_client import PayPalClient
from clausepay.pdf_renderer import PdfRenderer
from clausepay.webapp import create_app

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

    # Payees are attached later (from the web form), once the contract is settled
    deed = store.add(Deed(
        contract_text="Take PayPal's fee off first. Hold back 10% for 60 days. "
                      "Bob gets everything until his $1,000 licence is repaid, then Alice 60% / Bob 40%.",
    ))
    print("created:", deed.deed_id)

    deed = store.get(deed.deed_id)
    deed.status = "clarifying"
    deed.payees = [
        {"name": "Alice", "email": os.getenv("u_1_email")},
        {"name": "Bob", "email": os.getenv("u_2_email")},
    ]
    store.save(deed)
    print("updated:", store.get(deed.deed_id).status)
    print("all deeds:", [d.deed_id for d in store.list()])

    store.delete(deed.deed_id)


def demo_pdf():
    blocks = [
        {"type": "heading", "text": "What you received"},
        {"type": "paragraph", "text": "You received $156.00. Of $630 in sales, $30 went to PayPal fees "
                                      "and $60 was held back."},
        {"type": "table", "header": ["Step", "Amount"],
         "rows": [["Sales", "$630.00"], ["PayPal fees", "-$30.00"], ["Held back (10%)", "-$60.00"]]},
    ]
    path = PdfRenderer().render("Alice - Week 2 statement", blocks, "output/example.pdf")
    print("wrote:", path)


def demo_analyzer():
    """Two rounds with Claude: first it asks questions, then it returns the refined contract."""
    analyzer = ContractAnalyzer()
    contract = open("examples/alice_bob_contract.txt").read()

    first = analyzer.analyze(contract)
    for q in first["questions"]:
        print("Q:", q["question"], q["options"])

    # In the web app the user picks/types these; here we just take each first suggestion
    answers = [{"question": q["question"], "answer": q["options"][0]} for q in first["questions"]]
    second = analyzer.analyze(contract, answers)
    print(second["status"], "-", second["refined_contract"]["summary"])


def run_web():
    """Starts the web app at http://127.0.0.1:5000 (paste or upload a contract, answer questions)."""
    create_app().run(port=5000)


if __name__ == "__main__":
    # demo_paypal()  # sends a real sandbox payout each time, so it is commented out
    # demo_store()
    # demo_pdf()
    # demo_analyzer()  # calls Claude
    run_web()
