"""The record kept for each contract ("deed")."""
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

STATUSES = ("draft", "clarifying", "refined", "payees_collected", "pending_approval", "approved", "active")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_payees(payees):
    """Raises ValueError unless every payee is {"name", "email"} with a plausible email."""
    for payee in payees:
        if not payee.get("name") or not _EMAIL.match(payee.get("email", "")):
            raise ValueError(f"payee needs a name and a valid email: {payee}")


@dataclass
class Deed:
    contract_text: str
    payees: list = field(default_factory=list)  # [{"name", "email"}], from the payee form once refined
    deed_id: str = field(default_factory=lambda: f"deed_{uuid.uuid4().hex[:8]}")
    clarifications: list = field(default_factory=list)  # answered [{"question", "answer"}]
    pending_questions: list = field(default_factory=list)  # [{"question", "options"}] awaiting an answer
    refined_contract: dict = None  # summary, payees (names only) and rules, see contract_schema.py
    status: str = "draft"
    ledger: list = field(default_factory=list)  # sales, holdbacks, payouts
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        validate_payees(self.payees)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)
