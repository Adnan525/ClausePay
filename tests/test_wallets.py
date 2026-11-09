import json
import os
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from clausepay.fees import gross_for_net, sale_fee
from clausepay.paypal_client import PayPalError
from clausepay.wallets import TOPUP_TAG, WalletError, WalletService, WalletStore
from tests.test_contract_runner import EMAILS
from tests.test_live import FakePayPal, d


@pytest.fixture
def paypal() -> FakePayPal:
    return FakePayPal()


@pytest.fixture
def wallets(tmp_path: Path, paypal: FakePayPal) -> WalletService:
    return WalletService(lambda: paypal, WalletStore(tmp_path / "wallets.json"), lambda: EMAILS)  # type: ignore[arg-type, return-value]


def authorize(wallets: WalletService, paypal: FakePayPal, party: str = "Alice", payer: str | None = None) -> str:
    link = wallets.start(party, "https://app/return", "https://app/cancel")
    setup_id = link.rsplit("=", 1)[1]
    paypal.setup_payer[setup_id] = payer if payer is not None else EMAILS[party]
    return wallets.complete(setup_id)


# ---------- charging enough that the exact share arrives ----------

@pytest.mark.parametrize("net", ["0.01", "1.00", "24.40", "156.17", "390.40", "999.99"])
def test_the_charge_is_grossed_up_so_the_business_receives_the_exact_share(net: str) -> None:
    gross = gross_for_net(d(net))
    received = gross - sale_fee(gross)
    assert d(net) <= received <= d(net) + d("0.01")
    assert gross - sale_fee(gross - d("0.01")) < d(net) or True  # and it is the smallest such amount
    assert gross - d("0.01") - sale_fee(gross - d("0.01")) < d(net)


# ---------- authorizing ----------

def test_a_partner_is_sent_to_paypal_and_the_approval_is_saved(wallets: WalletService, paypal: FakePayPal) -> None:
    assert [w["authorized"] for w in wallets.status()] == [False, False]
    link = wallets.start("Alice", "https://app/return", "https://app/cancel")
    assert link.startswith("https://paypal.test/approve")
    assert authorize(wallets, paypal, "Alice") == "Alice"
    status = {w["name"]: w for w in wallets.status()}
    assert status["Alice"]["authorized"] is True and status["Alice"]["payer_email"] == EMAILS["Alice"]
    assert status["Bob"]["authorized"] is False and wallets.is_authorized("Alice")


def test_only_parties_of_the_contract_can_authorize(wallets: WalletService) -> None:
    with pytest.raises(WalletError, match="not a party"):
        wallets.start("Mallory", "r", "c")


def test_an_approval_we_were_not_expecting_is_refused(wallets: WalletService) -> None:
    with pytest.raises(WalletError, match="not expecting"):
        wallets.complete("SETUP-FORGED")


def test_a_wallet_belonging_to_someone_else_is_never_saved(wallets: WalletService, paypal: FakePayPal) -> None:
    with pytest.raises(WalletError, match="is not Alice's"):
        authorize(wallets, paypal, "Alice", payer="bob@example.com")  # Bob's PayPal account approved Alice's slot
    assert not wallets.is_authorized("Alice")


def test_an_approval_can_only_be_used_once(wallets: WalletService, paypal: FakePayPal) -> None:
    link = wallets.start("Alice", "r", "c")
    setup_id = link.rsplit("=", 1)[1]
    paypal.setup_payer[setup_id] = EMAILS["Alice"]
    wallets.complete(setup_id)
    with pytest.raises(WalletError, match="not expecting"):
        wallets.complete(setup_id)


def test_wallets_survive_a_restart_and_the_file_is_private(tmp_path: Path, paypal: FakePayPal) -> None:
    first = WalletService(lambda: paypal, WalletStore(tmp_path / "wallets.json"), lambda: EMAILS)  # type: ignore[arg-type]
    authorize(first, paypal, "Alice")
    again = WalletService(lambda: paypal, WalletStore(tmp_path / "wallets.json"), lambda: EMAILS)  # type: ignore[arg-type]
    assert again.is_authorized("Alice") and not again.is_authorized("Bob")
    assert stat.S_IMODE(os.stat(tmp_path / "wallets.json").st_mode) == 0o600
    assert json.loads((tmp_path / "wallets.json").read_text())["wallets"]["Alice"]["vault_id"] == "VAULT-SETUP1"


def test_a_wallet_can_be_removed(wallets: WalletService, paypal: FakePayPal) -> None:
    authorize(wallets, paypal, "Alice")
    wallets.store.remove("Alice")
    assert not wallets.is_authorized("Alice")


def test_a_damaged_wallet_file_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "wallets.json").write_text("{nope")
    assert WalletStore(tmp_path / "wallets.json").get("Alice") is None


def test_paypal_refusing_the_approval_is_explained(wallets: WalletService, paypal: FakePayPal) -> None:
    paypal.create_vault_setup_token = lambda *a, **k: (_ for _ in ()).throw(PayPalError("no", 403, {}))  # type: ignore[method-assign]
    with pytest.raises(WalletError, match="would not start the approval"):
        wallets.start("Alice", "r", "c")


# ---------- charging ----------

def test_a_charge_delivers_the_exact_share_and_the_partner_pays_the_fee(wallets: WalletService, paypal: FakePayPal) -> None:
    authorize(wallets, paypal, "Alice")
    result = wallets.charge("Alice", d("24.40"), "charge-id-1", "refund-9")
    assert result["received"] == "24.40" and result["charged"] == "25.31" and result["fee"] == "0.91" and result["status"] == "COMPLETED"
    assert paypal.charges["charge-id-1"]["custom_id"] == f"{TOPUP_TAG}:refund-9:Alice"


def test_charging_twice_with_the_same_id_charges_once(wallets: WalletService, paypal: FakePayPal) -> None:
    authorize(wallets, paypal, "Alice")
    first = wallets.charge("Alice", d("24.40"), "charge-id-1", "refund-9")
    again = wallets.charge("Alice", d("24.40"), "charge-id-1", "refund-9")
    assert first["capture_id"] == again["capture_id"] and len(paypal.charges) == 1


def test_a_partner_who_never_authorized_cannot_be_charged(wallets: WalletService) -> None:
    with pytest.raises(WalletError, match="has not authorized"):
        wallets.charge("Alice", d("10.00"), "x", "y")


def test_a_declined_charge_is_explained(wallets: WalletService, paypal: FakePayPal) -> None:
    authorize(wallets, paypal, "Alice")
    paypal.declined_vaults.add("VAULT-SETUP1")
    with pytest.raises(WalletError, match="Alice's wallet could not be charged"):
        wallets.charge("Alice", d("10.00"), "x", "y")
