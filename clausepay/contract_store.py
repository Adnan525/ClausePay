"""The contract being built: chat history, the draft code, approvals, and the frozen result.

One contract at a time for now (no separate users yet). The record is saved as JSON under
`data/` after every change, and frozen code is written to its own read-only file.
"""
import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SLOTS = 3  # the user writes this many scenarios, and approves each one
APPROVALS_NEEDED = SLOTS
EMAIL_PATTERN = re.compile(r"^[^@\s,;<>()]+@[^@\s,;<>()]+\.[^@\s,;<>()]{2,}$")


def _empty_slots() -> list[Any]:
    return [None] * SLOTS


@dataclass
class ContractRecord:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "chatting"  # chatting | ready | frozen
    version: int = 0
    display: list[dict[str, str]] = field(default_factory=list)  # what the user sees in the chat
    api_messages: list[dict[str, str]] = field(default_factory=list)  # what Claude sees (includes hidden fixes)
    summary: list[str] = field(default_factory=list)
    parties: list[dict[str, str]] = field(default_factory=list)
    code: str = ""
    scenarios: list[dict[str, Any] | None] = field(default_factory=_empty_slots)  # typed in by the user
    simulations: list[dict[str, Any] | None] = field(default_factory=_empty_slots)  # their dry-run results
    emails: dict[str, str] = field(default_factory=dict)  # party name -> PayPal email, typed in by the user
    internal_checks: list[str] = field(default_factory=list)  # titles of Claude's own tests that the code passed
    approvals: list[int] = field(default_factory=list)
    frozen: dict[str, str] | None = None


    def set_draft(self, summary: list[str], parties: list[dict[str, str]], code: str,
                  simulations: list[dict[str, Any] | None], checks: list[str] | None = None) -> None:
        """A new draft replaces the old one, re-checks the user's scenarios, and clears every approval."""
        names = {party["name"] for party in parties}
        self.summary, self.parties, self.code, self.simulations = summary, parties, code, simulations
        self.internal_checks = checks or []
        self.emails = {name: email for name, email in self.emails.items() if name in names}
        self.version += 1
        self.approvals = []
        self.status = "ready"


    def _require_draft(self) -> None:
        if self.status != "ready":
            raise ValueError("The contract is frozen" if self.status == "frozen" else "There is no draft yet")


    def set_scenario(self, slot: int, scenario: dict[str, Any] | None, simulation: dict[str, Any] | None) -> None:
        """Save (or clear, with None) one of the user's scenarios. Its old approval no longer applies."""
        self._require_draft()
        if not 0 <= slot < SLOTS:
            raise ValueError("Unknown scenario")
        self.scenarios[slot], self.simulations[slot] = scenario, simulation
        self.approvals = [index for index in self.approvals if index != slot]


    def approve(self, slot: int) -> None:
        self._require_draft()
        if not 0 <= slot < SLOTS:
            raise ValueError("Unknown scenario")
        result = self.simulations[slot]
        if result is None or "error" in result:
            raise ValueError("Run this scenario successfully before approving it")
        if slot not in self.approvals:
            self.approvals.append(slot)


    def set_emails(self, emails: dict[str, str]) -> None:
        """Store the PayPal email for each party. Raises ValueError with a message for the user."""
        self._require_draft()
        names = {party["name"] for party in self.parties}
        cleaned: dict[str, str] = {}
        for name, email in emails.items():
            if name not in names:
                raise ValueError(f"{name} is not one of the parties")
            email = str(email).strip().lower()
            if not email:
                continue
            if not EMAIL_PATTERN.match(email):
                raise ValueError(f"{name}: \"{email}\" does not look like an email address")
            cleaned[name] = email
        if len(set(cleaned.values())) != len(cleaned):
            raise ValueError("Two parties have the same email address")
        self.emails = cleaned


    def emails_complete(self) -> bool:
        return bool(self.parties) and all(party["name"] in self.emails for party in self.parties)


    def can_freeze(self) -> bool:
        return (self.status == "ready" and len(set(self.approvals)) >= APPROVALS_NEEDED
                and self.emails_complete())


    def public(self) -> dict[str, Any]:
        """The record as the browser sees it (no hidden messages)."""
        return {
            "id": self.id, "status": self.status, "version": self.version, "display": self.display,
            "summary": self.summary, "parties": self.parties, "code": self.code,
            "scenarios": self.scenarios, "simulations": self.simulations, "emails": self.emails,
            "emails_complete": self.emails_complete(), "internal_checks": self.internal_checks,
            "approvals": sorted(self.approvals),
            "approvals_needed": APPROVALS_NEEDED, "frozen": self.frozen,
        }


class ContractStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = threading.Lock()  # held while Claude is answering, so messages never overlap
        self._record = self._load()


    @property
    def path(self) -> Path:
        return self.directory / "contract.json"


    def _load(self) -> ContractRecord:
        if self.path.exists():
            try:
                return ContractRecord(**json.loads(self.path.read_text()))
            except (ValueError, TypeError):
                pass  # unreadable file: start fresh rather than crash the app
        return ContractRecord()


    def current(self) -> ContractRecord:
        return self._record


    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self._record), indent=2))
        os.replace(temporary, self.path)


    def reset(self) -> ContractRecord:
        """Start a new contract. Frozen contracts already written to disk are left untouched."""
        self._record = ContractRecord()
        self.save()
        return self._record


    def freeze(self) -> dict[str, str]:
        """Write the approved code to its own file and mark the contract frozen."""
        record = self._record
        if not record.can_freeze():
            raise ValueError(f"Freezing needs {APPROVALS_NEEDED} approved scenarios and an email for every party")
        folder = self.directory / "frozen"
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(record.code.encode()).hexdigest()
        code_path = folder / f"{record.id}-v{record.version}.py"
        code_path.write_text(record.code)
        code_path.chmod(0o444)
        meta = {"contract_id": record.id, "version": str(record.version), "sha256": digest,
                "file": str(code_path), "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        code_path.with_suffix(".json").write_text(json.dumps({**meta, "parties": record.parties,
                                                              "emails": record.emails,
                                                              "summary": record.summary}, indent=2))
        record.frozen, record.status = meta, "frozen"
        self.save()
        return meta
