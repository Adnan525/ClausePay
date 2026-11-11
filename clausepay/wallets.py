import json
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from clausepay.fees import gross_for_net, sale_fee
from clausepay.paypal_client import Json, PayPalClient, PayPalError

TOPUP_TAG = "topup"  # charges tagged like this are partners transferring money in, never sales


class WalletError(Exception):
    """A wallet could not be authorised or charged. The message is safe to show."""


class WalletStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.lock = threading.RLock()
        self._wallets: dict[str, dict[str, Any]] = {}  # party name -> {vault_id, payer_email, authorized_at}
        self._pending: dict[str, str] = {}  # setup token id -> party name, while the partner is on PayPal's page
        if path and path.exists():
            try:
                raw = json.loads(path.read_text())
                self._wallets, self._pending = raw["wallets"], raw["pending"]
            except (ValueError, KeyError, TypeError):
                self._wallets, self._pending = {}, {}


    def save(self) -> None:
        if self.path is None:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps({"wallets": self._wallets, "pending": self._pending}, indent=2))
            os.chmod(temporary, 0o600)  # these tokens can charge a partner's wallet
            os.replace(temporary, self.path)


    def get(self, party: str) -> dict[str, Any] | None:
        with self.lock:
            return self._wallets.get(party)


    def set(self, party: str, wallet: dict[str, Any]) -> None:
        with self.lock:
            self._wallets[party] = wallet
            self.save()


    def remove(self, party: str) -> None:
        with self.lock:
            self._wallets.pop(party, None)
            self.save()


    def expect(self, setup_id: str, party: str) -> None:
        with self.lock:
            self._pending[setup_id] = party
            self.save()


    def take_pending(self, setup_id: str) -> str | None:
        with self.lock:
            party = self._pending.pop(setup_id, None)
            self.save()
            return party


class WalletService:
    def __init__(self, get_client: Callable[[], PayPalClient], store: WalletStore, emails: Callable[[], dict[str, str]]) -> None:
        self.get_client = get_client
        self.store = store
        self.emails = emails  # party name -> the PayPal email typed in when the contract was frozen


    def is_authorized(self, party: str) -> bool:
        return self.store.get(party) is not None


    def status(self) -> list[dict[str, Any]]:
        return [{"name": name, "email": email, "authorized": self.is_authorized(name),
                 "authorized_at": (self.store.get(name) or {}).get("authorized_at", ""),
                 "payer_email": (self.store.get(name) or {}).get("payer_email", "")}
                for name, email in self.emails().items()]


    def start(self, party: str, return_url: str, cancel_url: str) -> str:
        """Begin authorising a partner's wallet. Returns the PayPal page the partner must approve on."""
        if party not in self.emails():
            raise WalletError(f"{party} is not a party of the frozen contract")
        try:
            setup = self.get_client().create_vault_setup_token(return_url, cancel_url)
            link = self.get_client().approval_link(setup)
        except PayPalError as error:
            raise WalletError(f"PayPal would not start the approval (error {error.status}).") from None
        self.store.expect(setup["id"], party)
        return link


    def complete(self, setup_id: str) -> str:
        """The partner approved on PayPal's page: turn that into a saved wallet. Returns the party's name."""
        party = self.store.take_pending(setup_id)
        if party is None:
            raise WalletError("We were not expecting this approval.")
        try:
            token = self.get_client().create_vault_payment_token(setup_id)
        except PayPalError as error:
            raise WalletError(f"PayPal would not save the wallet (error {error.status}).") from None
        payer = str(token.get("payment_source", {}).get("paypal", {}).get("email_address", "")).strip().lower()
        expected = self.emails().get(party, "").strip().lower()
        if payer and expected and payer != expected:  # someone else's PayPal account approved: never charge it for this partner
            raise WalletError(f"That PayPal account ({payer}) is not {party}'s ({expected}). It was not saved.")
        self.store.set(party, {"vault_id": token["id"], "payer_email": payer,
                               "authorized_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return party


    def charge(self, party: str, share: Decimal, request_id: str, tag: str) -> dict[str, Any]:
        """Charge a partner's wallet so that exactly `share` reaches the business (they pay PayPal's fee on the charge).

        Safe to repeat with the same `request_id`: PayPal returns the same charge instead of charging again.
        """
        wallet = self.store.get(party)
        if wallet is None:
            raise WalletError(f"{party} has not authorized ClausePay to charge their PayPal wallet.")
        gross = gross_for_net(share)
        try:
            order = self.get_client().charge_vaulted(wallet["vault_id"], gross, custom_id=f"{TOPUP_TAG}:{tag}:{party}"[:127], request_id=request_id)
        except PayPalError as error:
            raise WalletError(f"{party}'s wallet could not be charged (error {error.status}).") from None
        capture = order.get("purchase_units", [{}])[0].get("payments", {}).get("captures", [{}])[0]
        if capture.get("status") != "COMPLETED":
            raise WalletError(f"{party}'s charge is {capture.get('status', 'not completed')}.")
        breakdown = capture.get("seller_receivable_breakdown", {})
        received = Decimal(str(breakdown.get("net_amount", {}).get("value", share)))
        return {"party": party, "charged": str(gross), "fee": str(gross - received), "received": str(received),
                "capture_id": capture["id"], "status": "COMPLETED"}
