"""The record kept for each contract ("deed")."""
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

STATUSES = ("draft", "clarifying", "refined", "pending_approval", "approved", "active")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Deed:
    contract_text: str
    payees: list = field(default_factory=list)  # [{"name", "email"}], from a form once the contract is settled
    deed_id: str = field(default_factory=lambda: f"deed_{uuid.uuid4().hex[:8]}")
    clarifications: list = field(default_factory=list)  # answered [{"question", "answer"}]
    pending_questions: list = field(default_factory=list)  # [{"question", "options"}] awaiting an answer
    refined_contract: dict = None  # fixed-schema contract, see contract_schema.py
    status: str = "draft"
    ledger: list = field(default_factory=list)  # sales, holdbacks, payouts
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        for payee in self.payees:
            if not payee.get("name") or "@" not in payee.get("email", ""):
                raise ValueError(f"payee needs a name and an email: {payee}")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)
