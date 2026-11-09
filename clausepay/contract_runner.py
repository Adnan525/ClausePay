"""Runs a frozen contract on real sales and refunds, and remembers what it has done.

The generated functions stay pure: they remember nothing. This runner owns the memory. For one
frozen contract it keeps a journal file (next to the frozen code) with one entry per sale or
refund. Before every run it re-hashes the code and refuses to run if the code changed, ignores
a sale it has already seen, and only moves the contract's state forward once the payout is
confirmed. Every contract has its own code, journal and state; nothing is shared.
"""
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from clausepay.contract_sandbox import from_json, to_json
from clausepay.contract_sim import ContractProblem, StepOutcome, apply_refund, apply_sale
from clausepay.fees import CENT, DEFAULT_FEE_RATE, DEFAULT_FIXED_FEE, percentage_fee, sale_fee

ZERO = Decimal("0.00")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class RunnerError(Exception):
    """Base class: the runner refused to process a transaction. The message is safe to show."""


class CodeChangedError(RunnerError):
    """The contract's code no longer matches the hash recorded when it was frozen."""


class FeeMismatchError(RunnerError):
    """PayPal's reported fee differs from the expected fee, so the transaction is paused for a human."""


class PendingTransactionError(RunnerError):
    """An earlier transaction is still waiting for its payout to be confirmed."""


class InvalidTransactionError(RunnerError):
    """The sale or refund itself is not acceptable (bad amount, unknown sale, over-refund)."""


class ContractFailedError(RunnerError):
    """The contract's code broke one of the rules on this transaction."""


@dataclass(frozen=True)
class Transaction:
    """A journal entry as returned to the caller."""
    entry_id: str
    kind: str  # sale | refund
    status: str  # pending | committed | failed
    duplicate: bool  # True when this is the stored result of an earlier, identical request
    amounts: dict[str, Decimal]  # sale: what each party is paid; refund: what each must transfer in
    reserve_change: Decimal
    reserve_after: Decimal
    state_after: dict[str, Any]
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


    def by_email(self, emails: dict[str, str]) -> dict[str, Decimal]:
        """Amounts keyed by PayPal email (zero amounts dropped), for paying out or asking partners to transfer in."""
        missing = [name for name, value in self.amounts.items() if value > 0 and name not in emails]
        if missing:
            raise RunnerError(f"No email is recorded for: {', '.join(missing)}")
        return {emails[name]: value for name, value in self.amounts.items() if value > 0}


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path.resolve()), threading.Lock())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_money(value: Any, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value != value.quantize(CENT):
        raise InvalidTransactionError(f"{label} must be an amount in whole cents")
    return value


class ContractRunner:
    def __init__(self, code_path: Path, rate: Decimal = DEFAULT_FEE_RATE, fixed: Decimal = DEFAULT_FIXED_FEE) -> None:
        self.code_path = Path(code_path)
        self.meta_path = self.code_path.with_suffix(".json")
        self.journal_path = self.code_path.with_suffix(".journal.json")
        self.rate, self.fixed = rate, fixed
        try:
            meta = json.loads(self.meta_path.read_text())
            self.expected_hash: str = meta["sha256"]
            self.parties: list[str] = [party["name"] for party in meta["parties"]]
            self.emails: dict[str, str] = meta.get("emails", {})
            self.contract_id: str = meta["contract_id"]
            self.version: str = str(meta["version"])
        except (OSError, ValueError, KeyError) as error:
            raise RunnerError(f"The frozen contract's details could not be read ({type(error).__name__})") from None
        self._lock = _lock_for(self.journal_path)


    # ---- checks and storage -------------------------------------------------------------

    def _verified_code(self) -> tuple[str, str]:
        """The code text and its hash, only if it still matches what was frozen. Hash and run the same bytes."""
        try:
            raw = self.code_path.read_bytes()
        except OSError:
            raise CodeChangedError("The contract's code file is missing") from None
        digest = hashlib.sha256(raw).hexdigest()
        if digest != self.expected_hash:
            raise CodeChangedError("The contract's code does not match the hash recorded when it was frozen. Nothing was run.")
        return raw.decode(), digest


    def _load_journal(self, digest: str) -> dict[str, Any]:
        if not self.journal_path.exists():
            return {"contract_id": self.contract_id, "version": self.version, "code_sha256": digest, "entries": []}
        try:
            journal = from_json(self.journal_path.read_text())
            entries = journal["entries"]
            recorded = journal["code_sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            raise RunnerError("The contract's journal could not be read, so nothing was run. It was not reset.") from None
        if not isinstance(entries, list) or recorded != digest:
            raise CodeChangedError("This journal belongs to different code than the contract now has. Nothing was run.")
        return journal


    def _save(self, journal: dict[str, Any]) -> None:
        temporary = self.journal_path.with_name(self.journal_path.name + ".tmp")
        with open(temporary, "w") as handle:
            handle.write(to_json(journal))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.journal_path)


    @staticmethod
    def _live(journal: dict[str, Any]) -> list[dict[str, Any]]:
        """Entries that count: pending and committed (failed ones are history only)."""
        return [entry for entry in journal["entries"] if entry["status"] != "failed"]


    def _refunded(self, journal: dict[str, Any], sale_id: str) -> Decimal:
        return sum((entry["amount"] for entry in self._live(journal)
                    if entry["kind"] == "refund" and entry["sale_id"] == sale_id), ZERO)


    @staticmethod
    def _find(journal: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
        return next((entry for entry in journal["entries"] if entry["entry_id"] == entry_id and entry["status"] != "failed"), None)


    def _current(self, journal: dict[str, Any]) -> tuple[dict[str, Any], Decimal]:
        live = self._live(journal)
        return (live[-1]["state_after"], live[-1]["reserve_after"]) if live else ({}, ZERO)


    @staticmethod
    def _as_transaction(entry: dict[str, Any], duplicate: bool = False) -> Transaction:
        return Transaction(entry["entry_id"], entry["kind"], entry["status"], duplicate, entry["amounts"],
                           entry["reserve_change"], entry["reserve_after"], entry["state_after"],
                           entry["lines"], entry["notes"])


    def _record(self, journal: dict[str, Any], entry_id: str, kind: str, sale_id: str, amount: Decimal,
                fee: Decimal, outcome: StepOutcome) -> Transaction:
        attempt = 1 + sum(1 for entry in journal["entries"] if entry["entry_id"] == entry_id)
        entry = {"entry_id": entry_id, "kind": kind, "sale_id": sale_id, "attempt": attempt, "amount": amount,
                 "fee": fee, "amounts": outcome.amounts, "reserve_change": outcome.reserve_change,
                 "reserve_after": outcome.reserve_after, "state_after": outcome.state, "lines": outcome.lines,
                 "notes": outcome.notes, "status": "pending", "recorded_at": _now()}
        journal["entries"].append(entry)
        self._save(journal)
        return self._as_transaction(entry)


    # ---- processing ---------------------------------------------------------------------

    def process_sale(self, sale_id: str, sale_amount: Decimal, paypal_fee: Decimal) -> Transaction:
        """Run the contract on a sale. The result is recorded as pending until `commit` confirms the payout."""
        with self._lock:
            code, digest = self._verified_code()
            journal = self._load_journal(digest)
            entry_id = f"sale:{sale_id}"
            if existing := self._find(journal, entry_id):
                return self._as_transaction(existing, duplicate=True)
            self._require_nothing_pending(journal)
            amount, fee = _require_money(sale_amount, "The sale amount"), _require_money(paypal_fee, "PayPal's fee")
            if amount <= 0 or not 0 <= fee < amount:
                raise InvalidTransactionError("The sale amount must be positive and PayPal's fee must be less than the sale")
            expected = sale_fee(amount, self.rate, self.fixed)
            if fee != expected:
                raise FeeMismatchError(f"PayPal reported a fee of {fee} but {expected} was expected for a sale of {amount}. Paused.")
            state, reserve = self._current(journal)
            try:
                outcome = apply_sale(code, self.parties, sale_id, amount, fee, state, reserve)
            except ContractProblem as problem:
                raise ContractFailedError(" ".join(problem.problems)) from None
            return self._record(journal, entry_id, "sale", sale_id, amount, fee, outcome)


    def process_refund(self, sale_id: str, refund_id: str, refund_amount: Decimal, fee_returned: Decimal) -> Transaction:
        """Run the contract on a refund of an earlier sale (`refund_id` is PayPal's id for this refund)."""
        with self._lock:
            code, digest = self._verified_code()
            journal = self._load_journal(digest)
            entry_id = f"refund:{refund_id}"
            if existing := self._find(journal, entry_id):
                return self._as_transaction(existing, duplicate=True)
            self._require_nothing_pending(journal)
            amount, returned = _require_money(refund_amount, "The refund amount"), _require_money(fee_returned, "The returned fee")
            sale = self._find(journal, f"sale:{sale_id}")
            if sale is None:
                raise InvalidTransactionError(f"Sale {sale_id} is not in this contract's journal")
            already = self._refunded(journal, sale_id)
            if amount <= 0 or amount > sale["amount"] - already:
                raise InvalidTransactionError(f"Only {sale['amount'] - already} of sale {sale_id} is left to refund")
            expected = percentage_fee(amount, self.rate)
            if returned != expected:
                raise FeeMismatchError(f"PayPal reported returning {returned} of its fee but {expected} was expected. Paused.")
            state, reserve = self._current(journal)
            try:
                outcome = apply_refund(code, self.parties, sale_id, amount, returned, state, reserve)
            except ContractProblem as problem:
                raise ContractFailedError(" ".join(problem.problems)) from None
            return self._record(journal, entry_id, "refund", sale_id, amount, returned, outcome)


    def refundable(self, sale_id: str) -> Decimal:
        """How much of a sale can still be refunded: the sale amount minus every refund already counted."""
        with self._lock:
            _, digest = self._verified_code()
            journal = self._load_journal(digest)
            sale = self._find(journal, f"sale:{sale_id}")
            if sale is None:
                raise InvalidTransactionError(f"Sale {sale_id} is not in this contract's journal")
            return sale["amount"] - self._refunded(journal, sale_id)


    def quote_refund(self, sale_id: str, refund_amount: Decimal) -> Transaction:
        """What the contract would decide for a refund, without recording anything (status "quote").

        The returned fee is assumed to be the expected one; `process_refund` later checks PayPal's real figure.
        """
        with self._lock:
            code, digest = self._verified_code()
            journal = self._load_journal(digest)
            self._require_nothing_pending(journal)
            amount = _require_money(refund_amount, "The refund amount")
            sale = self._find(journal, f"sale:{sale_id}")
            if sale is None:
                raise InvalidTransactionError(f"Sale {sale_id} is not in this contract's journal")
            left = sale["amount"] - self._refunded(journal, sale_id)
            if amount <= 0 or amount > left:
                raise InvalidTransactionError(f"Only {left} of sale {sale_id} is left to refund")
            state, reserve = self._current(journal)
            try:
                outcome = apply_refund(code, self.parties, sale_id, amount, percentage_fee(amount, self.rate), state, reserve)
            except ContractProblem as problem:
                raise ContractFailedError(" ".join(problem.problems)) from None
            return Transaction(f"quote:{sale_id}", "refund", "quote", False, outcome.amounts, outcome.reserve_change,
                               outcome.reserve_after, outcome.state, outcome.lines, outcome.notes)


    @staticmethod
    def _require_nothing_pending(journal: dict[str, Any]) -> None:
        pending = [entry["entry_id"] for entry in journal["entries"] if entry["status"] == "pending"]
        if pending:
            raise PendingTransactionError(f"{pending[0]} is still waiting for its payout to be confirmed")


    # ---- finishing a transaction --------------------------------------------------------

    def _finish(self, entry_id: str, status: str, reason: str = "") -> Transaction:
        with self._lock:
            _, digest = self._verified_code()
            journal = self._load_journal(digest)
            entry = next((e for e in journal["entries"] if e["entry_id"] == entry_id and e["status"] == "pending"), None)
            if entry is None:
                raise InvalidTransactionError(f"{entry_id} is not waiting to be confirmed")
            entry["status"], entry["finished_at"] = status, _now()
            if reason:
                entry["failure"] = reason[:300]
            self._save(journal)
            return self._as_transaction(entry)


    def commit(self, entry_id: str) -> Transaction:
        """The payout (or the partners' transfers) was confirmed: the state moves forward for good."""
        return self._finish(entry_id, "committed")


    def fail(self, entry_id: str, reason: str) -> Transaction:
        """The payout did not happen: this entry stops counting, and the state stays where it was."""
        return self._finish(entry_id, "failed", reason)


    # ---- reading ------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """The contract's current remembered state (what the next transaction will start from)."""
        with self._lock:
            return self._current(self._load_journal(self._verified_code()[1]))[0]


    def reserve(self) -> Decimal:
        """How much is currently held back, tracked by the runner itself, not by the contract's code."""
        with self._lock:
            return self._current(self._load_journal(self._verified_code()[1]))[1]


    def entries(self) -> list[dict[str, Any]]:
        """The whole journal, oldest first, including failed attempts."""
        with self._lock:
            return list(self._load_journal(self._verified_code()[1])["entries"])
